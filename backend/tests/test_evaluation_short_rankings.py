from __future__ import annotations

from pathlib import Path

from app.evaluation.metrics import query_metrics
from app.evaluation.report import write_report
from app.evaluation.retrieval import QueryTrace, RankedPassage
from app.evaluation.schemas import EvidenceGroup, QueryJudgment


def _short_ranking() -> list[RankedPassage]:
    return [
        RankedPassage("p-a", 1, 0.9, "Passage A", "s-1", "A", {"bm25": 0.9}),
        RankedPassage("p-b", 2, 0.8, "Passage B", "s-1", "B", {"bm25": 0.8}),
        RankedPassage("p-c", 3, 0.7, "Passage C", "s-1", "C", {"bm25": 0.7}),
    ]


def _reviewed_judgment() -> QueryJudgment:
    return QueryJudgment(
        query_id="q-1",
        relevance={"p-a": 3, "p-b": 1, "p-c": 0},
        required_evidence_groups=[
            EvidenceGroup(
                group_id="fact-a",
                description="The passage states the required fact.",
                supporting_passage_ids=["p-a"],
            )
        ],
        review_state="reviewed",
        reviewer_ids=["reviewer-1"],
    )


def test_metrics_accept_complete_temporally_filtered_ranking_shorter_than_ten() -> None:
    metrics = query_metrics(
        [item.__dict__ for item in _short_ranking()],
        _reviewed_judgment(),
    )

    assert metrics == {
        "judgment_complete": True,
        "recall_at_5": 1.0,
        "mrr_at_10": 1.0,
        "ndcg_at_10": 1.0,
        "evidence_coverage_at_5": 1.0,
        "evidence_coverage_at_10": 1.0,
    }


def test_report_accepts_reviewed_run_when_all_available_results_are_judged(
    tmp_path: Path,
) -> None:
    ranking = _short_ranking()
    trace = QueryTrace(
        query_id="q-1",
        split="pilot",
        language="en",
        topic="topic-a",
        kind="simulation",
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

    summary = write_report(
        [trace],
        {"q-1": _reviewed_judgment()},
        tmp_path,
        dataset_status="reviewed",
        source_review_state="reviewed",
        expected_query_ids={"q-1"},
        target_queries=1,
        actual_queries=1,
        manifest_reviewed_queries=1,
        expected_profiles=("bm25",),
    )

    assert summary["result_completeness"] == "complete"
    assert summary["judgment_review_status"] == "complete"
    assert summary["data_status"] == "ready"
    assert summary["split_query_counts"]["unjudged_top10_trace_count"] == 0
