from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from .dataset import (
    _read_jsonl,
    _unique_map,
    available_passages,
    refresh_manifest_hashes,
    validate_dataset,
)
from .schemas import EvaluationQuery, PassageRecord, QueryJudgment, ReviewMethod, SourceRecord


def export_unjudged_pool(
    *,
    dataset_root: Path,
    traces_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    validation = validate_dataset(dataset_root)
    if validation.errors:
        raise ValueError("Dataset validation failed: " + "; ".join(validation.errors))
    queries = [
        *(_read_jsonl(dataset_root / "pilot_queries.jsonl", EvaluationQuery)),
        *(_read_jsonl(dataset_root / "heldout_queries.jsonl", EvaluationQuery)),
    ]
    queries_by_id = _unique_map(queries, "query_id", "query")
    passages = _unique_map(
        _read_jsonl(dataset_root / "passages.jsonl", PassageRecord), "passage_id", "passage"
    )
    sources = _unique_map(
        _read_jsonl(dataset_root / "sources.jsonl", SourceRecord), "source_id", "source"
    )
    judgments = _unique_map(
        _read_jsonl(dataset_root / "judgments.jsonl", QueryJudgment), "query_id", "judgment"
    )
    pooled: dict[str, dict[str, Any]] = {}
    with traces_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            trace = json.loads(line)
            query_id = trace["query_id"]
            if query_id not in queries_by_id:
                raise ValueError(f"Trace references unknown query: {query_id}")
            judgment = judgments.get(query_id)
            previously_reviewed = bool(
                judgment and judgment.review_state in {"reviewed", "adjudicated"}
            )
            judged_ids = set(judgment.relevance) if previously_reviewed and judgment else set()
            if judgment and not previously_reviewed:
                for passage_id in judgment.relevance:
                    if passage_id not in passages:
                        raise ValueError(
                            f"Draft judgment references unknown passage: {query_id}/{passage_id}"
                        )
                    pooled.setdefault(
                        f"{query_id}/{passage_id}",
                        {"query_id": query_id, "passage_id": passage_id},
                    )
            for item in trace.get("ranking", []):
                passage_id = item["passage_id"]
                if passage_id not in passages:
                    raise ValueError(f"Trace references unknown passage: {query_id}/{passage_id}")
                if passage_id in judged_ids:
                    continue
                pooled.setdefault(
                    f"{query_id}/{passage_id}",
                    {"query_id": query_id, "passage_id": passage_id},
                )

    output_dir.mkdir(parents=True, exist_ok=True)
    key: dict[str, dict[str, str]] = {}
    candidate_ids_by_query: dict[str, list[str]] = {}
    review_path = output_dir / "pooled_review.jsonl"
    with review_path.open("w", encoding="utf-8", newline="\n") as review:
        for item in sorted(pooled.values(), key=lambda row: (row["query_id"], row["passage_id"])):
            query_id = item["query_id"]
            passage_id = item["passage_id"]
            query = queries_by_id[query_id]
            passage = passages[passage_id]
            source = sources[passage.source_id]
            candidate_id = secrets.token_hex(12)
            key[candidate_id] = {"query_id": query_id, "passage_id": passage_id}
            candidate_ids_by_query.setdefault(query_id, []).append(candidate_id)
            packet = {
                "query_id": query_id,
                "query": query.text,
                "language": query.language,
                "topic": query.topic,
                "kind": query.kind,
                "review_context": {
                    "actor_id": query.actor_id if query.kind == "simulation" else None,
                    "as_of": query.as_of.isoformat() if query.as_of else None,
                    "source_creation_cutoff": (
                        query.source_creation_cutoff.isoformat()
                        if query.source_creation_cutoff else None
                    ),
                    "publication_cutoff": (
                        query.publication_cutoff.isoformat()
                        if query.publication_cutoff else None
                    ),
                    "effective_source_creation_cutoff": (
                        (query.source_creation_cutoff or query.as_of).isoformat()
                        if query.kind == "simulation"
                        and (query.source_creation_cutoff or query.as_of) else None
                    ),
                    "effective_publication_cutoff": (
                        (query.publication_cutoff or query.as_of).isoformat()
                        if query.kind == "simulation"
                        and (query.publication_cutoff or query.as_of) else None
                    ),
                    "allowed_knowledge_passage_ids": sorted(
                        passage.passage_id
                        for passage in available_passages(query, passages)
                    ) if query.kind == "simulation" else [],
                },
                "candidate_id": candidate_id,
                "passage_text": passage.text,
                "source_title": source.title,
                "institution": source.institution,
                "source_url": str(source.url),
                "anchor": passage.anchor,
                "source_document_created_on": (
                    passage.source_document_created_on.isoformat()
                    if passage.source_document_created_on else None
                ),
                "source_published_on": (
                    passage.source_published_on.isoformat()
                    if passage.source_published_on else None
                ),
                "actor_knowledge": [rule.model_dump(mode="json") for rule in passage.actor_knowledge],
                "required_evidence_groups": [
                    {"group_id": group.group_id, "description": group.description}
                    for group in (judgments[query_id].required_evidence_groups if query_id in judgments else [])
                ],
                "relevance": None,
                "evidence_group_ids": [],
            }
            review.write(json.dumps(packet, ensure_ascii=False) + "\n")

    (output_dir / "pool_key_private.json").write_text(
        json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "pool_manifest.json").write_text(
        json.dumps(
            {
                "candidate_ids_by_query": candidate_ids_by_query,
                "candidate_count": len(key),
                "profile_and_rank_hidden": True,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "candidate_count": len(key),
        "query_count": len(candidate_ids_by_query),
        "review_file": str(review_path),
        "private_key_file": str(output_dir / "pool_key_private.json"),
    }


def import_pool_judgments(
    *,
    dataset_root: Path,
    ratings_path: Path,
    private_key_path: Path,
    pool_manifest_path: Path,
    reviewer_id: str,
    review_method: ReviewMethod = "human",
    review_note: str | None = None,
) -> dict[str, int]:
    private_key: dict[str, dict[str, str]] = json.loads(
        private_key_path.read_text(encoding="utf-8")
    )
    pool_manifest = json.loads(pool_manifest_path.read_text(encoding="utf-8"))
    current = _read_jsonl(dataset_root / "judgments.jsonl", QueryJudgment)
    judgments = _unique_map(current, "query_id", "judgment")
    ratings: list[dict[str, Any]] = []
    with ratings_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid rating JSON on line {line_number}") from error
            ratings.append(record)

    seen: set[str] = set()
    evidence_by_query_group: dict[str, dict[str, set[str]]] = {}
    review_notes_by_query: dict[str, set[str]] = {}
    previously_reviewed = {
        query_id: judgment.model_copy(deep=True)
        for query_id, judgment in judgments.items()
        if judgment.review_state in {"reviewed", "adjudicated"}
    }
    for rating in ratings:
        candidate_id = rating.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in private_key:
            raise ValueError("Rating contains an unknown blind candidate ID")
        if candidate_id in seen:
            raise ValueError(f"Duplicate blind candidate ID: {candidate_id}")
        seen.add(candidate_id)
        reference = private_key[candidate_id]
        query_id = reference["query_id"]
        passage_id = reference["passage_id"]
        rating_note = rating.get("review_note")
        if rating_note is not None:
            if not isinstance(rating_note, str):
                raise ValueError(f"Invalid review_note for {candidate_id}")
            if rating_note.strip():
                review_notes_by_query.setdefault(query_id, set()).add(rating_note.strip())
        grade = rating.get("relevance")
        if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
            raise ValueError(f"Rating for {candidate_id} must be an integer from 0 to 3")
        judgment = judgments.setdefault(
            query_id,
            QueryJudgment(query_id=query_id, review_state="draft"),
        )
        group_ids = rating.get("evidence_group_ids", [])
        if not isinstance(group_ids, list) or any(not isinstance(group_id, str) for group_id in group_ids):
            raise ValueError(f"Invalid evidence_group_ids for {candidate_id}")
        known_groups = {group.group_id for group in judgment.required_evidence_groups}
        if any(group_id not in known_groups for group_id in group_ids):
            raise ValueError(f"Rating references an undefined evidence group for {query_id}")
        judgment.relevance[passage_id] = grade
        for group_id in group_ids:
            evidence_by_query_group.setdefault(query_id, {}).setdefault(group_id, set()).add(
                passage_id
            )

    candidate_ids_by_query = pool_manifest.get("candidate_ids_by_query", {})
    reviewed_query_count = 0
    for query_id, candidate_ids in candidate_ids_by_query.items():
        judgment = judgments.get(query_id)
        complete = judgment is not None and all(candidate_id in seen for candidate_id in candidate_ids)
        if complete:
            evidence = evidence_by_query_group.get(query_id, {})
            old_judgment = previously_reviewed.get(query_id)
            old_support = {
                group.group_id: set(group.supporting_passage_ids)
                for group in old_judgment.required_evidence_groups
            } if old_judgment else {}
            judgment.required_evidence_groups = [
                group.model_copy(
                    update={
                        "supporting_passage_ids": sorted(
                            old_support.get(group.group_id, set())
                            | evidence.get(group.group_id, set())
                        )
                    }
                )
                for group in judgment.required_evidence_groups
                if old_support.get(group.group_id) or evidence.get(group.group_id)
            ]
            judgment.review_state = "reviewed"
            previous_method = old_judgment.review_method if old_judgment else None
            if previous_method is None and old_judgment:
                previous_method = "human"
            judgment.review_method = (
                "mixed"
                if previous_method is not None and previous_method != review_method
                else review_method
            )
            notes = [judgment.adjudication_note, review_note]
            notes.extend(sorted(review_notes_by_query.get(query_id, set())))
            judgment.adjudication_note = "\n".join(
                dict.fromkeys(note.strip() for note in notes if note and note.strip())
            ) or None
            if reviewer_id not in judgment.reviewer_ids:
                judgment.reviewer_ids.append(reviewer_id)
            reviewed_query_count += 1
        elif judgment is not None and query_id not in previously_reviewed:
            judgment.review_state = "draft"

    output_path = dataset_root / "judgments.jsonl"
    with output_path.open("w", encoding="utf-8", newline="\n") as stream:
        for judgment in sorted(judgments.values(), key=lambda item: item.query_id):
            stream.write(judgment.model_dump_json(exclude_none=True) + "\n")
    refresh_manifest_hashes(dataset_root)
    return {"ratings_imported": len(ratings), "queries_reviewed": reviewed_query_count}
