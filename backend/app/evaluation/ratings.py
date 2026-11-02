from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .answer_study import ANSWER_CONDITIONS


class ClaimRating(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=80)
    claim_text: str = Field(min_length=1)
    factual_status: Literal["correct", "incorrect", "uncertain", "not_assessed"]
    evidence_support: Literal["supported", "unsupported", "contradicted", "not_applicable"]
    citation_status: Literal["correct", "incorrect", "missing", "not_applicable"]
    cited_passage_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class AnswerRating(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1, max_length=120)
    claim_ratings: list[ClaimRating] = Field(default_factory=list)
    coverage_score: int = Field(ge=0, le=3)
    relevance_score: int = Field(ge=0, le=3)
    temporal_consistency: Literal[
        "consistent", "leaks_future_information", "inconsistent", "unclear", "not_applicable"
    ]
    notes: str | None = None


def import_answer_ratings(
    *,
    ratings_path: Path,
    packets_path: Path,
    private_key_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    private_key = json.loads(private_key_path.read_text(encoding="utf-8"))
    packets = _read_jsonl(packets_path)
    packet_by_id = {packet.get("response_id"): packet for packet in packets}
    if len(packet_by_id) != len(packets) or None in packet_by_id:
        raise ValueError("Blind packet file contains duplicate or missing response IDs")
    if set(private_key) != set(packet_by_id):
        raise ValueError("Private answer key does not match the blind packet response IDs")
    if any(
        not isinstance(private_key[response_id], dict)
        or private_key[response_id].get("condition") not in ANSWER_CONDITIONS
        for response_id in packet_by_id
    ):
        raise ValueError("Private answer key contains a missing or unknown condition")

    ratings = _read_ratings(ratings_path)
    seen: set[tuple[str, str]] = set()
    for rating in ratings:
        response_id = rating.response_id
        if response_id not in packet_by_id:
            raise ValueError(f"Rating references an unknown response ID: {response_id}")
        key = (response_id, rating.reviewer_id)
        if key in seen:
            raise ValueError(f"Duplicate rating for response/reviewer: {response_id}/{rating.reviewer_id}")
        seen.add(key)
        claim_ids = [claim.claim_id for claim in rating.claim_ratings]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError(f"Duplicate claim ID in rating for response {response_id}")
        supplied_ids = {
            item.get("passage_id")
            for item in packet_by_id[response_id].get("evidence", [])
            if isinstance(item, dict)
        }
        for claim in rating.claim_ratings:
            if len(claim.cited_passage_ids) != len(set(claim.cited_passage_ids)):
                raise ValueError(f"Duplicate citation ID in {response_id}/{claim.claim_id}")
            if claim.citation_status == "correct":
                if (
                    not claim.cited_passage_ids
                    or not set(claim.cited_passage_ids) <= supplied_ids
                    or claim.evidence_support != "supported"
                ):
                    raise ValueError(
                        f"Correct citation on {response_id}/{claim.claim_id} must cite supplied, "
                        "claim-supporting passages"
                    )
            if claim.citation_status == "missing" and claim.cited_passage_ids:
                raise ValueError(
                    f"Missing citation rating on {response_id}/{claim.claim_id} has cited passage IDs"
                )

    summary = _summarize_ratings(ratings, private_key)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "answer_quality_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "answer_quality_summary.md").write_text(
        _markdown_summary(summary), encoding="utf-8"
    )
    return summary


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON in {path.name}:{line_number}") from error
            if not isinstance(row, dict):
                raise ValueError(f"Expected a JSON object in {path.name}:{line_number}")
            records.append(row)
    return records


def _read_ratings(path: Path) -> list[AnswerRating]:
    ratings: list[AnswerRating] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                ratings.append(AnswerRating.model_validate(row))
            except (json.JSONDecodeError, ValidationError) as error:
                raise ValueError(f"Invalid answer rating in {path.name}:{line_number}") from error
    return ratings


def _summarize_ratings(
    ratings: list[AnswerRating],
    private_key: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_condition: dict[str, list[AnswerRating]] = defaultdict(list)
    expected_ids: dict[str, set[str]] = defaultdict(set)
    generation_latencies: dict[str, list[float]] = defaultdict(list)
    for response_id, private in private_key.items():
        expected_ids[private["condition"]].add(response_id)
        elapsed = private.get("generation", {}).get("elapsed_ms")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            generation_latencies[private["condition"]].append(float(elapsed))
    for rating in ratings:
        condition = private_key[rating.response_id]["condition"]
        by_condition[condition].append(rating)

    conditions: dict[str, Any] = {}
    for condition in ANSWER_CONDITIONS:
        rows = by_condition.get(condition, [])
        expected_responses = len(expected_ids.get(condition, set()))
        claims = [claim for row in rows for claim in row.claim_ratings]
        factual_claims = [
            claim for claim in claims if claim.factual_status in {"correct", "incorrect"}
        ]
        citations = [
            claim for claim in claims
            if claim.citation_status in {"correct", "incorrect", "missing"}
        ]
        support_ratings = [
            claim for claim in claims
            if claim.evidence_support in {"supported", "unsupported", "contradicted"}
        ]
        rated_response_ids = {row.response_id for row in rows}
        conditions[condition] = {
            "ratings": len(rows),
            "rated_responses": len(rated_response_ids),
            "expected_responses": expected_responses,
            "response_coverage": len(rated_response_ids) / expected_responses
            if expected_responses
            else None,
            "claim_count": len(claims),
            "factual_claim_count_assessed": len(factual_claims),
            "factual_accuracy_rate": _rate(
                sum(claim.factual_status == "correct" for claim in factual_claims),
                len(factual_claims),
            ),
            "factual_inaccuracy_count": sum(
                claim.factual_status == "incorrect" for claim in factual_claims
            ),
            "evidence_supported_count": sum(
                claim.evidence_support == "supported" for claim in support_ratings
            ),
            "evidence_unsupported_count": sum(
                claim.evidence_support == "unsupported" for claim in support_ratings
            ),
            "evidence_contradicted_count": sum(
                claim.evidence_support == "contradicted" for claim in support_ratings
            ),
            "evidence_support_assessed_count": len(support_ratings),
            "citation_correct_count": sum(
                claim.citation_status == "correct" for claim in citations
            ),
            "citation_incorrect_count": sum(
                claim.citation_status == "incorrect" for claim in citations
            ),
            "citation_missing_count": sum(
                claim.citation_status == "missing" for claim in citations
            ),
            "citation_correctness_rate": _rate(
                sum(claim.citation_status == "correct" for claim in citations),
                len(citations),
            ),
            "coverage_score_mean": mean(row.coverage_score for row in rows) if rows else None,
            "relevance_score_mean": mean(row.relevance_score for row in rows) if rows else None,
            "temporal_inconsistency_count": sum(
                row.temporal_consistency == "inconsistent" for row in rows
            ),
            "future_information_leak_count": sum(
                row.temporal_consistency == "leaks_future_information" for row in rows
            ),
            "uncertain_fact_count": sum(
                claim.factual_status == "uncertain" for claim in claims
            ),
            "generation_latency_ms": {
                "samples": len(generation_latencies.get(condition, [])),
                "p50": _percentile(generation_latencies.get(condition, []), 0.50),
                "p95": _percentile(generation_latencies.get(condition, []), 0.95),
            },
        }
    return {
        "rating_source": "blind human evaluation",
        "fact_status_independent_of_evidence_support": True,
        "no_context_is_not_auto_scored_factually_incorrect": True,
        "expected_responses_per_condition": {
            condition: len(response_ids)
            for condition, response_ids in sorted(expected_ids.items())
        },
        "ratings_imported": len(ratings),
        "conditions": conditions,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# Human answer-quality evaluation",
        "",
        "Blind human ratings are primary. Factual accuracy, evidence support, and citation correctness are separate.",
        "No-context answers are not automatically factually incorrect.",
        "",
        "| Condition | Rated / expected | Factual accuracy | Unsupported claims | Contradicted claims | Citation correctness | Coverage | Relevance | Future leaks |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition, row in summary["conditions"].items():
        lines.append(
            f"| {condition} | {row['rated_responses']}/{row['expected_responses']} | "
            f"{_display(row['factual_accuracy_rate'])} | {row['evidence_unsupported_count']} | "
            f"{row['evidence_contradicted_count']} | {_display(row['citation_correctness_rate'])} | "
            f"{_display(row['coverage_score_mean'])} | {_display(row['relevance_score_mean'])} | "
            f"{row['future_information_leak_count']} |"
        )
    return "\n".join(lines) + "\n"


def _display(value: float | None) -> str:
    return "pending" if value is None else f"{value:.3f}"
