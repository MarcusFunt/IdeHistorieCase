from __future__ import annotations

import json

import httpx
import pytest

from app.evaluation.model_clients import (
    EMBEDDING_DIMENSION,
    GTE_CODE_REVISION,
    GTE_REVISION,
    QWEN_QUERY_INSTRUCTION_VERSION,
    QWEN_REVISION,
    LiteClientError,
    LiteEmbeddingClient,
    LiteRerankerClient,
)


def _metadata(alias: str, task: str, revision: str, capabilities: dict, limits: dict) -> dict:
    return {"data": [{"id": alias, "task": task, "revision": revision, "capabilities": capabilities, "limits": limits}]}


def test_embedding_client_uses_lite_v1_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health" or request.url.path == "/ready":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json=_metadata(
                    "ddu-embed",
                    "embedding",
                    QWEN_REVISION,
                    {
                        "dimensions": EMBEDDING_DIMENSION,
                        "normalized": True,
                        "dtype": "float32",
                        "input_types": ["query", "document"],
                        "query_instruction_version": QWEN_QUERY_INSTRUCTION_VERSION,
                    },
                    {"max_batch_size": 8},
                ),
            )
        assert request.url.path == "/v1/embeddings"
        payload = json.loads(request.content)
        assert payload == {
            "model": "ddu-embed",
            "input": ["query text"],
            "input_type": "query",
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "model": "ddu-embed",
                "data": [{"index": 0, "embedding": [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    client = LiteEmbeddingClient(
        "http://embed.test", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        result = client.embed_queries(["query text"])
    finally:
        client.close()

    assert len(result.vectors) == 1
    assert result.revision == QWEN_REVISION
    assert [request.url.path for request in requests] == [
        "/health",
        "/ready",
        "/v1/models",
        "/v1/embeddings",
    ]


def test_embedding_client_fails_closed_on_capability_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health" or request.url.path == "/ready":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json=_metadata(
                "ddu-embed",
                "embedding",
                QWEN_REVISION,
                {"dimensions": 768},
                {"max_batch_size": 8},
            ),
        )

    client = LiteEmbeddingClient(
        "http://embed.test", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(LiteClientError, match="capability dimensions mismatch"):
            client.embed_documents(["document text"])
    finally:
        client.close()


def test_reranker_client_requires_pinned_code_revision_and_uses_lite_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health" or request.url.path == "/ready":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json=_metadata(
                    "ddu-rerank",
                    "rerank",
                    GTE_REVISION,
                    {"score_semantics": "raw_ranking_signal", "code_revision": GTE_CODE_REVISION},
                    {"max_candidate_pairs": 20},
                ),
            )
        assert request.url.path == "/v1/rerank"
        assert json.loads(request.content) == {
            "model": "ddu-rerank",
            "query": "question",
            "documents": ["first", "second"],
            "top_n": 2,
        }
        return httpx.Response(
            200,
            json={
                "model": "ddu-rerank",
                "revision": GTE_REVISION,
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": -0.1},
                ],
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            },
        )

    client = LiteRerankerClient(
        "http://rerank.test", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        result = client.rerank("question", ["first", "second"], top_n=2)
    finally:
        client.close()

    assert result.ranked_indexes == [1, 0]
    assert result.revision == GTE_REVISION
    assert [request.url.path for request in requests] == [
        "/health",
        "/ready",
        "/v1/models",
        "/v1/rerank",
    ]


def test_reranker_client_rejects_wrong_code_revision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health" or request.url.path == "/ready":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json=_metadata(
                "ddu-rerank",
                "rerank",
                GTE_REVISION,
                {"score_semantics": "raw_ranking_signal", "code_revision": "wrong"},
                {"max_candidate_pairs": 20},
            ),
        )

    client = LiteRerankerClient(
        "http://rerank.test", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(LiteClientError, match="capability code_revision mismatch"):
            client.rerank("question", ["document"], top_n=1)
    finally:
        client.close()
