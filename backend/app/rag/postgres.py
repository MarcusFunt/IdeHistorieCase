"""PostgreSQL lexical and pgvector candidate store with eligibility prefilters."""
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    HistoricalPassage,
    HistoricalPassageEmbedding,
    HistoricalPassageReview,
    RagScenarioPolicy,
)
from app.rag.policy import (
    EvidenceContext,
    EvidencePassage,
    PassageApproval,
    is_eligible,
)
from app.rag.retrieval import SearchCandidate
from app.scenarios.evidence import available_source_ids


class PostgresRagRepository:
    """Search only current approved records pinned to one scenario version."""

    def __init__(self, db: AsyncSession, *, embedding_model_id: str):
        self.db = db
        self.embedding_model_id = embedding_model_id

    def _base_statement(self, context: EvidenceContext, *, require_embedding: bool = False):
        permitted_sources = available_source_ids(
            context.evidence_policy,
            context.world_date,
            actor_id=context.actor_id,
            mode=context.mode,
        )
        if not permitted_sources:
            return None

        source_entries = {
            entry["source_id"]: entry
            for entry in context.evidence_policy.get("source_access", [])
            if isinstance(entry, dict)
            and isinstance(entry.get("source_id"), str)
            and entry["source_id"] in permitted_sources
        }
        postgame_sources: list[str] = []
        dated_sources: list[tuple[str, date]] = []
        confidential_sources: list[str] = []
        ordinary_sources: list[str] = []
        for source_id, entry in source_entries.items():
            availability = entry.get("in_world_available_from")
            if availability == "postgame":
                postgame_sources.append(source_id)
            elif isinstance(availability, str):
                try:
                    dated_sources.append((source_id, date.fromisoformat(availability)))
                except ValueError:
                    continue
            scope = entry.get("scope")
            if isinstance(scope, str) and scope.startswith(("confidential", "restricted")):
                confidential_sources.append(source_id)
            else:
                ordinary_sources.append(source_id)

        latest = (
            select(
                HistoricalPassageReview.passage_id.label("passage_id"),
                func.max(HistoricalPassageReview.revision).label("revision"),
            )
            .group_by(HistoricalPassageReview.passage_id)
            .subquery()
        )
        statement = (
            select(HistoricalPassage, HistoricalPassageReview)
            .join(
                RagScenarioPolicy,
                RagScenarioPolicy.scenario_version_id == HistoricalPassage.scenario_version_id,
            )
            .join(
                latest,
                latest.c.passage_id == HistoricalPassage.id,
            )
            .join(
                HistoricalPassageReview,
                (HistoricalPassageReview.passage_id == latest.c.passage_id)
                & (HistoricalPassageReview.revision == latest.c.revision),
            )
            .where(
                HistoricalPassage.scenario_version_id == context.scenario_version_id,
                HistoricalPassage.scenario_slug == context.scenario_slug,
                HistoricalPassage.scenario_version == context.scenario_version,
                RagScenarioPolicy.scenario_slug == context.scenario_slug,
                RagScenarioPolicy.scenario_version == context.scenario_version,
                HistoricalPassage.corpus_sha256 == RagScenarioPolicy.corpus_sha256,
                HistoricalPassage.source_identifier.in_(permitted_sources),
                HistoricalPassageReview.review_status == "approved",
                HistoricalPassageReview.rights_status == "approved",
                or_(
                    and_(
                        HistoricalPassage.source_identifier.in_(confidential_sources),
                        HistoricalPassage.visibility_scope == "confidential",
                    ),
                    and_(
                        HistoricalPassage.source_identifier.in_(ordinary_sources),
                        HistoricalPassage.visibility_scope != "confidential",
                    ),
                ),
                or_(
                    and_(
                        HistoricalPassage.source_identifier.in_(postgame_sources),
                        HistoricalPassage.postgame_only.is_(True),
                        HistoricalPassage.in_world_available_from.is_(None),
                        HistoricalPassage.visibility_scope == "postgame",
                    ),
                    *[
                        and_(
                            HistoricalPassage.source_identifier == source_id,
                            HistoricalPassage.postgame_only.is_(False),
                            HistoricalPassage.in_world_available_from >= available_from,
                            HistoricalPassage.visibility_scope != "postgame",
                        )
                        for source_id, available_from in dated_sources
                    ],
                ),
            )
        )
        if context.actor_id is not None:
            statement = statement.where(HistoricalPassage.actor_ids.contains([context.actor_id]))
        else:
            # Actorless results are allowed only for an explicitly public postgame view.
            statement = statement.where(
                HistoricalPassage.postgame_only.is_(True),
                HistoricalPassage.visibility_scope == "postgame",
                HistoricalPassage.actor_ids == [],
            )

        if context.mode == "gameplay":
            statement = statement.where(
                HistoricalPassage.postgame_only.is_(False),
                HistoricalPassage.document_date <= context.world_date,
                HistoricalPassage.in_world_available_from <= context.world_date,
            )
        if require_embedding:
            statement = statement.join(
                HistoricalPassageEmbedding,
                (HistoricalPassageEmbedding.passage_id == HistoricalPassage.id)
                & (HistoricalPassageEmbedding.passage_sha256 == HistoricalPassage.passage_sha256)
                & (HistoricalPassageEmbedding.model_id == self.embedding_model_id),
            )
        return statement

    @staticmethod
    def _passage(row: HistoricalPassage) -> EvidencePassage:
        return EvidencePassage(
            id=row.id,
            scenario_version_id=row.scenario_version_id,
            scenario_slug=row.scenario_slug,
            scenario_version=row.scenario_version,
            passage_key=row.passage_key,
            source_identifier=row.source_identifier,
            source_title=row.source_title,
            source_url=row.source_url,
            source_anchor=row.source_anchor,
            text=row.passage_text,
            passage_sha256=row.passage_sha256,
            document_date=row.document_date,
            in_world_available_from=row.in_world_available_from,
            postgame_only=row.postgame_only,
            actor_ids=tuple(row.actor_ids),
            visibility_scope=row.visibility_scope,
        )

    @classmethod
    def _candidate(
        cls,
        passage: HistoricalPassage,
        review: HistoricalPassageReview,
        *,
        lexical_score: float | None = None,
        dense_similarity: float | None = None,
    ) -> SearchCandidate:
        return SearchCandidate(
            passage=cls._passage(passage),
            approval=PassageApproval(
                review_status=review.review_status,
                rights_status=review.rights_status,
                revision=review.revision,
            ),
            lexical_score=float(lexical_score) if lexical_score is not None else None,
            dense_similarity=float(dense_similarity) if dense_similarity is not None else None,
        )

    @staticmethod
    def _eligible(candidate: SearchCandidate, context: EvidenceContext) -> bool:
        return is_eligible(
            candidate.passage,
            context,
            candidate.approval,
            context.evidence_policy,
        )

    async def has_eligible(self, context: EvidenceContext) -> bool:
        statement = self._base_statement(context)
        if statement is None:
            return False
        rows = (await self.db.execute(statement.limit(100))).all()
        return any(
            self._eligible(self._candidate(passage, review), context)
            for passage, review in rows
        )

    async def lexical_search(
        self,
        context: EvidenceContext,
        query: str,
        limit: int,
    ) -> list[SearchCandidate]:
        statement = self._base_statement(context)
        if statement is None:
            return []
        regconfig = literal_column("'simple'::regconfig")
        document_vector = func.to_tsvector(regconfig, HistoricalPassage.passage_text)
        query_vector = func.plainto_tsquery(regconfig, query)
        score = func.ts_rank_cd(document_vector, query_vector).label("score")
        rows = (
            await self.db.execute(
                statement.add_columns(score)
                .where(document_vector.op("@@")(query_vector))
                .order_by(score.desc(), HistoricalPassage.id)
                .limit(limit)
            )
        ).all()
        candidates = [
            self._candidate(passage, review, lexical_score=score_value)
            for passage, review, score_value in rows
        ]
        return [candidate for candidate in candidates if self._eligible(candidate, context)]

    async def dense_search(
        self,
        context: EvidenceContext,
        vector: list[float],
        limit: int,
    ) -> list[SearchCandidate]:
        statement = self._base_statement(context, require_embedding=True)
        if statement is None:
            return []
        distance = HistoricalPassageEmbedding.embedding.cosine_distance(vector)
        similarity = (1.0 - distance).label("similarity")
        rows = (
            await self.db.execute(
                statement.add_columns(similarity)
                .order_by(distance, HistoricalPassage.id)
                .limit(limit)
            )
        ).all()
        candidates = [
            self._candidate(passage, review, dense_similarity=similarity_value)
            for passage, review, similarity_value in rows
        ]
        return [candidate for candidate in candidates if self._eligible(candidate, context)]

    async def revalidate(
        self,
        context: EvidenceContext,
        candidate_ids: Sequence[UUID],
    ) -> list[SearchCandidate]:
        if not candidate_ids:
            return []
        statement = self._base_statement(context)
        if statement is None:
            return []
        rows = (
            await self.db.execute(
                statement.where(HistoricalPassage.id.in_(candidate_ids)).with_for_update(
                    read=True,
                    of=HistoricalPassage,
                )
            )
        ).all()
        candidates = [self._candidate(passage, review) for passage, review in rows]
        return [candidate for candidate in candidates if self._eligible(candidate, context)]

    async def phase_policy(self, scenario_version_id: UUID) -> RagScenarioPolicy | None:
        return (
            await self.db.execute(
                select(RagScenarioPolicy).where(
                    RagScenarioPolicy.scenario_version_id == scenario_version_id
                )
            )
        ).scalar_one_or_none()
