from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluation.answer_study import select_reviewed_answer_queries
from app.evaluation.report import write_report
from app.evaluation.retrieval import QueryTrace, RankedPassage
from app.evaluation.schemas import EvaluationQuery, QueryJudgment


def _trace(query_id: str) -> QueryTrace:
    ranking = [
        RankedPassage("p-a", 1, 0.9, "Passage A", "s-1", "A", {"bm25": 0.9}),
        RankedPassage("p-b", 2, 0.8, "Passage B", "s-1", "B", {"bm25": 0.8}),
        RankedPassage("p-c", 3, 0.7, "Passage C", "s-1", "C", {"bm25": 0.7}),
    ]
    return QueryTrace(
        query_id=query_id,
        split="pilot",
        language="en",
        topic="topic-a",
        kind="retrospective_qa",
        profile="bm25",
        model_identities={},
        timings_ms={"bm25": 1.0, "total_client_retrieval": 1.0},
        token_usage={},
        pre_rerank=[],
        ranking=ranking,
        retrieved_passages=[
            {
                "passage_id": item.passage_id,
                "source_id": item.source_id,
                "anchor": item.anchor,
                "text": item.text,
            }
            for item in ranking
        ],
    )


def test_report_allows_unreviewed_queries_when_trace_coverage_is_complete(
    tmp_path: Path,
) -> None:
    reviewed = QueryJudgment(
        query_id="q-1",
        relevance={"p-a": 3, "p-b": 1, "p-c": 0},
        review_state="reviewed",
        review_method="agent",
        reviewer_ids=["gpt-6-astra:web-review"],
    )

    summary = write_report(
        [_trace("q-1"), _trace("q-2")],
        {"q-1": reviewed},
        tmp_path,
        dataset_status="draft",
        source_review_state="draft",
        expected_query_ids={"q-1", "q-2"},
        target_queries=2,
        actual_queries=2,
        manifest_reviewed_queries=1,
        expected_profiles=("bm25",),
    )

    assert summary["result_completeness"] == "complete"
    assert summary["data_status"] == "ready"
    assert summary["judgment_review_status"] == "partial"
    assert summary["quality_evaluation_status"] == "available"
    assert summary["groups"][0]["judgment_complete_queries"] == 1
    assert summary["groups"][0]["recall_at_5"] == 1.0
    assert summary["groups"][0]["ndcg_at_10"] == 1.0
    assert summary["judgment_review_coverage"] == {
        "reviewed_queries": 1,
        "queries": 2,
        "fraction": 0.5,
        "human_reviewed_queries": 0,
        "agent_reviewed_queries": 1,
        "mixed_reviewed_queries": 0,
        "not_fully_judged_top10_traces": 1,
    }


def test_answer_study_samples_from_reviewed_subset_not_entire_heldout_set() -> None:
    queries = [
        EvaluationQuery(
            query_id=f"heldout-{index:02}",
            split="heldout",
            language="en" if index % 2 else "da",
            text=f"Historical question {index}?",
            topic=f"topic-{index % 5}",
            kind="retrospective_qa",
            answerable=True,
        )
        for index in range(30)
    ]
    judgments = {
        query.query_id: QueryJudgment(
            query_id=query.query_id,
            relevance={"p-oracle": 3},
            review_state="reviewed" if index < 25 else "draft",
            reviewer_ids=["reviewer-1"] if index < 25 else [],
        )
        for index, query in enumerate(queries)
    }

    selected = select_reviewed_answer_queries(
        queries,
        judgments,
        sample_size=25,
        seed=198,
    )

    assert len(selected) == 25
    assert {query.query_id for query in selected} == {
        f"heldout-{index:02}" for index in range(25)
    }


def test_answer_study_requires_only_the_requested_reviewed_sample() -> None:
    queries = [
        EvaluationQuery(
            query_id=f"heldout-{index:02}",
            split="heldout",
            language="en",
            text=f"Question {index}?",
            topic="topic-a",
            kind="retrospective_qa",
            answerable=True,
        )
        for index in range(24)
    ]
    judgments = {
        query.query_id: QueryJudgment(
            query_id=query.query_id,
            relevance={"p-oracle": 3},
            review_state="reviewed",
            reviewer_ids=["reviewer-1"],
        )
        for query in queries
    }

    with pytest.raises(ValueError, match="Only 24 held-out queries"):
        select_reviewed_answer_queries(queries, judgments, sample_size=25, seed=198)
