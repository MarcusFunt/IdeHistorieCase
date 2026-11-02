"""Additive session-scoped historical evidence endpoint."""
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.connection import session_factory
from app.db.models import GameSession, RagScenarioPolicy, ScenarioVersion
from app.game.routes import owned_session, require_token
from app.rag.clients import EmbeddingServiceClient, RerankServiceClient
from app.rag.policy import EvidenceContext, is_eligible
from app.rag.postgres import PostgresRagRepository
from app.rag.retrieval import RAG_NO_EVIDENCE, RagSearchService
from app.rag.schemas import EvidenceSearchRequest

router = APIRouter(prefix="/api/v1")


async def get_rag_db() -> AsyncIterator[AsyncSession]:
    async with session_factory() as db:
        yield db


async def get_rag_search_service(
    db: AsyncSession = Depends(get_rag_db),
) -> AsyncIterator[RagSearchService]:
    settings = get_settings()
    async with httpx.AsyncClient(trust_env=False) as client:
        embedder = EmbeddingServiceClient(
            settings.rag_embed_base_url,
            settings.rag_embed_model_id,
            client,
            timeout_seconds=settings.rag_embed_timeout_seconds,
        )
        reranker = (
            RerankServiceClient(
                settings.rag_rerank_base_url,
                settings.rag_rerank_model_id,
                client,
                timeout_seconds=settings.rag_rerank_timeout_seconds,
            )
            if settings.rag_rerank_enabled
            else None
        )
        repository = PostgresRagRepository(
            db,
            embedding_model_id=settings.rag_embed_model_id,
        )
        from app.rag.retrieval import RagSearchConfig

        yield RagSearchService(
            repository,
            embedder,
            reranker=reranker,
            config=RagSearchConfig(
                candidate_limit=settings.rag_candidate_limit,
                rrf_k=settings.rag_rrf_k,
                min_lexical_score=settings.rag_min_lexical_score,
                min_dense_similarity=settings.rag_min_dense_similarity,
                min_rerank_score=settings.rag_min_rerank_score,
            ),
        )


def _actor_id(scenario: ScenarioVersion, role_id: str | None) -> str | None:
    if role_id is None or not isinstance(scenario.definition, dict):
        return None
    role = next(
        (
            item
            for item in scenario.definition.get("roles", [])
            if isinstance(item, dict) and item.get("id") == role_id
        ),
        None,
    )
    return role.get("actor_id") if isinstance(role, dict) else None


@router.post("/sessions/{session_id}/evidence/search")
async def search_session_evidence(
    session_id: UUID,
    payload: EvidenceSearchRequest,
    token: str = Depends(require_token),
    db: AsyncSession = Depends(get_rag_db),
    service: RagSearchService = Depends(get_rag_search_service),
):
    session: GameSession = await owned_session(db, session_id, token)
    scenario = await db.get(ScenarioVersion, session.scenario_version_id)
    if scenario is None or scenario.definition is None or not scenario.evidence_policy:
        return {"status": RAG_NO_EVIDENCE, "passages": []}

    policy = (
        await db.execute(
            select(RagScenarioPolicy).where(
                RagScenarioPolicy.scenario_version_id == session.scenario_version_id
            )
        )
    ).scalar_one_or_none()
    if (
        policy is None
        or policy.scenario_slug != scenario.slug
        or policy.scenario_version != scenario.version
    ):
        return {"status": RAG_NO_EVIDENCE, "passages": []}

    phase_id = session.state.get("phase") if isinstance(session.state, dict) else None
    phase_dates = policy.phase_world_dates
    phase_date = phase_dates.get(phase_id) if isinstance(phase_dates, dict) else None
    try:
        world_date = date.fromisoformat(phase_date)
    except (TypeError, ValueError):
        return {"status": RAG_NO_EVIDENCE, "passages": []}

    postgame = phase_id == "reflection"
    context = EvidenceContext(
        scenario_version_id=scenario.id,
        scenario_slug=scenario.slug,
        scenario_version=scenario.version,
        actor_id=None if postgame else _actor_id(scenario, session.player_role_id),
        world_date=world_date,
        mode="postgame" if postgame else "gameplay",
        evidence_policy=scenario.evidence_policy,
    )
    result = await service.search(payload.query, context, top_k=payload.top_k)
    if result.status != "ok":
        return {"status": RAG_NO_EVIDENCE, "passages": []}
    passages = []
    for candidate in result.passages:
        if not is_eligible(
            candidate.passage,
            context,
            candidate.approval,
            context.evidence_policy,
        ):
            continue
        passages.append(
            {
                "scenario_slug": candidate.passage.scenario_slug,
                "scenario_version": candidate.passage.scenario_version,
                "passage_key": candidate.passage.passage_key,
                "source_identifier": candidate.passage.source_identifier,
                "source_title": candidate.passage.source_title,
                "source_url": candidate.passage.source_url,
                "source_anchor": candidate.passage.source_anchor,
                "passage_text": candidate.passage.text,
                "passage_sha256": candidate.passage.passage_sha256,
                "document_date": candidate.passage.document_date.isoformat(),
                "in_world_available_from": (
                    candidate.passage.in_world_available_from.isoformat()
                    if candidate.passage.in_world_available_from
                    else None
                ),
                "rrf_score": candidate.rrf_score,
                "dense_similarity": candidate.dense_similarity,
                "lexical_score": candidate.lexical_score,
                "rerank_score": candidate.rerank_score,
                "review": {
                    "status": candidate.approval.review_status,
                    "rights_status": candidate.approval.rights_status,
                    "revision": candidate.approval.revision,
                },
            }
        )
    if not passages:
        return {"status": RAG_NO_EVIDENCE, "passages": []}
    return {"status": "ok", "passages": passages}
