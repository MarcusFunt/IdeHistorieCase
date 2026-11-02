from __future__ import annotations

import hashlib
import json
import random
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from .dataset import available_passages, load_dataset, sha256_file, validate_dataset
from .prompts import build_messages
from .report import read_traces
from .retrieval import PROFILES, QueryTrace
from .schemas import EvaluationDataset, EvaluationQuery, PassageRecord, QueryJudgment

ANSWER_CONDITIONS = (*PROFILES, "oracle", "no_context")
MIN_ANSWER_SAMPLE = 25
MAX_ANSWER_SAMPLE = 40


class TokenCounter(Protocol):
    def count_tokens(self, text: str) -> int: ...


class AnswerStudyError(ValueError):
    pass


class VllmAnswerStudyClient:
    """Optional Stage 2 client for the existing LITE chat worker."""

    def __init__(
        self,
        base_url: str,
        model_alias: str,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_alias = model_alias
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0), trust_env=False)
        self.identity: dict[str, Any] | None = None

    def preflight(self) -> dict[str, Any]:
        health = self.http.get(f"{self.base_url}/health")
        if health.status_code != 200:
            raise AnswerStudyError(f"Generator /health returned {health.status_code}")
        models = self.http.get(f"{self.base_url}/v1/models")
        models.raise_for_status()
        try:
            document = models.json()
        except ValueError as error:
            raise AnswerStudyError("Generator /v1/models did not return JSON") from error
        records = document.get("data") if isinstance(document, dict) else None
        if not isinstance(records, list):
            raise AnswerStudyError("Generator /v1/models response has no data list")
        record = next(
            (
                item for item in records
                if isinstance(item, dict) and item.get("id") == self.model_alias
            ),
            None,
        )
        if record is None:
            raise AnswerStudyError(f"Generator does not serve model alias {self.model_alias}")
        self.identity = {
            key: record.get(key)
            for key in ("id", "created", "owned_by", "root", "revision")
            if key in record
        }
        return self.identity

    def count_tokens(self, text: str) -> int:
        if self.identity is None:
            self.preflight()
        response = self.http.post(
            f"{self.base_url}/tokenize",
            json={"model": self.model_alias, "prompt": text, "add_special_tokens": True},
        )
        if response.status_code == 404:
            raise AnswerStudyError(
                "The generator does not expose /tokenize; Stage 2 requires exact served-tokenizer counts."
            )
        response.raise_for_status()
        try:
            document = response.json()
        except ValueError as error:
            raise AnswerStudyError("Generator /tokenize did not return JSON") from error
        count = document.get("count") if isinstance(document, dict) else None
        tokens = document.get("tokens") if isinstance(document, dict) else None
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(tokens, list)
            or len(tokens) != count
        ):
            raise AnswerStudyError("Generator /tokenize response has an invalid token count")
        return count

    def generate(self, query: EvaluationQuery, evidence_block: str) -> dict[str, Any]:
        if self.identity is None:
            self.preflight()
        started = time.perf_counter()
        response = self.http.post(
            f"{self.base_url}/v1/chat/completions",
            json={
                "model": self.model_alias,
                "stream": False,
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 700,
                "messages": build_messages(query, evidence_block),
            },
        )
        response.raise_for_status()
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            document = response.json()
        except ValueError as error:
            raise AnswerStudyError("Generator completion response is not JSON") from error
        if not isinstance(document, dict) or document.get("model") != self.model_alias:
            raise AnswerStudyError("Generator completion model alias mismatch")
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AnswerStudyError("Generator completion response has no choice")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise AnswerStudyError(
                f"Generator completion did not finish normally: {choice.get('finish_reason')!r}"
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise AnswerStudyError("Generator completion content is empty or malformed")
        usage = document.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        return {
            "answer": content.strip(),
            "elapsed_ms": elapsed_ms,
            "prompt_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
            "completion_tokens": completion_tokens if isinstance(completion_tokens, int) else None,
            "system_fingerprint": document.get("system_fingerprint"),
        }

    def close(self) -> None:
        if self._owns_http:
            self.http.close()


def select_stratified_queries(
    queries: list[EvaluationQuery], *, sample_size: int, seed: int
) -> list[EvaluationQuery]:
    if not MIN_ANSWER_SAMPLE <= sample_size <= MAX_ANSWER_SAMPLE:
        raise AnswerStudyError(
            f"Answer sample size must be between {MIN_ANSWER_SAMPLE} and {MAX_ANSWER_SAMPLE}"
        )
    groups: dict[tuple[str, str, bool, str], list[EvaluationQuery]] = defaultdict(list)
    for query in queries:
        if query.split != "heldout":
            continue
        key = (query.language, query.topic, query.answerable, query.kind)
        groups[key].append(query)
    for key, group in groups.items():
        group.sort(
            key=lambda query: hashlib.sha256(
                f"{seed}:{query.query_id}".encode("utf-8")
            ).hexdigest()
        )

    selected: list[EvaluationQuery] = []
    group_keys = sorted(groups)
    round_index = 0
    while len(selected) < sample_size:
        added = False
        for key in group_keys:
            if round_index < len(groups[key]):
                selected.append(groups[key][round_index])
                added = True
                if len(selected) == sample_size:
                    break
        if not added:
            break
        round_index += 1
    if len(selected) != sample_size:
        raise AnswerStudyError(
            f"Only {len(selected)} held-out queries are available for requested sample {sample_size}"
        )
    return selected


def select_reviewed_answer_queries(
    queries: list[EvaluationQuery],
    judgments: dict[str, QueryJudgment],
    *,
    sample_size: int,
    seed: int,
) -> list[EvaluationQuery]:
    eligible: list[EvaluationQuery] = []
    for query in queries:
        if query.split != "heldout":
            continue
        judgment = judgments.get(query.query_id)
        if judgment is None or judgment.review_state not in {"reviewed", "adjudicated"}:
            continue
        if query.answerable and not _oracle_passage_ids(judgment):
            continue
        eligible.append(query)
    return select_stratified_queries(eligible, sample_size=sample_size, seed=seed)


def _passage_block(passage: PassageRecord, dataset: EvaluationDataset) -> str:
    source = dataset.sources[passage.source_id]
    return (
        f"[{passage.passage_id}]\n{passage.text}\n"
        f"Source: {source.title}; {passage.anchor}; {source.url}"
    )


def _evidence_block(passages: list[PassageRecord], dataset: EvaluationDataset) -> str:
    return "\n\n".join(_passage_block(passage, dataset) for passage in passages)


def _select_evidence(
    passage_ids: list[str],
    dataset: EvaluationDataset,
    tokenizer: TokenCounter,
    *,
    token_budget: int,
    max_passages: int,
    token_cache: dict[str, int],
) -> tuple[list[PassageRecord], int]:
    selected: list[PassageRecord] = []
    estimated_sum = 0
    for passage_id in passage_ids:
        passage = dataset.passages.get(passage_id)
        if passage is None or passage.text_kind != "source_text":
            continue
        block = _passage_block(passage, dataset)
        if block not in token_cache:
            token_cache[block] = tokenizer.count_tokens(block)
        passage_tokens = token_cache[block]
        if estimated_sum + passage_tokens > token_budget:
            continue
        selected.append(passage)
        estimated_sum += passage_tokens
        if len(selected) >= max_passages:
            break
    while selected:
        exact_count = tokenizer.count_tokens(_evidence_block(selected, dataset))
        if exact_count <= token_budget:
            return selected, exact_count
        selected.pop()
    return [], 0


def _oracle_passage_ids(judgment: QueryJudgment) -> list[str]:
    return [
        passage_id
        for passage_id, grade in sorted(
            judgment.relevance.items(), key=lambda item: (-item[1], item[0])
        )
        if grade > 0
    ]


def _review_context(query: EvaluationQuery, dataset: EvaluationDataset) -> dict[str, Any]:
    context: dict[str, Any] = {
        "kind": query.kind,
        "actor_id": query.actor_id if query.kind == "simulation" else None,
        "as_of": query.as_of.isoformat() if query.as_of else None,
        "source_creation_cutoff": (
            query.source_creation_cutoff.isoformat() if query.source_creation_cutoff else None
        ),
        "publication_cutoff": (
            query.publication_cutoff.isoformat() if query.publication_cutoff else None
        ),
    }
    if query.kind == "simulation":
        context["effective_source_creation_cutoff"] = (
            (query.source_creation_cutoff or query.as_of).isoformat()
            if query.source_creation_cutoff or query.as_of
            else None
        )
        context["effective_publication_cutoff"] = (
            (query.publication_cutoff or query.as_of).isoformat()
            if query.publication_cutoff or query.as_of
            else None
        )
        context["allowed_knowledge_passage_ids"] = sorted(
            passage.passage_id for passage in available_passages(query, dataset.passages)
        )
    return context


def _validate_stage1_traces(
    *,
    dataset_root: Path,
    traces_path: Path,
    dataset: EvaluationDataset,
    traces: list[QueryTrace],
) -> dict[str, dict[str, QueryTrace]]:
    run_path = traces_path.parent / "run.json"
    if not run_path.is_file():
        raise AnswerStudyError("Answer study requires run.json beside the retrieval traces")
    try:
        run_metadata = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnswerStudyError("Stage 1 run.json is missing or malformed") from error
    if not isinstance(run_metadata, dict) or run_metadata.get("split") != "heldout":
        raise AnswerStudyError("Answer study requires Stage 1 metadata for the held-out split")
    recorded_profiles = run_metadata.get("profiles")
    if (
        not isinstance(recorded_profiles, list)
        or any(not isinstance(profile, str) for profile in recorded_profiles)
        or set(recorded_profiles) != set(PROFILES)
    ):
        raise AnswerStudyError("Stage 1 metadata must record all four retrieval profiles")

    query_filename = "heldout_queries.jsonl"
    expected_hashes = {
        name: sha256_file(dataset_root / name)
        for name in ("sources.jsonl", "passages.jsonl", query_filename)
    }
    recorded_hashes = run_metadata.get("dataset_hashes")
    if not isinstance(recorded_hashes, dict):
        raise AnswerStudyError("Stage 1 run.json has no dataset hashes")
    stale = [
        filename
        for filename, digest in expected_hashes.items()
        if recorded_hashes.get(filename) != digest
    ]
    if stale:
        raise AnswerStudyError(
            "Stage 1 source, passage, or held-out query hashes are stale: "
            + ", ".join(stale)
        )

    queries_by_id = {query.query_id: query for query in dataset.queries}
    allowed_by_query = {
        query_id: {
            passage.passage_id for passage in available_passages(query, dataset.passages)
        }
        for query_id, query in queries_by_id.items()
    }
    traces_by_query: dict[str, dict[str, QueryTrace]] = defaultdict(dict)
    for trace in traces:
        if trace.split != "heldout" or trace.query_id not in queries_by_id:
            raise AnswerStudyError(
                f"Stage 1 trace is outside the current held-out dataset: {trace.query_id}"
            )
        if trace.profile not in PROFILES:
            raise AnswerStudyError(f"Unknown retrieval profile in trace: {trace.profile}")
        if trace.profile in traces_by_query[trace.query_id]:
            raise AnswerStudyError(
                f"Duplicate Stage 1 trace for {trace.query_id}/{trace.profile}"
            )
        for item in [*trace.pre_rerank, *trace.ranking]:
            passage = dataset.passages.get(item.passage_id)
            if passage is None or passage.text != item.text:
                raise AnswerStudyError(
                    f"Stage 1 trace passage does not match the current corpus: {item.passage_id}"
                )
            if item.passage_id not in allowed_by_query[trace.query_id]:
                raise AnswerStudyError(
                    f"Stage 1 trace contains temporally unavailable evidence: "
                    f"{trace.query_id}/{item.passage_id}"
                )
        traces_by_query[trace.query_id][trace.profile] = trace

    expected_query_ids = set(queries_by_id)
    if set(traces_by_query) != expected_query_ids:
        missing = sorted(expected_query_ids - set(traces_by_query))
        extra = sorted(set(traces_by_query) - expected_query_ids)
        raise AnswerStudyError(
            f"Stage 1 traces must cover the full held-out set; missing={missing[:5]}, extra={extra[:5]}"
        )
    for query_id, profiles in traces_by_query.items():
        missing = set(PROFILES) - set(profiles)
        if missing:
            raise AnswerStudyError(
                f"Stage 1 trace for {query_id} is missing profiles: {', '.join(sorted(missing))}"
            )
    return traces_by_query


def prepare_answer_study(
    *,
    dataset_root: Path,
    traces_path: Path,
    output_dir: Path,
    generator_url: str,
    generator_model: str,
    sample_size: int = 25,
    seed: int = 198,
    evidence_token_budget: int = 1500,
    max_passages: int = 5,
    client: VllmAnswerStudyClient | None = None,
) -> dict[str, Any]:
    if evidence_token_budget < 1 or max_passages < 1:
        raise AnswerStudyError("Evidence token budget and max passages must be positive")
    validation = validate_dataset(dataset_root)
    if validation.errors:
        raise AnswerStudyError("Dataset validation failed: " + "; ".join(validation.errors))
    dataset = load_dataset(dataset_root, "heldout")
    heldout_target = dataset.manifest.targets["heldout"]
    if len(dataset.queries) < heldout_target.target_queries:
        raise AnswerStudyError(
            f"Held-out target is incomplete: {len(dataset.queries)}/{heldout_target.target_queries}"
        )
    selected = select_reviewed_answer_queries(
        dataset.queries,
        dataset.judgments,
        sample_size=sample_size,
        seed=seed,
    )

    traces = read_traces(traces_path)
    if not traces:
        raise AnswerStudyError("Answer study requires non-empty held-out retrieval traces")
    traces_by_query = _validate_stage1_traces(
        dataset_root=dataset_root,
        traces_path=traces_path,
        dataset=dataset,
        traces=traces,
    )

    owns_client = client is None
    generator = client or VllmAnswerStudyClient(generator_url, generator_model)
    token_cache: dict[str, int] = {}
    packets: list[dict[str, Any]] = []
    private_key: dict[str, dict[str, Any]] = {}
    query_refs: dict[str, str] = {}
    latencies_by_condition: dict[str, list[float]] = defaultdict(list)
    try:
        identity = generator.preflight()
        for query in selected:
            question_ref = query_refs.setdefault(query.query_id, secrets.token_urlsafe(8))
            judgment = dataset.judgments[query.query_id]
            temporally_available_ids = {
                passage.passage_id
                for passage in available_passages(query, dataset.passages)
            }
            candidates: dict[str, list[str]] = {
                profile: [item.passage_id for item in traces_by_query[query.query_id][profile].ranking]
                for profile in PROFILES
            }
            candidates["oracle"] = [
                passage_id
                for passage_id in _oracle_passage_ids(judgment)
                if passage_id in temporally_available_ids
            ]
            candidates["no_context"] = []
            for condition in ANSWER_CONDITIONS:
                evidence, token_count = _select_evidence(
                    candidates[condition],
                    dataset,
                    generator,
                    token_budget=evidence_token_budget,
                    max_passages=max_passages,
                    token_cache=token_cache,
                )
                evidence_block = _evidence_block(evidence, dataset)
                generated = generator.generate(query, evidence_block)
                latencies_by_condition[condition].append(generated["elapsed_ms"])
                response_id = secrets.token_urlsafe(18)
                private_key[response_id] = {
                    "query_id": query.query_id,
                    "question_ref": question_ref,
                    "condition": condition,
                    "evidence_passage_ids": [passage.passage_id for passage in evidence],
                    "evidence_token_count": token_count,
                    "generation": {
                        "elapsed_ms": generated["elapsed_ms"],
                        "prompt_tokens": generated["prompt_tokens"],
                        "completion_tokens": generated["completion_tokens"],
                        "system_fingerprint": generated["system_fingerprint"],
                    },
                }
                packets.append(
                    {
                        "response_id": response_id,
                        "question_ref": question_ref,
                        "language": query.language,
                        "question": query.text,
                        "review_context": _review_context(query, dataset),
                        "answer": generated["answer"],
                        "evidence": [
                            {
                                "passage_id": passage.passage_id,
                                "text": passage.text,
                                "source_title": dataset.sources[passage.source_id].title,
                                "institution": dataset.sources[passage.source_id].institution,
                                "source_url": str(dataset.sources[passage.source_id].url),
                                "anchor": passage.anchor,
                                "source_document_created_on": (
                                    passage.source_document_created_on.isoformat()
                                    if passage.source_document_created_on else None
                                ),
                                "source_published_on": (
                                    passage.source_published_on.isoformat()
                                    if passage.source_published_on else None
                                ),
                                "actor_knowledge": [
                                    rule.model_dump(mode="json") for rule in passage.actor_knowledge
                                ],
                            }
                            for passage in evidence
                        ],
                    }
                )
    finally:
        if owns_client:
            generator.close()

    random.Random(seed).shuffle(packets)
    output_dir.mkdir(parents=True, exist_ok=True)
    packets_path = output_dir / "blind_review_packets.jsonl"
    with packets_path.open("w", encoding="utf-8", newline="\n") as stream:
        for packet in packets:
            stream.write(json.dumps(packet, ensure_ascii=False) + "\n")
    (output_dir / "answer_key_private.json").write_text(
        json.dumps(private_key, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "answer_run_private.json").write_text(
        json.dumps(
            {
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "split": "heldout",
                "sample_size": sample_size,
                "seed": seed,
                "strata": ["language", "topic", "answerable", "kind"],
                "conditions": list(ANSWER_CONDITIONS),
                "max_passages_per_condition": max_passages,
                "evidence_token_budget": evidence_token_budget,
                "tokenizer": identity,
                "generation_latency_ms_by_condition": {
                    condition: {
                        "samples": len(values),
                        "p50": _percentile(values, 0.50),
                        "p95": _percentile(values, 0.95),
                    }
                    for condition, values in sorted(latencies_by_condition.items())
                },
                "ratings_primary": "blind human review of the selected sample",
                "query_review_methods": {
                    query.query_id: (
                        dataset.judgments[query.query_id].review_method or "human"
                    )
                    for query in selected
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "rating_template.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "response_id": packet["response_id"],
                    "reviewer_id": "",
                    "claim_ratings": [],
                    "coverage_score": None,
                    "relevance_score": None,
                    "temporal_consistency": None,
                    "notes": None,
                },
                ensure_ascii=False,
            ) + "\n"
            for packet in packets
        ),
        encoding="utf-8",
    )
    return {
        "question_count": sample_size,
        "response_count": len(packets),
        "conditions": list(ANSWER_CONDITIONS),
        "review_packets": str(packets_path),
        "rating_template": str(output_dir / "rating_template.jsonl"),
        "private_key": str(output_dir / "answer_key_private.json"),
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
