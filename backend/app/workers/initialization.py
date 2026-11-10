"""Real-provider worker for durable schema-v2 session initialization."""
from __future__ import annotations

import asyncio
import logging
from time import perf_counter

import httpx

from app.ai.llm import InferenceClient
from app.core.settings import Settings, get_settings
from app.db.connection import session_factory
from app.db.models import GameSession, ScenarioVersion
from app.game.initialization import (
    LEASE_RENEW_INTERVAL_SECONDS,
    InitializationClaim,
    claim_next,
    complete_claim,
    fail_claim,
    mark_repair_attempt,
    renew_claim,
)
from app.rag.clients import EmbeddingServiceClient, ModelServiceError, RerankServiceClient
from app.rag.postgres import PostgresRagRepository
from app.rag.retrieval import RagSearchConfig, RagSearchService
from app.runtime.context import ContextBuildFailure, build_session_context
from app.runtime.initializer import OpeningGeneration, OpeningRejected, generate_opening
from app.scenarios.schema_v2 import ScenarioDefinitionV2

logger = logging.getLogger(__name__)


class SessionPerCallRagRepository:
    """Open short DB sessions around retrieval queries, never around model calls."""

    def __init__(self, embedding_model_id: str):
        self.embedding_model_id = embedding_model_id

    async def _call(self, method: str, *args):
        async with session_factory() as db:
            repository = PostgresRagRepository(
                db, embedding_model_id=self.embedding_model_id
            )
            return await getattr(repository, method)(*args)

    async def has_eligible(self, context):
        return await self._call("has_eligible", context)

    async def lexical_search(self, context, query, limit):
        return await self._call("lexical_search", context, query, limit)

    async def dense_search(self, context, vector, limit):
        return await self._call("dense_search", context, vector, limit)

    async def revalidate(self, context, candidate_ids):
        return await self._call("revalidate", context, candidate_ids)


def build_retrieval_service(settings: Settings, http: httpx.AsyncClient) -> RagSearchService:
    repository = SessionPerCallRagRepository(settings.rag_embed_model_id)
    embedder = EmbeddingServiceClient(
        settings.rag_embed_base_url,
        settings.rag_embed_model_id,
        http,
        timeout_seconds=settings.rag_embed_timeout_seconds,
    )
    reranker = (
        RerankServiceClient(
            settings.rag_rerank_base_url,
            settings.rag_rerank_model_id,
            http,
            timeout_seconds=settings.rag_rerank_timeout_seconds,
        )
        if settings.rag_rerank_enabled and settings.rag_rerank_base_url
        else None
    )
    return RagSearchService(
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


async def _renew_while_running(
    claim: InitializationClaim,
    finished: asyncio.Event,
    owner: asyncio.Task | None,
) -> None:
    try:
        while not finished.is_set():
            try:
                await asyncio.wait_for(
                    finished.wait(), timeout=LEASE_RENEW_INTERVAL_SECONDS
                )
                return
            except TimeoutError:
                pass
            try:
                renewed = await renew_claim(claim)
            except Exception as exc:
                logger.warning("initialization lease renewal failed: %s", type(exc).__name__)
                continue
            if not renewed:
                # The parent task is cancelled only after the fenced heartbeat
                # proves that this worker no longer owns the claim.
                asyncio.get_running_loop().call_soon(_cancel_current_owner, owner)
                return
    except asyncio.CancelledError:
        raise


def _cancel_current_owner(owner: asyncio.Task | None) -> None:
    if owner is not None and not owner.done():
        owner.cancel()


async def process_initialization_claim(
    claim: InitializationClaim,
    *,
    generator,
    retrieval_service,
    renew_lease: bool = True,
) -> bool:
    """Build context and call inference without a DB transaction, then persist once."""
    stage = "context"
    started = perf_counter()
    finished = asyncio.Event()
    owner_task = asyncio.current_task()
    heartbeat = (
        asyncio.create_task(_renew_while_running(claim, finished, owner_task))
        if renew_lease
        else None
    )
    generation: OpeningGeneration | None = None
    failure: tuple[str, bool, str, list[dict]] | None = None
    try:
        async with session_factory() as db:
            session = await db.get(GameSession, claim.session_id)
            scenario = await db.get(ScenarioVersion, claim.scenario_version_id)
            if (
                session is None
                or scenario is None
                or scenario.schema_version != 2
                or session.runtime_version != "v2_ai"
                or session.scenario_version_id != scenario.id
                or session.player_role_id != claim.player_role_id
            ):
                raise ContextBuildFailure(
                    "validation_failure", "initialization claim does not match its pinned session"
                )
            definition = ScenarioDefinitionV2.model_validate(scenario.definition)
            selected_role = next(
                (role for role in definition.roles if role.id == claim.player_role_id), None
            )
            if selected_role is None:
                raise ContextBuildFailure(
                    "validation_failure", "initialization session role is missing from its release"
                )
            retrieval_query = " ".join(
                [
                    definition.metadata.title,
                    selected_role.title,
                    selected_role.introduction,
                    definition.historical_context.introduction,
                    *(objective.description for objective in definition.learning_objectives),
                ]
            )[:1800]
            context = await build_session_context(
                db,
                session,
                stage="initialization",
                query=retrieval_query,
                retrieval_service=retrieval_service,
                release_transaction_before_retrieval=True,
            )

        stage = "model"

        async def authorize_repair() -> bool:
            return await mark_repair_attempt(claim)

        generation = await generate_opening(
            generator,
            definition=definition,
            context=context,
            role_id=claim.player_role_id or "",
            session_id=claim.session_id,
            attempt_number=claim.attempts,
            repair_used=claim.repair_used,
            authorize_repair=authorize_repair,
        )
    except OpeningRejected as exc:
        failure = (
            exc.failure_code,
            False,
            "validation",
            exc.attempt_metadata,
        )
    except ContextBuildFailure as exc:
        if exc.status == "retrieval_failure":
            failure = ("RETRIEVAL_UNAVAILABLE", True, "context", [])
        else:
            failure = ("PINNED_CONTENT_INVALID", False, "context", [])
    except ModelServiceError:
        failure = ("RETRIEVAL_UNAVAILABLE", True, "context", [])
    except httpx.HTTPError:
        elapsed = max(0, int((perf_counter() - started) * 1000))
        failure = (
            "PROVIDER_UNAVAILABLE",
            True,
            "model" if stage == "model" else "context",
            [
                {
                    "attempt_number": claim.attempts,
                    "repair": claim.repair_used,
                    "outcome": "provider_error",
                    "error_code": "PROVIDER_UNAVAILABLE",
                    "duration_ms": elapsed,
                }
            ],
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("initialization worker failed with %s", type(exc).__name__)
        elapsed = max(0, int((perf_counter() - started) * 1000))
        failure = (
            "INITIALIZATION_FAILED",
            True,
            stage,
            [
                {
                    "attempt_number": claim.attempts,
                    "repair": claim.repair_used,
                    "outcome": "worker_error",
                    "error_code": "INITIALIZATION_FAILED",
                    "duration_ms": elapsed,
                }
            ],
        )
    finally:
        finished.set()
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    if failure is not None:
        code, retryable, failure_stage, attempt_metadata = failure
        return await fail_claim(
            claim,
            code,
            retryable=retryable,
            failure_stage=failure_stage,
            attempt_metadata=attempt_metadata,
        )
    if generation is None:
        return False
    committed = await complete_claim(
        claim,
        generation.opening,
        generation.initial_state,
        provenance=generation.provenance,
        attempt_metadata=generation.attempt_metadata,
    )
    if committed:
        logger.info(
            "initialized session %s with model %s in %sms",
            claim.session_id,
            generation.provenance.get("served_model")
            or generation.provenance.get("requested_model"),
            generation.provenance.get("latency_ms"),
        )
    return committed


async def process_next_initialization(
    *,
    generator,
    retrieval_service,
    renew_lease: bool = True,
) -> bool:
    claim = await claim_next()
    if claim is None:
        return False
    await process_initialization_claim(
        claim,
        generator=generator,
        retrieval_service=retrieval_service,
        renew_lease=renew_lease,
    )
    return True


async def run_initialization_worker() -> None:
    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.llm_completion_timeout_seconds), trust_env=False
    ) as http:
        generator = InferenceClient(
            settings.llm_base_url,
            settings.llm_model_alias,
            http,
            enable_thinking=settings.llm_enable_thinking,
        )
        retrieval_service = build_retrieval_service(settings, http)
        while True:
            claimed = await process_next_initialization(
                generator=generator,
                retrieval_service=retrieval_service,
            )
            if not claimed:
                await asyncio.sleep(settings.worker_poll_seconds)
