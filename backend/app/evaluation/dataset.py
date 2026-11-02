from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schemas import (
    EvaluationDataset,
    EvaluationManifest,
    EvaluationQuery,
    PassageRecord,
    QueryJudgment,
    SourceRecord,
    SplitName,
    ValidationSummary,
)


class DatasetError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"Cannot read JSON file {path.name}") from error
    if not isinstance(value, dict):
        raise DatasetError(f"{path.name} must contain a JSON object")
    return value


def _read_jsonl(path: Path, model_type: type) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise DatasetError(f"Cannot read JSONL file {path.name}") from error
    records: list[Any] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            records.append(model_type.model_validate(value))
        except (json.JSONDecodeError, ValidationError) as error:
            raise DatasetError(f"Invalid record in {path.name}:{line_number}") from error
    return records


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash UTF-8 text with universal newlines for stable cross-platform manifests."""
    digest = hashlib.sha256()
    with path.open("r", encoding="utf-8", newline=None) as source:
        for block in iter(lambda: source.read(1024 * 1024), ""):
            digest.update(block.encode("utf-8"))
    return digest.hexdigest()


def refresh_manifest_hashes(root: Path) -> None:
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    filenames = (
        "sources.jsonl",
        "passages.jsonl",
        "pilot_queries.jsonl",
        "heldout_queries.jsonl",
        "judgments.jsonl",
    )
    manifest["file_hashes"] = {
        filename: sha256_file(root / filename) for filename in filenames
    }
    judgments = _read_jsonl(root / "judgments.jsonl", QueryJudgment)
    reviewed_query_ids = {
        judgment.query_id
        for judgment in judgments
        if judgment.review_state in {"reviewed", "adjudicated"}
    }
    for split_name, query_filename in (
        ("pilot", "pilot_queries.jsonl"),
        ("heldout", "heldout_queries.jsonl"),
    ):
        queries = _read_jsonl(root / query_filename, EvaluationQuery)
        manifest["targets"][split_name]["actual_queries"] = len(queries)
        manifest["targets"][split_name]["reviewed_queries"] = sum(
            query.query_id in reviewed_query_ids for query in queries
        )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_dataset(root: Path, split: SplitName) -> EvaluationDataset:
    manifest = EvaluationManifest.model_validate(_read_json(root / "manifest.json"))
    sources_list = _read_jsonl(root / "sources.jsonl", SourceRecord)
    passages_list = _read_jsonl(root / "passages.jsonl", PassageRecord)
    query_file = "pilot_queries.jsonl" if split == "pilot" else "heldout_queries.jsonl"
    queries = _read_jsonl(root / query_file, EvaluationQuery)
    judgments_list = _read_jsonl(root / "judgments.jsonl", QueryJudgment)

    sources = _unique_map(sources_list, "source_id", "source")
    passages = _unique_map(passages_list, "passage_id", "passage")
    judgments = _unique_map(judgments_list, "query_id", "judgment")

    if any(query.split != split for query in queries):
        raise DatasetError(f"{query_file} contains a query from the wrong split")
    dataset = EvaluationDataset(
        manifest=manifest,
        sources=sources,
        passages=passages,
        queries=queries,
        judgments=judgments,
        split=split,
    )
    summary = _validate_references(dataset, query_counts={split: len(queries)})
    if summary.errors:
        raise DatasetError("; ".join(summary.errors))
    return dataset


def _unique_map(records: list[Any], key: str, kind: str) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for record in records:
        identifier = getattr(record, key)
        if identifier in mapped:
            raise DatasetError(f"Duplicate {kind} ID: {identifier}")
        mapped[identifier] = record
    return mapped


def validate_dataset(root: Path) -> ValidationSummary:
    manifest = EvaluationManifest.model_validate(_read_json(root / "manifest.json"))
    sources_list = _read_jsonl(root / "sources.jsonl", SourceRecord)
    passages_list = _read_jsonl(root / "passages.jsonl", PassageRecord)
    pilot = _read_jsonl(root / "pilot_queries.jsonl", EvaluationQuery)
    heldout = _read_jsonl(root / "heldout_queries.jsonl", EvaluationQuery)
    judgments_list = _read_jsonl(root / "judgments.jsonl", QueryJudgment)
    sources = _unique_map(sources_list, "source_id", "source")
    passages = _unique_map(passages_list, "passage_id", "passage")
    queries = pilot + heldout
    query_map = _unique_map(queries, "query_id", "query")
    judgments = _unique_map(judgments_list, "query_id", "judgment")

    errors: list[str] = []
    warnings: list[str] = []
    allowed_hash_files = {
        "sources.jsonl", "passages.jsonl", "pilot_queries.jsonl",
        "heldout_queries.jsonl", "judgments.jsonl",
    }
    for filename, expected_digest in manifest.file_hashes.items():
        if filename not in allowed_hash_files:
            errors.append(f"Manifest contains an unsupported file hash path: {filename}")
            continue
        file_path = root / filename
        if not file_path.is_file() or sha256_file(file_path) != expected_digest:
            errors.append(f"Manifest file hash mismatch: {filename}")
    for passage in passages.values():
        if passage.source_id not in sources:
            errors.append(f"Passage {passage.passage_id} references unknown source {passage.source_id}")
        if sha256_text(passage.text) != passage.sha256:
            errors.append(f"Passage text hash mismatch: {passage.passage_id}")
        source = sources.get(passage.source_id)
        if source and passage.source_document_created_on != source.document_created_on:
            warnings.append(f"Creation date differs between passage and source: {passage.passage_id}")
        if source and passage.source_published_on != source.source_published_on:
            warnings.append(f"Publication date differs between passage and source: {passage.passage_id}")

    for query in queries:
        if query.split == "pilot" and query not in pilot:
            errors.append(f"Query is in the wrong split file: {query.query_id}")
        if query.split == "heldout" and query not in heldout:
            errors.append(f"Query is in the wrong split file: {query.query_id}")
        if query.kind == "simulation" and (query.actor_id is None or query.as_of is None):
            errors.append(f"Simulation query needs actor_id and as_of: {query.query_id}")

    for judgment in judgments.values():
        if judgment.query_id not in query_map:
            errors.append(f"Judgment references unknown query: {judgment.query_id}")
        for passage_id, grade in judgment.relevance.items():
            if passage_id not in passages:
                errors.append(f"Judgment references unknown passage: {passage_id}")
            if not 0 <= grade <= 3:
                errors.append(f"Judgment grade outside 0–3: {judgment.query_id}/{passage_id}")
        for group in judgment.required_evidence_groups:
            for passage_id in group.supporting_passage_ids:
                if passage_id not in passages:
                    errors.append(
                        f"Evidence group references unknown passage: {judgment.query_id}/{passage_id}"
                    )

    required_hash_files = {
        "sources.jsonl", "passages.jsonl", "pilot_queries.jsonl",
        "heldout_queries.jsonl", "judgments.jsonl",
    }
    missing_hashes = required_hash_files - set(manifest.file_hashes)
    if missing_hashes:
        errors.append("Manifest is missing hashes for: " + ", ".join(sorted(missing_hashes)))
    for split_name, records in (("pilot", pilot), ("heldout", heldout)):
        target = manifest.targets[split_name]
        if len(records) != target.actual_queries:
            errors.append(
                f"{split_name} manifest actual_queries={target.actual_queries}, found {len(records)}"
            )
        if len(records) < target.target_queries:
            warnings.append(
                f"{split_name} target incomplete: {len(records)}/{target.target_queries} queries"
            )
        query_ids_for_split = {query.query_id for query in records}
        reviewed_for_split = sum(
            judgment.query_id in query_ids_for_split
            and judgment.review_state in {"reviewed", "adjudicated"}
            for judgment in judgments.values()
        )
        if reviewed_for_split != target.reviewed_queries:
            warnings.append(
                f"{split_name} manifest reviewed_queries={target.reviewed_queries}, "
                f"found {reviewed_for_split}"
            )

    reviewed = sum(item.review_state in {"reviewed", "adjudicated"} for item in judgments.values())
    judged_query_coverage = reviewed / len(queries) if queries else 0.0
    if manifest.status != "reviewed":
        warnings.append("Dataset status is draft; review status does not block retrieval evaluation")
    if reviewed < len(queries):
        warnings.append(
            f"Reviewed query sample: {reviewed}/{len(queries)}; only reviewed queries contribute to graded metrics"
        )
    if manifest.source_review_state != "reviewed":
        warnings.append(
            f"Source review state is {manifest.source_review_state}; source review does not block retrieval evaluation"
        )

    return ValidationSummary(
        source_count=len(sources),
        passage_count=len(passages),
        pilot_query_count=len(pilot),
        heldout_query_count=len(heldout),
        judgment_count=len(judgments),
        reviewed_judgment_count=reviewed,
        judged_query_coverage=judged_query_coverage,
        errors=errors,
        warnings=warnings,
    )


def _validate_references(
    dataset: EvaluationDataset,
    *,
    query_counts: dict[SplitName, int],
) -> ValidationSummary:
    del query_counts
    query_ids = {query.query_id for query in dataset.queries}
    errors: list[str] = []
    for passage in dataset.passages.values():
        if passage.source_id not in dataset.sources:
            errors.append(f"Passage {passage.passage_id} references unknown source {passage.source_id}")
        if sha256_text(passage.text) != passage.sha256:
            errors.append(f"Passage text hash mismatch: {passage.passage_id}")
    for query_id, judgment in dataset.judgments.items():
        if query_id not in query_ids:
            continue
        for passage_id in judgment.relevance:
            if passage_id not in dataset.passages:
                errors.append(f"Judgment references unknown passage: {passage_id}")
    return ValidationSummary(
        split=dataset.split,
        source_count=len(dataset.sources),
        passage_count=len(dataset.passages),
        pilot_query_count=sum(q.split == "pilot" for q in dataset.queries),
        heldout_query_count=sum(q.split == "heldout" for q in dataset.queries),
        judgment_count=len(dataset.judgments),
        reviewed_judgment_count=sum(
            judgment.review_state in {"reviewed", "adjudicated"}
            for judgment in dataset.judgments.values()
        ),
        judged_query_coverage=(
            sum(
                judgment.review_state in {"reviewed", "adjudicated"}
                for judgment in dataset.judgments.values()
            ) / len(dataset.queries)
            if dataset.queries
            else 0.0
        ),
        errors=errors,
        warnings=[],
    )


def available_passages(
    query: EvaluationQuery, passages: dict[str, PassageRecord]
) -> list[PassageRecord]:
    if query.kind == "retrospective_qa":
        return [p for p in passages.values() if p.text_kind == "source_text"]
    if query.actor_id is None or query.as_of is None:
        raise DatasetError(f"Simulation query needs actor/time access rules: {query.query_id}")

    accessible: list[PassageRecord] = []
    for passage in passages.values():
        if passage.text_kind != "source_text":
            continue
        creation_cutoff = query.source_creation_cutoff or query.as_of
        publication_cutoff = query.publication_cutoff or query.as_of
        if passage.source_document_created_on and passage.source_document_created_on > creation_cutoff:
            continue
        if passage.source_published_on and passage.source_published_on > publication_cutoff:
            continue
        rule = next(
            (entry for entry in passage.actor_knowledge if entry.actor_id == query.actor_id),
            None,
        )
        if rule is None or rule.available_on > query.as_of:
            continue
        if rule.unavailable_after and query.as_of > rule.unavailable_after:
            continue
        accessible.append(passage)
    return accessible
