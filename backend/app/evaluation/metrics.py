from __future__ import annotations

import random
from collections import defaultdict
from statistics import mean
from typing import Any

from .retrieval import QueryTrace
from .schemas import QueryJudgment


def query_metrics(
    ranking: list[dict[str, Any]],
    judgment: QueryJudgment | None,
    *,
    k_values: tuple[int, ...] = (5, 10),
) -> dict[str, Any]:
    if judgment is None or judgment.review_state not in {"reviewed", "adjudicated"}:
        return {
            "judgment_complete": False,
            "recall_at_5": None,
            "mrr_at_10": None,
            "ndcg_at_10": None,
            "evidence_coverage_at_5": None,
            "evidence_coverage_at_10": None,
        }
    grades = judgment.relevance
    complete_at_10 = all(
        item["passage_id"] in grades for item in ranking[:10]
    )
    relevant = {passage_id for passage_id, grade in grades.items() if grade > 0}
    retrieved_ids = [item["passage_id"] for item in ranking]
    if not relevant:
        recall = None
        mrr = 0.0 if complete_at_10 else None
    else:
        recall = (
            len(relevant.intersection(retrieved_ids[:5])) / len(relevant)
            if complete_at_10
            else None
        )
        first = next((index + 1 for index, pid in enumerate(retrieved_ids[:10]) if pid in relevant), None)
        mrr = (1.0 / first if first else 0.0) if complete_at_10 else None

    dcg = 0.0
    for index, passage_id in enumerate(retrieved_ids[:10], start=1):
        grade = grades.get(passage_id)
        if grade is None:
            continue
        dcg += (2**grade - 1) / _discount(index)
    ideal_grades = sorted(grades.values(), reverse=True)[:10]
    ideal_dcg = sum((2**grade - 1) / _discount(rank) for rank, grade in enumerate(ideal_grades, 1))
    ndcg = (dcg / ideal_dcg if ideal_dcg else 0.0) if complete_at_10 else None

    result: dict[str, Any] = {
        "judgment_complete": complete_at_10,
        "recall_at_5": recall,
        "mrr_at_10": mrr,
        "ndcg_at_10": ndcg,
    }
    for k in k_values:
        groups = judgment.required_evidence_groups
        covered = sum(
            bool(set(group.supporting_passage_ids).intersection(retrieved_ids[:k]))
            for group in groups
        )
        result[f"evidence_coverage_at_{k}"] = (
            covered / len(groups) if complete_at_10 and groups else None
        )
    return result


def _discount(rank: int) -> float:
    import math

    return math.log2(rank + 1)


def paired_rerank_deltas(
    traces: list[QueryTrace],
    judgments: dict[str, QueryJudgment],
) -> list[dict[str, Any]]:
    by_query: dict[str, dict[str, QueryTrace]] = defaultdict(dict)
    for trace in traces:
        by_query[trace.query_id][trace.profile] = trace
    deltas: list[dict[str, Any]] = []
    for query_id, profiles in by_query.items():
        pre = profiles.get("hybrid")
        post = profiles.get("hybrid_rerank")
        if pre is None or post is None:
            continue
        judgment = judgments.get(query_id)
        before = query_metrics([item.__dict__ for item in pre.ranking], judgment)
        after = query_metrics([item.__dict__ for item in post.ranking], judgment)
        deltas.append(
            {
                "query_id": query_id,
                "judgment_complete": bool(before["judgment_complete"] and after["judgment_complete"]),
                "recall_at_5_delta": _delta(before["recall_at_5"], after["recall_at_5"]),
                "mrr_at_10_delta": _delta(before["mrr_at_10"], after["mrr_at_10"]),
                "ndcg_at_10_delta": _delta(before["ndcg_at_10"], after["ndcg_at_10"]),
                "evidence_coverage_at_5_delta": _delta(
                    before["evidence_coverage_at_5"], after["evidence_coverage_at_5"]
                ),
                "evidence_coverage_at_10_delta": _delta(
                    before["evidence_coverage_at_10"], after["evidence_coverage_at_10"]
                ),
                "rank_movement": _rank_movement(pre, post),
            }
        )
    return deltas


def summarize(
    traces: list[QueryTrace],
    judgments: dict[str, QueryJudgment],
    *,
    bootstrap_samples: int = 2000,
    seed: int = 198,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    judgmented_counts: dict[tuple[str, str], list[bool]] = defaultdict(list)
    latency_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    index_build_ms: dict[tuple[str, str], dict[str, float]] = {}
    for trace in traces:
        metric = query_metrics([item.__dict__ for item in trace.ranking], judgments.get(trace.query_id))
        key = (trace.split, trace.language, trace.topic, trace.kind, trace.profile)
        grouped[key].append(metric)
        judgmented_counts[(trace.split, trace.profile)].append(metric["judgment_complete"])
        for timing_name, elapsed_ms in trace.timings_ms.items():
            if timing_name.startswith("corpus_embedding_"):
                index_build_ms.setdefault((trace.split, trace.profile), {})[
                    timing_name.removeprefix("corpus_embedding_")
                ] = elapsed_ms
                continue
            latency_values[(trace.split, trace.profile, timing_name)].append(elapsed_ms)

    summaries: list[dict[str, Any]] = []
    for (split, language, topic, kind, profile), metrics in sorted(grouped.items()):
        row: dict[str, Any] = {
            "split": split,
            "language": language,
            "topic": topic,
            "kind": kind,
            "profile": profile,
            "queries": len(metrics),
            "judgment_complete_queries": sum(bool(item["judgment_complete"]) for item in metrics),
        }
        for metric_name in (
            "recall_at_5",
            "mrr_at_10",
            "ndcg_at_10",
            "evidence_coverage_at_5",
            "evidence_coverage_at_10",
        ):
            values = [item[metric_name] for item in metrics if item.get(metric_name) is not None]
            row[metric_name] = mean(values) if values else None
            row[f"{metric_name}_ci95"] = _bootstrap_ci(values, bootstrap_samples, seed)
        summaries.append(row)

    rerank_deltas = paired_rerank_deltas(traces, judgments)
    paired_metrics = (
        "recall_at_5_delta",
        "mrr_at_10_delta",
        "ndcg_at_10_delta",
        "evidence_coverage_at_5_delta",
        "evidence_coverage_at_10_delta",
    )
    rerank_delta_summary = {}
    for index, metric_name in enumerate(paired_metrics):
        values = [
            row[metric_name]
            for row in rerank_deltas
            if row["judgment_complete"] and row[metric_name] is not None
        ]
        rerank_delta_summary[metric_name] = {
            "queries": len(values),
            "mean": mean(values) if values else None,
            "ci95": _bootstrap_ci(values, bootstrap_samples, seed + index + 1),
        }

    return {
        "groups": summaries,
        "rerank_deltas": rerank_deltas,
        "rerank_delta_summary": rerank_delta_summary,
        "judgment_coverage_by_split_profile": {
            f"{split}/{profile}": {
                "judged": sum(flags),
                "queries": len(flags),
                "fraction": sum(flags) / len(flags) if flags else 0.0,
            }
            for (split, profile), flags in sorted(judgmented_counts.items())
        },
        "latency_ms_by_split_profile": {
            f"{split}/{profile}/{timing_name}": {
                "samples": len(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
            }
            for (split, profile, timing_name), values in sorted(latency_values.items())
        },
        "index_build_ms_by_split_profile": {
            f"{split}/{profile}": timings
            for (split, profile), timings in sorted(index_build_ms.items())
        },
    }


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return after - before


def _rank_movement(before: QueryTrace, after: QueryTrace) -> list[dict[str, int]]:
    before_rank = {item.passage_id: item.rank for item in before.ranking}
    after_rank = {item.passage_id: item.rank for item in after.ranking}
    ids = sorted(set(before_rank).union(after_rank))
    return [
        {
            "passage_id": passage_id,
            "before_rank": before_rank.get(passage_id, 0),
            "after_rank": after_rank.get(passage_id, 0),
        }
        for passage_id in ids
    ]


def _bootstrap_ci(values: list[float], samples: int, seed: int) -> dict[str, float] | None:
    if not values:
        return None
    if len(values) == 1:
        return {"low": values[0], "high": values[0]}
    rng = random.Random(seed)
    means = sorted(
        mean(rng.choices(values, k=len(values)))
        for _ in range(samples)
    )
    low = means[int(0.025 * (samples - 1))]
    high = means[int(0.975 * (samples - 1))]
    return {"low": low, "high": high}


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
