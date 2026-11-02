from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal

from .dataset import available_passages
from .model_clients import GTE_CODE_REVISION, LiteEmbeddingClient, LiteRerankerClient
from .schemas import EvaluationDataset, PassageRecord

Profile = Literal["bm25", "dense", "hybrid", "hybrid_rerank"]
PROFILES: tuple[Profile, ...] = ("bm25", "dense", "hybrid", "hybrid_rerank")


@dataclass(frozen=True)
class RankedPassage:
    passage_id: str
    rank: int
    score: float
    text: str
    source_id: str
    anchor: str
    component_scores: dict[str, float]


@dataclass(frozen=True)
class QueryTrace:
    query_id: str
    split: str
    language: str
    topic: str
    kind: str
    profile: str
    model_identities: dict[str, dict[str, Any]]
    timings_ms: dict[str, float]
    token_usage: dict[str, int]
    pre_rerank: list[RankedPassage]
    ranking: list[RankedPassage]
    retrieved_passages: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def bm25_scores(
    query: str,
    passages: list[PassageRecord],
    *,
    k1: float,
    b: float,
) -> dict[str, float]:
    documents = {p.passage_id: tokenize(p.text) for p in passages}
    if not documents:
        return {}
    average_length = sum(len(tokens) for tokens in documents.values()) / len(documents)
    query_terms = tokenize(query)
    document_frequency: dict[str, int] = {}
    for tokens in documents.values():
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    scores: dict[str, float] = {}
    total_docs = len(documents)
    for passage_id, tokens in documents.items():
        frequencies: dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if frequency == 0:
                continue
            df = document_frequency.get(term, 0)
            inverse_frequency = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            length_norm = 1 - b + b * (len(tokens) / average_length if average_length else 0)
            score += inverse_frequency * (
                frequency * (k1 + 1) / (frequency + k1 * length_norm)
            )
        scores[passage_id] = score
    return scores


def _rank(
    passages: list[PassageRecord],
    scores: dict[str, float],
    component_scores: dict[str, dict[str, float]] | None = None,
) -> list[RankedPassage]:
    component_scores = component_scores or {}
    ordered = sorted(passages, key=lambda p: (-scores.get(p.passage_id, 0.0), p.passage_id))
    return [
        RankedPassage(
            passage_id=passage.passage_id,
            rank=index,
            score=float(scores.get(passage.passage_id, 0.0)),
            text=passage.text,
            source_id=passage.source_id,
            anchor=passage.anchor,
            component_scores={
                name: float(values.get(passage.passage_id, 0.0))
                for name, values in component_scores.items()
            },
        )
        for index, passage in enumerate(ordered, start=1)
    ]


def _dense_scores(
    query_vector: list[float],
    passages: list[PassageRecord],
    vectors: dict[str, list[float]],
) -> dict[str, float]:
    return {
        passage.passage_id: sum(
            left * right for left, right in zip(query_vector, vectors[passage.passage_id], strict=True)
        )
        for passage in passages
        if passage.passage_id in vectors
    }


def _rrf(
    bm25_ranking: list[RankedPassage],
    dense_ranking: list[RankedPassage],
    *,
    rrf_k: int,
) -> tuple[list[RankedPassage], dict[str, float]]:
    scores: dict[str, float] = {}
    for ranking in (bm25_ranking, dense_ranking):
        for item in ranking:
            scores[item.passage_id] = scores.get(item.passage_id, 0.0) + 1.0 / (
                rrf_k + item.rank
            )
    by_id: dict[str, RankedPassage] = {
        item.passage_id: item for item in (*bm25_ranking, *dense_ranking)
    }
    ordered_ids = sorted(scores, key=lambda passage_id: (-scores[passage_id], passage_id))
    result = [
        RankedPassage(
            passage_id=passage_id,
            rank=index,
            score=scores[passage_id],
            text=by_id[passage_id].text,
            source_id=by_id[passage_id].source_id,
            anchor=by_id[passage_id].anchor,
            component_scores={
                "bm25": next(
                    (item.score for item in bm25_ranking if item.passage_id == passage_id), 0.0
                ),
                "dense": next(
                    (item.score for item in dense_ranking if item.passage_id == passage_id), 0.0
                ),
            },
        )
        for index, passage_id in enumerate(ordered_ids, start=1)
    ]
    return result, scores


def _embed_all_documents(
    passages: list[PassageRecord], client: LiteEmbeddingClient
) -> tuple[dict[str, list[float]], float, int, int, float, float]:
    vectors: dict[str, list[float]] = {}
    elapsed_ms = 0.0
    prompt_tokens = 0
    total_tokens = 0
    request_timings: list[float] = []
    for start in range(0, len(passages), 8):
        batch = passages[start : start + 8]
        result = client.embed_documents([passage.text for passage in batch])
        vectors.update(
            (passage.passage_id, vector) for passage, vector in zip(batch, result.vectors, strict=True)
        )
        elapsed_ms += result.elapsed_ms
        request_timings.append(result.elapsed_ms)
        prompt_tokens += result.prompt_tokens
        total_tokens += result.total_tokens
    return (
        vectors,
        elapsed_ms,
        prompt_tokens,
        total_tokens,
        request_timings[0] if request_timings else 0.0,
        sum(request_timings[1:]),
    )


def run_retrieval(
    dataset: EvaluationDataset,
    *,
    profiles: list[Profile],
    embedding_client: LiteEmbeddingClient | None,
    reranker_client: LiteRerankerClient | None,
    on_phase: Callable[[str], None] | None = None,
) -> list[QueryTrace]:
    if not profiles or any(profile not in PROFILES for profile in profiles):
        raise ValueError(f"Profiles must be selected from {', '.join(PROFILES)}")
    if any(profile in {"dense", "hybrid", "hybrid_rerank"} for profile in profiles):
        if embedding_client is None:
            raise ValueError("Dense/hybrid profiles require the real Qwen embedding client")
        if embedding_client.identity is None:
            embedding_client.preflight()
    if "hybrid_rerank" in profiles:
        if reranker_client is None:
            raise ValueError("hybrid_rerank requires the real GTE reranker client")
        if reranker_client.identity is None:
            reranker_client.preflight()

    all_passages = [p for p in dataset.passages.values() if p.text_kind == "source_text"]
    dense_vectors: dict[str, list[float]] = {}
    corpus_embedding_ms = 0.0
    corpus_prompt_tokens = 0
    corpus_total_tokens = 0
    corpus_embedding_first_ms = 0.0
    corpus_embedding_remaining_ms = 0.0
    if any(profile in {"dense", "hybrid", "hybrid_rerank"} for profile in profiles):
        assert embedding_client is not None
        if on_phase:
            on_phase("qwen_corpus_embedding")
        (
            dense_vectors,
            corpus_embedding_ms,
            corpus_prompt_tokens,
            corpus_total_tokens,
            corpus_embedding_first_ms,
            corpus_embedding_remaining_ms,
        ) = _embed_all_documents(all_passages, embedding_client)

    traces: list[QueryTrace] = []
    first_rerank_request = True
    for profile in profiles:
        uses_dense = profile in {"dense", "hybrid", "hybrid_rerank"}
        uses_rerank = profile == "hybrid_rerank"
        for query in dataset.queries:
            if on_phase:
                on_phase(f"profile:{profile}:start")
            available = available_passages(query, dataset.passages)
            if not available:
                raise ValueError(f"No temporally available passages for query {query.query_id}")

            timings: dict[str, float] = {}
            usage = {
                "qwen_query_prompt_tokens": 0,
                "qwen_query_total_tokens": 0,
                "qwen_corpus_prompt_tokens": 0,
                "qwen_corpus_total_tokens": 0,
                "gte_prompt_tokens": 0,
                "gte_total_tokens": 0,
            }
            identities: dict[str, dict[str, Any]] = {}
            rerank_pre: list[RankedPassage] = []

            bm25_rank: list[RankedPassage] = []
            if profile in {"bm25", "hybrid", "hybrid_rerank"}:
                if on_phase:
                    on_phase(f"bm25:{profile}")
                started = time.perf_counter()
                lexical_scores = bm25_scores(
                    query.text,
                    available,
                    k1=dataset.manifest.bm25_k1,
                    b=dataset.manifest.bm25_b,
                )
                bm25_rank = _rank(available, lexical_scores, {"bm25": lexical_scores})
                timings["bm25"] = (time.perf_counter() - started) * 1000

            dense_rank: list[RankedPassage] = []
            if uses_dense:
                assert embedding_client is not None
                if on_phase:
                    on_phase(f"qwen_query_embedding:{profile}")
                query_result = embedding_client.embed_queries([query.text])
                timings["dense_query_embedding"] = query_result.elapsed_ms
                usage["qwen_query_prompt_tokens"] = query_result.prompt_tokens
                usage["qwen_query_total_tokens"] = query_result.total_tokens
                usage["qwen_corpus_prompt_tokens"] = corpus_prompt_tokens
                usage["qwen_corpus_total_tokens"] = corpus_total_tokens
                if embedding_client.identity:
                    identities["embedding"] = {
                        "alias": embedding_client.identity.alias,
                        "task": embedding_client.identity.task,
                        "revision": embedding_client.identity.revision,
                        "query_instruction_version": embedding_client.identity.capabilities.get(
                            "query_instruction_version"
                        ),
                    }
                if on_phase:
                    on_phase(f"dense_rank:{profile}")
                started = time.perf_counter()
                dense_scores = _dense_scores(query_result.vectors[0], available, dense_vectors)
                dense_rank = _rank(available, dense_scores, {"dense": dense_scores})
                timings["dense_rank"] = (time.perf_counter() - started) * 1000

            if uses_dense:
                timings.update(
                    {
                        "corpus_embedding_total": corpus_embedding_ms,
                        "corpus_embedding_first_request": corpus_embedding_first_ms,
                        "corpus_embedding_remaining_requests": corpus_embedding_remaining_ms,
                        "corpus_embedding_per_query_amortized": (
                            corpus_embedding_ms / len(dataset.queries)
                            if dataset.queries
                            else 0.0
                        ),
                    }
                )

            if profile == "bm25":
                ranking = bm25_rank
            elif profile == "dense":
                ranking = dense_rank
            else:
                if on_phase:
                    on_phase(f"rrf:{profile}")
                started = time.perf_counter()
                ranking, _ = _rrf(
                    bm25_rank, dense_rank, rrf_k=dataset.manifest.rrf_k
                )
                timings["rrf"] = (time.perf_counter() - started) * 1000

            if uses_rerank:
                assert reranker_client is not None
                if reranker_client.identity:
                    identities["reranker"] = {
                        "alias": reranker_client.identity.alias,
                        "task": reranker_client.identity.task,
                        "revision": reranker_client.identity.revision,
                        "expected_code_revision": GTE_CODE_REVISION,
                    }
                pool_size = min(dataset.manifest.candidate_pool_size, 20, len(ranking))
                rerank_pre = ranking[:pool_size]
                if on_phase:
                    on_phase(f"gte_rerank:{profile}")
                reranked = reranker_client.rerank(
                    query.text,
                    [item.text for item in rerank_pre],
                    top_n=pool_size,
                )
                rerank_timing_name = (
                    "gte_rerank_first_query_request" if first_rerank_request else "gte_rerank"
                )
                timings[rerank_timing_name] = reranked.elapsed_ms
                first_rerank_request = False
                usage["gte_prompt_tokens"] = reranked.prompt_tokens
                usage["gte_total_tokens"] = reranked.total_tokens
                ranking = [
                    RankedPassage(
                        passage_id=rerank_pre[index].passage_id,
                        rank=rank,
                        score=reranked.scores_by_index[index],
                        text=rerank_pre[index].text,
                        source_id=rerank_pre[index].source_id,
                        anchor=rerank_pre[index].anchor,
                        component_scores={
                            **rerank_pre[index].component_scores,
                            "rrf": rerank_pre[index].score,
                        },
                    )
                    for rank, index in enumerate(reranked.ranked_indexes, start=1)
                ] + [
                    RankedPassage(
                        passage_id=item.passage_id,
                        rank=pool_size + offset,
                        score=item.score,
                        text=item.text,
                        source_id=item.source_id,
                        anchor=item.anchor,
                        component_scores=item.component_scores,
                    )
                    for offset, item in enumerate(ranking[pool_size:], start=1)
                ]

            timings["total_client_retrieval"] = sum(
                elapsed
                for name, elapsed in timings.items()
                if not name.startswith("corpus_embedding_")
            )
            if on_phase:
                on_phase("idle")

            result = ranking[:10]
            traces.append(
                QueryTrace(
                    query_id=query.query_id,
                    split=query.split,
                    language=query.language,
                    topic=query.topic,
                    kind=query.kind,
                    profile=profile,
                    model_identities=identities,
                    timings_ms=timings,
                    token_usage=usage,
                    pre_rerank=rerank_pre,
                    ranking=result,
                    retrieved_passages=[
                        {
                            "passage_id": item.passage_id,
                            "source_id": item.source_id,
                            "anchor": item.anchor,
                            "text": item.text,
                        }
                        for item in result
                    ],
                )
            )
    return traces
