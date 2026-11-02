"""Hybrid search policy uses injectable stores and clients and abstains explicitly."""
import hashlib
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from app.rag.policy import EvidenceContext, EvidencePassage, PassageApproval
from app.rag.retrieval import (
    RAG_NO_EVIDENCE,
    RagSearchConfig,
    RagSearchService,
    SearchCandidate,
)

ROOT = Path(__file__).parents[2]
VERSION_ID = UUID("5bc6c117-a22e-44b6-bf87-9a282e9fe679")
POLICY = yaml.safe_load(
    (ROOT / "scenarios" / "versailles-1919" / "2.5_evidence.yaml").read_text(encoding="utf-8")
)


def context():
    return EvidenceContext(
        scenario_version_id=VERSION_ID,
        scenario_slug="versailles-1919",
        scenario_version="2.5",
        actor_id="britain",
        world_date=date(1919, 4, 22),
        mode="gameplay",
        evidence_policy=POLICY,
    )


def evidence(key):
    return EvidencePassage(
        id=UUID(f"00000000-0000-4000-8000-{1 if key == 'first' else 2:012d}"),
        scenario_version_id=VERSION_ID,
        scenario_slug="versailles-1919",
        scenario_version="2.5",
        passage_key=key,
        source_identifier="R04",
        source_title="Wilson address",
        source_url="https://history.state.gov/historicaldocuments/sample",
        source_anchor="Document 4, page 1",
        text=f"Evidence passage for {key}.",
        passage_sha256=hashlib.sha256(f"Evidence passage for {key}.".encode("utf-8")).hexdigest(),
        document_date=date(1918, 1, 8),
        in_world_available_from=date(1918, 1, 8),
        postgame_only=False,
        actor_ids=("britain",),
        visibility_scope="actor_limited",
    )


def candidate(key, *, lexical=None, dense=None):
    return SearchCandidate(
        passage=evidence(key),
        approval=PassageApproval("approved", "approved", 1),
        lexical_score=lexical,
        dense_similarity=dense,
    )


class FakeRepository:
    def __init__(self, *, eligible=True, lexical=None, dense=None, revalidated=None):
        self.eligible = eligible
        self.lexical = lexical or []
        self.dense = dense or []
        self.revalidated = revalidated
        self.calls = []

    async def has_eligible(self, _context):
        return self.eligible

    async def lexical_search(self, _context, _query, _limit):
        return self.lexical

    async def dense_search(self, _context, _vector, _limit):
        return self.dense

    async def revalidate(self, _context, candidate_ids):
        self.calls.append(tuple(candidate_ids))
        if self.revalidated is not None:
            return self.revalidated
        sources = {item.passage.id: item for item in self.lexical + self.dense}
        return [sources[key] for key in candidate_ids if key in sources]


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    async def embed_query(self, _query):
        self.calls += 1
        return [1.0] + [0.0] * 1023


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores

    async def rerank(self, _query, passages):
        assert len(passages) == len(self.scores)
        return self.scores


@pytest.mark.asyncio
async def test_no_eligible_approved_passages_returns_no_evidence_before_embedding():
    repository, embedder = FakeRepository(eligible=False), FakeEmbedder()
    service = RagSearchService(repository, embedder)

    result = await service.search("What happened?", context(), top_k=5)

    assert result.status == RAG_NO_EVIDENCE
    assert result.passages == []
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_hybrid_rrf_combines_rankings_and_final_revalidation():
    repository = FakeRepository(
        lexical=[candidate("first", lexical=0.2), candidate("second", lexical=0.1)],
        dense=[candidate("second", dense=0.91)],
    )
    service = RagSearchService(
        repository,
        FakeEmbedder(),
        config=RagSearchConfig(min_lexical_score=0.03, min_dense_similarity=0.6),
    )

    result = await service.search("What happened?", context(), top_k=2)

    assert result.status == "ok"
    assert [item.passage.passage_key for item in result.passages] == ["second", "first"]
    assert len(repository.calls) == 1


@pytest.mark.asyncio
async def test_optional_reranker_changes_order_using_injected_scores():
    repository = FakeRepository(
        lexical=[candidate("first", lexical=0.2), candidate("second", lexical=0.1)],
        dense=[candidate("first", dense=0.91)],
    )
    service = RagSearchService(
        repository,
        FakeEmbedder(),
        reranker=FakeReranker([0.1, 0.95]),
        config=RagSearchConfig(
            min_lexical_score=0.03, min_dense_similarity=0.6, min_rerank_score=0.2
        ),
    )

    result = await service.search("What happened?", context(), top_k=2)

    assert result.status == "ok"
    assert [item.passage.passage_key for item in result.passages] == ["second", "first"]
    assert result.passages[0].rerank_score == 0.95


@pytest.mark.asyncio
async def test_low_relevance_scores_and_revoked_final_decisions_return_no_evidence():
    low_score_repository = FakeRepository(
        lexical=[candidate("first", lexical=0.001)],
        dense=[candidate("first", dense=0.2)],
    )
    config = RagSearchConfig(min_lexical_score=0.03, min_dense_similarity=0.6)
    result = await RagSearchService(low_score_repository, FakeEmbedder(), config=config).search(
        "unsupported claim", context(), top_k=5
    )
    assert result.status == RAG_NO_EVIDENCE

    revoked = candidate("first", lexical=0.2)
    revoked = SearchCandidate(
        passage=revoked.passage,
        approval=PassageApproval("revoked", "approved", 2),
        lexical_score=revoked.lexical_score,
    )
    repository = FakeRepository(lexical=[revoked])
    result = await RagSearchService(repository, FakeEmbedder(), config=config).search(
        "supported query", context(), top_k=5
    )
    assert result.status == RAG_NO_EVIDENCE


@pytest.mark.asyncio
async def test_retrieval_discards_candidates_removed_by_post_rerank_revalidation():
    repository = FakeRepository(
        lexical=[candidate("first", lexical=0.2)],
        revalidated=[],
    )
    service = RagSearchService(repository, FakeEmbedder())

    result = await service.search("What happened?", context(), top_k=5)

    assert result.status == RAG_NO_EVIDENCE
    assert result.passages == []
