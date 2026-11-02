from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from statistics import mean
from typing import Any

from .metrics import summarize
from .retrieval import PROFILES, QueryTrace
from .schemas import QueryJudgment


def write_report(
    traces: list[QueryTrace],
    judgments: dict[str, QueryJudgment],
    output_dir: Path,
    *,
    dataset_status: str = "draft",
    validation_warnings: list[str] | None = None,
    resource_path: Path | None = None,
    reported_dataset_hashes: dict[str, str] | None = None,
    source_review_state: str = "draft",
    source_review_method: str | None = None,
    source_reviewer_ids: list[str] | None = None,
    expected_query_ids: set[str] | None = None,
    target_queries: int = 0,
    actual_queries: int = 0,
    manifest_reviewed_queries: int = 0,
    expected_profiles: tuple[str, ...] = PROFILES,
) -> dict[str, Any]:
    summary = summarize(traces, judgments)
    trace_query_ids = {trace.query_id for trace in traces}
    if expected_query_ids is None:
        expected_query_ids = trace_query_ids
    reviewed_queries = {
        query_id
        for query_id in expected_query_ids
        if query_id in judgments
        and judgments[query_id].review_state in {"reviewed", "adjudicated"}
    }
    review_methods = {
        query_id: (
            judgments[query_id].review_method or "human"
        )
        for query_id in reviewed_queries
    }
    review_method_counts = {
        method: sum(value == method for value in review_methods.values())
        for method in ("human", "agent", "mixed")
    }
    traces_by_query: dict[str, dict[str, QueryTrace]] = {}
    duplicate_trace_count = 0
    for trace in traces:
        by_profile = traces_by_query.setdefault(trace.query_id, {})
        if trace.profile in by_profile:
            duplicate_trace_count += 1
        by_profile[trace.profile] = trace
    missing_queries = sorted(expected_query_ids - trace_query_ids)
    unexpected_queries = sorted(trace_query_ids - expected_query_ids)
    missing_profiles = {
        query_id: sorted(set(expected_profiles) - set(traces_by_query.get(query_id, {})))
        for query_id in expected_query_ids
        if set(expected_profiles) - set(traces_by_query.get(query_id, {}))
    }
    unexpected_profiles = {
        query_id: sorted(set(by_profile) - set(expected_profiles))
        for query_id, by_profile in traces_by_query.items()
        if set(by_profile) - set(expected_profiles)
    }
    unjudged_top10 = [
        f"{trace.query_id}/{trace.profile}"
        for trace in traces
        if (
            trace.query_id not in judgments
            or judgments[trace.query_id].review_state not in {"reviewed", "adjudicated"}
            or any(
                item.passage_id not in judgments[trace.query_id].relevance
                for item in trace.ranking[:10]
            )
        )
    ]
    query_target_met = (
        target_queries > 0
        and actual_queries >= target_queries
        and len(expected_query_ids) >= target_queries
    )
    result_complete = (
        not missing_queries
        and not unexpected_queries
        and not missing_profiles
        and not unexpected_profiles
        and duplicate_trace_count == 0
    )
    review_status = (
        "none"
        if not reviewed_queries
        else "complete"
        if len(reviewed_queries) == len(expected_query_ids)
        else "partial"
    )
    quality_metrics_available = any(
        row["judgment_complete_queries"] > 0 for row in summary["groups"]
    )
    summary["judgment_review_status"] = review_status
    summary["result_completeness"] = "complete" if result_complete else "incomplete"
    summary["data_status"] = "ready" if result_complete else "incomplete"
    summary["quality_evaluation_status"] = (
        "available" if quality_metrics_available else "awaiting_review"
    )
    summary["dataset_status"] = dataset_status
    summary["source_review_state"] = source_review_state
    summary["source_review_method"] = source_review_method
    summary["source_reviewer_ids"] = source_reviewer_ids or []
    summary["split_query_counts"] = {
        "target": target_queries,
        "actual": actual_queries,
        "traced": len(trace_query_ids),
        "expected_profiles": list(expected_profiles),
        "traces": len(traces),
        "reviewed_judgments": len(reviewed_queries),
        "review_methods": review_method_counts,
        "manifest_reviewed_queries": manifest_reviewed_queries,
        "missing_queries": missing_queries,
        "unexpected_queries": unexpected_queries,
        "missing_profile_queries": missing_profiles,
        "unexpected_profile_queries": unexpected_profiles,
        "duplicate_trace_count": duplicate_trace_count,
        "unjudged_top10_trace_count": len(unjudged_top10),
    }
    summary["judgment_review_coverage"] = {
        "reviewed_queries": len(reviewed_queries),
        "queries": len(expected_query_ids),
        "fraction": len(reviewed_queries) / len(expected_query_ids)
        if expected_query_ids
        else 0.0,
        "human_reviewed_queries": review_method_counts["human"],
        "agent_reviewed_queries": review_method_counts["agent"],
        "mixed_reviewed_queries": review_method_counts["mixed"],
        "not_fully_judged_top10_traces": len(unjudged_top10),
    }
    warnings = list(validation_warnings or [])
    if not query_target_met:
        warnings.append(f"Split target incomplete: {actual_queries}/{target_queries} queries")
    if source_review_state != "reviewed":
        warnings.append(
            f"Source review state is {source_review_state}; it does not block retrieval results"
        )
    if dataset_status != "reviewed":
        warnings.append(
            f"Dataset manifest status is {dataset_status}; review status is informational"
        )
    if missing_queries:
        warnings.append(f"Retrieval traces are missing {len(missing_queries)} expected queries")
    if unexpected_queries:
        warnings.append(f"Retrieval traces contain {len(unexpected_queries)} unexpected queries")
    if missing_profiles:
        warnings.append(f"Retrieval traces are missing profiles for {len(missing_profiles)} queries")
    if unexpected_profiles:
        warnings.append(f"Retrieval traces contain unexpected profiles for {len(unexpected_profiles)} queries")
    if duplicate_trace_count:
        warnings.append(f"Retrieval traces contain {duplicate_trace_count} duplicate query/profile records")
    if unjudged_top10:
        warnings.append(
            f"Quality metrics use fully judged reviewed cases; "
            f"{len(unjudged_top10)}/{len(traces)} profile traces are outside that metric sample"
        )
    if manifest_reviewed_queries < len(expected_query_ids):
        warnings.append(
            f"Reviewed query coverage: "
            f"{manifest_reviewed_queries}/{len(expected_query_ids)}"
        )
    summary["reviewed_query_count"] = len(reviewed_queries)
    summary["judgment_query_count"] = len(expected_query_ids)
    summary["validation_warnings"] = list(dict.fromkeys(warnings))
    summary["dataset_hashes_used_for_metrics"] = reported_dataset_hashes or {}
    summary["docker_resources"] = _read_resource_summary(resource_path)
    run_path = output_dir / "run.json"
    run_metadata = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
    summary["run_context"] = {
        key: run_metadata.get(key)
        for key in (
            "run_id",
            "split",
            "profiles",
            "execution_location",
            "cache_state",
            "warmup",
            "worker_start_metadata",
            "dataset_status",
            "source_review_state",
            "dataset_counts",
            "model_pins",
            "model_identities",
            "hardware",
            "dataset_hashes",
        )
        if key in run_metadata
    }
    worker_start_path = (
        resource_path.parent / "workers-at-start.json"
        if resource_path is not None
        else output_dir / "workers-at-start.json"
    )
    worker_sampling = (
        json.loads(worker_start_path.read_text(encoding="utf-8"))
        if worker_start_path.is_file()
        else {"local_docker_sampling": "unavailable"}
    )
    summary["worker_sampling"] = (
        worker_sampling if isinstance(worker_sampling, dict)
        else {"local_docker_sampling": "unavailable", "metadata_error": "unexpected format"}
    )
    summary["query_count"] = len({trace.query_id for trace in traces})
    summary["trace_count"] = len(traces)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    rows = summary["groups"]
    lines = [
        "# Historical retrieval evaluation",
        "",
        f"Retrieval readiness: {summary['data_status']}; trace completeness: {summary['result_completeness']}",
        f"Quality evaluation: {summary['quality_evaluation_status']}; reviewed sample: "
        f"{len(reviewed_queries)}/{len(expected_query_ids)} ({review_status})",
        f"Queries: {summary['query_count']}; profile traces: {summary['trace_count']}; "
        f"judged traces: {sum(row['judgment_complete_queries'] for row in summary['groups'])}",
        f"Split target: {actual_queries}/{target_queries}; source review (informational): "
        f"{source_review_state}/{source_review_method or 'method not recorded'}",
        "",
        "## Validation and completeness warnings",
        "",
    ]
    if summary["validation_warnings"]:
        lines.extend(f"- {warning}" for warning in summary["validation_warnings"])
    else:
        lines.append("- None")
    lines.extend([
        "",
        "| Split | Lang | Topic | Kind | Profile | Judged | Recall@5 | MRR@10 | nDCG@10 | Coverage@5 | Coverage@10 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        lines.append(
            "| {split} | {language} | {topic} | {kind} | {profile} | {judgment_complete_queries}/{queries} | "
            "{recall_at_5} | {mrr_at_10} | {ndcg_at_10} | {evidence_coverage_at_5} | "
            "{evidence_coverage_at_10} |".format(
                **{
                    **row,
                    "recall_at_5": _display(row["recall_at_5"]),
                    "mrr_at_10": _display(row["mrr_at_10"]),
                    "ndcg_at_10": _display(row["ndcg_at_10"]),
                    "evidence_coverage_at_5": _display(row["evidence_coverage_at_5"]),
                    "evidence_coverage_at_10": _display(row["evidence_coverage_at_10"]),
                }
            )
        )
    lines.extend(
        [
            "",
            "Only fully judged reviewed cases contribute to graded metrics. "
            "Review coverage is reported as a sample size; it does not block retrieval-run completeness.",
            "",
            "## Paired hybrid reranking deltas",
            "",
            "| Metric | Judged pairs | Mean paired Δ | 95% bootstrap CI |",
            "|---|---:|---:|---:|",
        ]
    )
    for metric_name, row in summary["rerank_delta_summary"].items():
        ci = row["ci95"]
        ci_text = "pending" if ci is None else f"[{ci['low']:.3f}, {ci['high']:.3f}]"
        lines.append(
            f"| {metric_name} | {row['queries']} | {_display(row['mean'])} | {ci_text} |"
        )
    lines.extend(
        [
            "",
            "Per-query paired deltas",
            "",
            "| Query | Judged | Recall@5 Δ | MRR@10 Δ | nDCG@10 Δ |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in summary["rerank_deltas"]:
        lines.append(
            f"| {row['query_id']} | {row['judgment_complete']} | "
            f"{_display(row['recall_at_5_delta'])} | {_display(row['mrr_at_10_delta'])} | "
            f"{_display(row['ndcg_at_10_delta'])} |"
        )
    lines.extend(
        [
            "",
            "## Retrieval latency (client wall time, ms)",
            "",
            "| Split / Profile / Stage | Samples | p50 | p95 |",
            "|---|---:|---:|---:|",
        ]
    )
    for key, values in summary["latency_ms_by_split_profile"].items():
        lines.append(
            f"| {key} | {values['samples']} | {_display(values['p50'])} | {_display(values['p95'])} |"
        )
    lines.extend(
        [
            "",
            "## Corpus index construction",
            "",
            "| Split / Profile | First request | Remaining requests | Total index embedding | Amortized / query |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for key, timings in summary["index_build_ms_by_split_profile"].items():
        lines.append(
            f"| {key} | {_display(timings.get('first_request'))} | "
            f"{_display(timings.get('remaining_requests'))} | {_display(timings.get('total'))} | "
            f"{_display(timings.get('per_query_amortized'))} |"
        )
    lines.extend(
        [
            "",
            "## Existing-worker Docker resources",
            "",
            f"Sampling status: {summary['docker_resources']['status']}",
            f"Worker sampling context: {summary['worker_sampling'].get('local_docker_sampling', 'unavailable')}",
            "",
            "| Worker / phase | Samples | CPU p50% | CPU p95% | Memory mean | Memory peak |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for key, row in summary["docker_resources"].get("groups", {}).items():
        lines.append(
            f"| {key} | {row['samples']} | {_display(row['cpu_percent_p50'])} | "
            f"{_display(row['cpu_percent_p95'])} | {_bytes_display(row['memory_mean_bytes'])} | "
            f"{_bytes_display(row['memory_peak_bytes'])} |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def _display(value: float | None) -> str:
    return "pending" if value is None else f"{value:.3f}"


def _bytes_display(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value / (1024 * 1024):.1f} MiB"


def _read_resource_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "unavailable", "sample_count": 0, "groups": {}}
    if not path.is_file():
        raise ValueError(f"Docker resource sample file not found: {path}")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    sample_count = 0
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid Docker resource sample at {path.name}:{line_number}") from error
            if not isinstance(row, dict) or not isinstance(row.get("service"), str):
                raise ValueError(f"Malformed Docker resource sample at {path.name}:{line_number}")
            phase = row.get("phase")
            if not isinstance(phase, str):
                raise ValueError(f"Docker resource sample has no phase at {path.name}:{line_number}")
            grouped.setdefault((row["service"], phase), []).append(row)
            sample_count += 1
    groups: dict[str, Any] = {}
    for (service, phase), rows in sorted(grouped.items()):
        cpu = [
            float(row["cpu_percent"])
            for row in rows
            if isinstance(row.get("cpu_percent"), (float, int))
        ]
        memory = [
            int(row["memory_usage_bytes"])
            for row in rows
            if isinstance(row.get("memory_usage_bytes"), int)
            and not isinstance(row.get("memory_usage_bytes"), bool)
        ]
        groups[f"{service}/{phase}"] = {
            "samples": len(rows),
            "cpu_percent_p50": _percentile(cpu, 0.50),
            "cpu_percent_p95": _percentile(cpu, 0.95),
            "cpu_percent_mean": mean(cpu) if cpu else None,
            "memory_mean_bytes": mean(memory) if memory else None,
            "memory_peak_bytes": max(memory) if memory else None,
            "host": sorted({str(row.get("host", "unknown")) for row in rows}),
        }
    return {
        "status": "available" if sample_count else "unavailable",
        "sample_count": sample_count,
        "groups": groups,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def read_traces(path: Path) -> list[QueryTrace]:
    from .retrieval import RankedPassage

    traces: list[QueryTrace] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            fields_by_name = {field.name for field in fields(QueryTrace)}
            trace_fields = {key: value for key, value in record.items() if key in fields_by_name}
            trace_fields["ranking"] = [
                RankedPassage(**item) for item in record.get("ranking", [])
            ]
            trace_fields["pre_rerank"] = [
                RankedPassage(**item) for item in record.get("pre_rerank", [])
            ]
            traces.append(QueryTrace(**trace_fields))
    return traces
