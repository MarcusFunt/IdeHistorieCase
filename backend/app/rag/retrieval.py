"""Scenario-filtered hybrid retrieval with explicit abstention and final recheck."""
from dataclasses import dataclass, replace
from math import isfinite
from typing import Protocol

from app.rag.policy import EvidenceContext, EvidencePassage, PassageApproval, is_eligible
from app.rag.ports import DocumentEmbedder, PassageReranker

RAG_NO_EVIDENCE = "RAG_NO_EVIDENCE"
MAX_QUERY_CHARS = 2000
MAX_TOP_K = 10


@dataclass(frozen=True)
class SearchCandidate:
    passage: EvidencePassage
    approval: PassageApproval
    lexical_score: float | None = None
    dense_similarity: float | None = None
    rerank_score: float | None = None
    rrf_score: float = 0.0


@dataclass(frozen=True)
class RagSearchResult:
    status: str
    passages: list[SearchCandidate]


@dataclass(frozen=True)
class RagSearchConfig:
    candidate_limit: int = 20
    rrf_k: int = 60
    min_lexical_score: float = 0.03
    min_dense_similarity: float = 0.60
    min_rerank_score: float = 0.20

    def __post_init__(self):
        if not 2 <= self.candidate_limit <= 100:
            raise ValueError("candidate_limit must be between 2 and 100")
        if not 1 <= self.rrf_k <= 1000:
            raise ValueError("rrf_k must be between 1 and 1000")
        if not 0.0 <= self.min_lexical_score <= 1.0:
            raise ValueError("min_lexical_score must be between 0 and 1")
        if not -1.0 <= self.min_dense_similarity <= 1.0:
            raise ValueError("min_dense_similarity must be between -1 and 1")
        if not 0.0 <= self.min_rerank_score <= 1.0:
            raise ValueError("min_rerank_score must be between 0 and 1")


class RetrievalRepository(Protocol):
    async def has_eligible(self, context: EvidenceContext) -> bool: ...

    async def lexical_search(
        self, context: EvidenceContext, query: str, limit: int
    ) -> list[SearchCandidate]: ...

    async def dense_search(
        self, context: EvidenceContext, vector: list[float], limit: int
    ) -> list[SearchCandidate]: ...

    async def revalidate(
        self, context: EvidenceContext, candidate_ids: list
    ) -> list[SearchCandidate]: ...


def reciprocal_rank_fusion(
    lexical: list[SearchCandidate],
    dense: list[SearchCandidate],
    *,
    k: int = 60,
) -> list[SearchCandidate]:
    """Fuse candidate ranks while retaining each backend's score for gating."""
    by_id: dict = {}
    for ranked_list, kind in ((lexical, "lexical"), (dense, "dense")):
        for rank, candidate in enumerate(ranked_list, start=1):
            current = by_id.get(candidate.passage.id)
            if current is None:
                current = SearchCandidate(candidate.passage, candidate.approval)
            current = replace(
                current,
                lexical_score=(
                    candidate.lexical_score if kind == "lexical" else current.lexical_score
                ),
                dense_similarity=(
                    candidate.dense_similarity if kind == "dense" else current.dense_similarity
                ),
                rrf_score=current.rrf_score + 1.0 / (k + rank),
            )
            by_id[candidate.passage.id] = current
    return sorted(
        by_id.values(),
        key=lambda item: (-item.rrf_score, str(item.passage.id)),
    )


class RagSearchService:
    def __init__(
        self,
        repository: RetrievalRepository,
        embedder: DocumentEmbedder,
        *,
        reranker: PassageReranker | None = None,
        config: RagSearchConfig | None = None,
    ):
        self.repository = repository
        self.embedder = embedder
        self.reranker = reranker
        self.config = config or RagSearchConfig()

    async def search(
        self,
        query: str,
        context: EvidenceContext,
        *,
        top_k: int = 5,
    ) -> RagSearchResult:
        query = query.strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must contain 1 to {MAX_QUERY_CHARS} characters")
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")

        # Avoid sending a user query to an embedding service if this pinned session
        # has no currently approved evidence available to its actor and date.
        if not await self.repository.has_eligible(context):
            return RagSearchResult(RAG_NO_EVIDENCE, [])

        vector = await self.embedder.embed_query(query)
        lexical = await self.repository.lexical_search(
            context, query, self.config.candidate_limit
        )
        dense = await self.repository.dense_search(
            context, vector, self.config.candidate_limit
        )
        lexical = [
            candidate
            for candidate in lexical
            if is_eligible(candidate.passage, context, candidate.approval, context.evidence_policy)
            and self._valid_score(candidate.lexical_score, minimum=0.0, maximum=None)
        ]
        dense = [
            candidate
            for candidate in dense
            if is_eligible(candidate.passage, context, candidate.approval, context.evidence_policy)
            and self._valid_score(candidate.dense_similarity, minimum=-1.0, maximum=1.0)
        ]
        fused = reciprocal_rank_fusion(lexical, dense, k=self.config.rrf_k)
        fused = fused[: self.config.candidate_limit]
        if not fused:
            return RagSearchResult(RAG_NO_EVIDENCE, [])

        if self.reranker is not None:
            rerank_scores = await self.reranker.rerank(
                query, [candidate.passage.text for candidate in fused]
            )
            if len(rerank_scores) != len(fused) or any(
                not self._valid_score(score, minimum=0.0, maximum=1.0)
                for score in rerank_scores
            ):
                raise ValueError("reranker returned malformed or non-normalized scores")
            fused = [
                replace(candidate, rerank_score=score)
                for candidate, score in zip(fused, rerank_scores, strict=True)
            ]
            fused.sort(
                key=lambda item: (
                    -item.rerank_score,
                    -item.rrf_score,
                    str(item.passage.id),
                )
            )

        relevant = [candidate for candidate in fused if self._relevant(candidate)]
        relevant = relevant[:top_k]
        if not relevant:
            return RagSearchResult(RAG_NO_EVIDENCE, [])

        latest = await self.repository.revalidate(
            context, [candidate.passage.id for candidate in relevant]
        )
        latest_by_id = {candidate.passage.id: candidate for candidate in latest}
        visible = []
        for ranked in relevant:
            current = latest_by_id.get(ranked.passage.id)
            if current is None or not is_eligible(
                current.passage, context, current.approval, context.evidence_policy
            ):
                continue
            visible.append(
                replace(
                    ranked,
                    passage=current.passage,
                    approval=current.approval,
                )
            )
        if not visible:
            return RagSearchResult(RAG_NO_EVIDENCE, [])
        return RagSearchResult("ok", visible)

    def _relevant(self, candidate: SearchCandidate) -> bool:
        if (
            candidate.lexical_score is not None
            and candidate.lexical_score >= self.config.min_lexical_score
        ):
            return True
        if (
            candidate.dense_similarity is not None
            and candidate.dense_similarity >= self.config.min_dense_similarity
        ):
            return True
        return (
            candidate.rerank_score is not None
            and candidate.rerank_score >= self.config.min_rerank_score
        )

    @staticmethod
    def _valid_score(value: float | None, *, minimum: float, maximum: float | None) -> bool:
        return (
            value is not None
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and isfinite(value)
            and value >= minimum
            and (maximum is None or value <= maximum)
        )
