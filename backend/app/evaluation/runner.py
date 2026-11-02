from __future__ import annotations

import json
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .dataset import load_dataset, sha256_file, validate_dataset
from .model_clients import (
    GTE_CODE_REVISION,
    GTE_MODEL,
    GTE_REVISION,
    QWEN_MODEL,
    QWEN_QUERY_INSTRUCTION_VERSION,
    QWEN_REVISION,
    LiteEmbeddingClient,
    LiteRerankerClient,
)
from .report import write_report
from .retrieval import Profile, QueryTrace, run_retrieval


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_phase(path: Path, phase: str) -> None:
    event = {"timestamp_utc": _utc_now(), "phase": phase}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event) + "\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def _clean_endpoint(url: str) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}:{parsed.port}{parsed.path}"


def _dataset_hashes(root: Path, split: str) -> dict[str, str]:
    query_file = "pilot_queries.jsonl" if split == "pilot" else "heldout_queries.jsonl"
    names = (
        "manifest.json",
        "sources.jsonl",
        "passages.jsonl",
        query_file,
        "judgments.jsonl",
    )
    return {name: sha256_file(root / name) for name in names}


def _client_identity(client: Any) -> dict[str, Any] | None:
    identity = getattr(client, "identity", None)
    if identity is None:
        return None
    return {
        "alias": identity.alias,
        "task": identity.task,
        "revision": identity.revision,
        "capabilities": identity.capabilities,
        "limits": identity.limits,
        "preflight": {
            "health_ms": identity.health_ms,
            "ready_ms": identity.ready_ms,
            "models_ms": identity.models_ms,
            "completed_at_utc": identity.preflight_completed_at_utc,
        },
    }


def run_evaluation(
    *,
    dataset_root: Path,
    split: str,
    output_dir: Path,
    profiles: list[Profile],
    embed_url: str,
    rerank_url: str,
    execution_location: str,
    cache_state: str,
    warmup_count: int,
) -> dict[str, Any]:
    data_validation = validate_dataset(dataset_root)
    if data_validation.errors:
        raise ValueError(
            "Dataset validation failed before retrieval: " + "; ".join(data_validation.errors)
        )
    dataset = load_dataset(dataset_root, split)  # type: ignore[arg-type]
    if not dataset.queries:
        raise ValueError(f"The {split} split has no queries; populate it before retrieval.")
    wants_dense = any(profile in {"dense", "hybrid", "hybrid_rerank"} for profile in profiles)
    wants_rerank = "hybrid_rerank" in profiles
    pins = dataset.manifest.model_pins
    if wants_dense and (
        pins.embedding_model != QWEN_MODEL
        or pins.embedding_revision != QWEN_REVISION
        or pins.embedding_query_instruction_version != QWEN_QUERY_INSTRUCTION_VERSION
    ):
        raise ValueError("Dataset Qwen model, revision, or query-instruction pin does not match the LITE worker")
    if wants_rerank and (
        pins.reranker_model != GTE_MODEL
        or pins.reranker_revision != GTE_REVISION
    ):
        raise ValueError("Dataset GTE model or model-revision pin does not match the LITE worker")
    if wants_rerank and pins.reranker_code_revision != GTE_CODE_REVISION:
        raise ValueError("Dataset GTE source-code provenance pin does not match this evaluator baseline")
    output_dir.mkdir(parents=True, exist_ok=True)
    phase_path = output_dir / "phase-events.jsonl"
    phase_path.write_text("", encoding="utf-8")

    http = httpx.Client(timeout=httpx.Timeout(60.0), trust_env=False)
    embedding_client = (
        LiteEmbeddingClient(embed_url, http=http) if wants_dense else None
    )
    reranker_client = (
        LiteRerankerClient(rerank_url, http=http) if wants_rerank else None
    )
    warmup: dict[str, Any] = {"count": 0, "embedding_ms": [], "reranking_ms": []}
    try:
        _append_phase(phase_path, "preflight:start")
        if embedding_client:
            embedding_client.preflight()
        if reranker_client:
            reranker_client.preflight()
        _append_phase(phase_path, "preflight:complete")
        if cache_state == "warm":
            _append_phase(phase_path, "warmup:start")
            for _ in range(warmup_count):
                if embedding_client:
                    _append_phase(phase_path, "warmup:embedding")
                    result = embedding_client.embed_queries(["Versailles retrieval warmup"])
                    warmup["embedding_ms"].append(result.elapsed_ms)
                if reranker_client:
                    _append_phase(phase_path, "warmup:reranking")
                    result = reranker_client.rerank(
                        "Versailles retrieval warmup",
                        ["A warmup passage used only to initialize the service."],
                        top_n=1,
                    )
                    warmup["reranking_ms"].append(result.elapsed_ms)
                warmup["count"] += 1
            _append_phase(phase_path, "warmup:complete")
        traces = run_retrieval(
            dataset,
            profiles=profiles,
            embedding_client=embedding_client,
            reranker_client=reranker_client,
            on_phase=lambda phase: _append_phase(phase_path, phase),
        )
    finally:
        http.close()

    trace_path = output_dir / "traces.jsonl"
    judgments = dataset.judgments
    split_query_ids = {query.query_id for query in dataset.queries}
    split_judgments = {
        query_id: judgment
        for query_id, judgment in judgments.items()
        if query_id in split_query_ids
    }
    trace_rows: list[dict[str, Any]] = []
    for trace in traces:
        row = trace.to_dict()
        row["metrics"] = _query_metrics_for_trace(trace, judgments.get(trace.query_id))
        row["query_text"] = next(
            question.text for question in dataset.queries if question.query_id == trace.query_id
        )
        trace_rows.append(row)
    _write_jsonl(trace_path, trace_rows)
    worker_start_path = output_dir / "workers-at-start.json"
    worker_start_metadata = (
        json.loads(worker_start_path.read_text(encoding="utf-8"))
        if worker_start_path.is_file()
        else {"local_docker_sampling": "unavailable"}
    )

    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": str(uuid.uuid4()),
                "created_at_utc": _utc_now(),
                "split": split,
                "profiles": profiles,
                "execution_location": execution_location,
                "cache_state": cache_state,
                "warmup": warmup,
                "worker_start_metadata": worker_start_metadata,
                "dataset_status": dataset.manifest.status,
                "source_review_state": dataset.manifest.source_review_state,
                "dataset_hashes": _dataset_hashes(dataset_root, split),
                "dataset_counts": {
                    "sources": len(dataset.sources),
                    "passages": len(dataset.passages),
                    "queries": len(dataset.queries),
                    "judgments": len(split_judgments),
                    "reviewed_judgments": sum(
                        item.review_state in {"reviewed", "adjudicated"}
                        for item in split_judgments.values()
                    ),
                    "target_queries": dataset.manifest.targets[split].target_queries,
                    "actual_queries": dataset.manifest.targets[split].actual_queries,
                    "target_reviewed_queries": dataset.manifest.targets[split].reviewed_queries,
                },
                "model_pins": dataset.manifest.model_pins.model_dump(),
                "model_identities": {
                    "embedding": _client_identity(embedding_client),
                    "reranker": _client_identity(reranker_client),
                },
                "endpoints": {
                    "embedding": _clean_endpoint(embed_url) if wants_dense else None,
                    "reranking": _clean_endpoint(rerank_url) if wants_rerank else None,
                },
                "hardware": {
                    "host": platform.node(),
                    "platform": platform.platform(),
                    "processor": platform.processor(),
                    "cpu_count": os.cpu_count(),
                },
                "bm25_parameters": {
                    "k1": dataset.manifest.bm25_k1,
                    "b": dataset.manifest.bm25_b,
                },
                "rrf_k": dataset.manifest.rrf_k,
                "candidate_pool_size": dataset.manifest.candidate_pool_size,
                "validation_warnings": data_validation.warnings,
                "phase_events": phase_path.name,
                "trace_file": trace_path.name,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary = write_report(
        traces,
        judgments,
        output_dir,
        dataset_status=dataset.manifest.status,
        validation_warnings=data_validation.warnings,
        reported_dataset_hashes=_dataset_hashes(dataset_root, split),
        source_review_state=dataset.manifest.source_review_state,
        source_review_method=dataset.manifest.source_review_method,
        source_reviewer_ids=dataset.manifest.source_reviewer_ids,
        expected_query_ids={query.query_id for query in dataset.queries},
        target_queries=dataset.manifest.targets[split].target_queries,
        actual_queries=dataset.manifest.targets[split].actual_queries,
        manifest_reviewed_queries=dataset.manifest.targets[split].reviewed_queries,
    )
    return summary


def _query_metrics_for_trace(trace: QueryTrace, judgment: Any) -> dict[str, Any]:
    from .metrics import query_metrics

    return query_metrics([item.__dict__ for item in trace.ranking], judgment)
