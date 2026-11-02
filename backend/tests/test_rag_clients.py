"""Injected HTTP clients validate the Local-AI retrieval API contract."""

import json
import math

import httpx
import pytest

from app.rag.clients import EmbeddingServiceClient, ModelServiceError, RerankServiceClient

EMBEDDING_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
RERANKER_REVISION = "8215cf04918ba6f7b6a62bb44238ce2953d8831c"
RERANKER_CODE_REVISION = "40ced75c3017eb27626c9d4ea981bde21a2662f4"


def vector(value=1.0):
    return [value] + [0.0] * 1023


def embedding_metadata(revision=EMBEDDING_REVISION):
    return {
        "data": [
            {
                "id": "ddu-embed",
                "revision": revision,
                "capabilities": {
                    "dimensions": 1024,
                    "input_types": ["query", "document"],
                    "query_instruction_version": "qwen3-question-to-passages-en-v1",
                },
            }
        ]
    }


def reranker_metadata(revision=RERANKER_REVISION):
    return {
        "data": [
            {
                "id": "ddu-rerank",
                "revision": revision,
                "capabilities": {
                    "code_revision": RERANKER_CODE_REVISION,
                    "score_semantics": "raw_ranking_signal",
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_embedding_client_sends_local_ai_document_contract_and_validates_vectors():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=embedding_metadata())
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "ddu-embed",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": vector(1.0)},
                    {"object": "embedding", "index": 1, "embedding": vector(0.5)},
                ],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient(
            "http://ddu-embed:8080",
            "Qwen/Qwen3-Embedding-0.6B@" + EMBEDDING_REVISION,
            client,
            timeout_seconds=7.5,
        )
        vectors = await embedder.embed_documents(["first", "second"])

    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"
    assert requests[1].url.path == "/v1/embeddings"
    assert json.loads(requests[1].read()) == {
        "model": "ddu-embed",
        "input": ["first", "second"],
        "input_type": "document",
    }
    assert requests[1].extensions["timeout"]["read"] == 7.5
    assert vectors[0][0] == 1.0 and len(vectors) == 2


@pytest.mark.asyncio
async def test_embedding_queries_use_query_input_type_and_pinned_instruction():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=embedding_metadata())
        return httpx.Response(
            200,
            json={
                "model": "ddu-embed",
                "data": [
                    {"index": 0, "embedding": vector()},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient(
            "http://ddu-embed:8080",
            "Qwen/Qwen3-Embedding-0.6B@" + EMBEDDING_REVISION,
            client,
        )
        await embedder.embed_query("question")

    assert json.loads(requests[1].read()) == {
        "model": "ddu-embed",
        "input": "question",
        "input_type": "query",
    }
    assert embedding_metadata()["data"][0]["capabilities"]["query_instruction_version"] == (
        "qwen3-question-to-passages-en-v1"
    )


@pytest.mark.asyncio
async def test_embedding_client_batches_32_documents_at_local_ai_limit_of_eight():
    batch_sizes = []

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=embedding_metadata())
        body = json.loads(request.read())
        values = body["input"]
        batch_sizes.append(len(values))
        return httpx.Response(
            200,
            json={
                "model": "ddu-embed",
                "data": [{"index": index, "embedding": vector()} for index, _ in enumerate(values)],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient(
            "http://ddu-embed:8080",
            "Qwen/Qwen3-Embedding-0.6B@" + EMBEDDING_REVISION,
            client,
        )
        vectors = await embedder.embed_documents([f"passage {index}" for index in range(32)])

    assert batch_sizes == [8, 8, 8, 8]
    assert len(vectors) == 32


@pytest.mark.parametrize(
    "response",
    [
        {"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
        {"data": [{"index": 0, "embedding": vector(float("nan"))}]},
        {"embedding": vector()},
    ],
)
@pytest.mark.asyncio
async def test_embedding_client_rejects_wrong_dimensions_nonfinite_and_wrong_shapes(response):
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=embedding_metadata())
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient(
            "http://ddu-embed:8080",
            "Qwen/Qwen3-Embedding-0.6B@" + EMBEDDING_REVISION,
            client,
        )
        with pytest.raises(ModelServiceError):
            await embedder.embed_documents(["query"])


@pytest.mark.asyncio
async def test_embedding_client_fails_closed_when_runtime_revision_does_not_match_pin():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json=embedding_metadata(revision="unreviewed"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient(
            "http://ddu-embed:8080",
            "Qwen/Qwen3-Embedding-0.6B@" + EMBEDDING_REVISION,
            client,
        )
        with pytest.raises(ModelServiceError, match="pin"):
            await embedder.embed_documents(["query"])

    assert [request.url.path for request in calls] == ["/v1/models"]


@pytest.mark.asyncio
async def test_reranker_adapts_local_ai_raw_scores_to_input_order_and_bounded_values():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=reranker_metadata())
        assert request.extensions["timeout"]["read"] == 13.5
        return httpx.Response(
            200,
            json={
                "model": "ddu-rerank",
                "revision": RERANKER_REVISION,
                "results": [
                    {"index": 1, "relevance_score": 1.0},
                    {"index": 0, "relevance_score": -1.0},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = RerankServiceClient(
            "http://ddu-rerank:8080",
            "Alibaba-NLP/gte-multilingual-reranker-base@" + RERANKER_REVISION,
            client,
            timeout_seconds=13.5,
        )
        scores = await reranker.rerank("what happened", ["first", "second"])

    assert requests[0].url.path == "/v1/models"
    assert requests[1].url.path == "/v1/rerank"
    assert json.loads(requests[1].read()) == {
        "model": "ddu-rerank",
        "query": "what happened",
        "documents": ["first", "second"],
    }
    assert scores == pytest.approx([1 / (1 + math.exp(1)), 1 / (1 + math.exp(-1))])
    assert all(0 <= score <= 1 for score in scores)


@pytest.mark.parametrize(
    "response",
    [
        {"results": [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}]},
        {"results": [{"index": 2, "relevance_score": 0.5}]},
        {
            "results": [
                {"index": 0, "relevance_score": math.inf},
                {"index": 1, "relevance_score": 0.4},
            ]
        },
        {"scores": [0.1, 0.4]},
    ],
)
@pytest.mark.asyncio
async def test_reranker_rejects_duplicate_out_of_range_nonfinite_or_malformed_scores(response):
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=reranker_metadata())
        return httpx.Response(
            200, json={"model": "ddu-rerank", "revision": RERANKER_REVISION, **response}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = RerankServiceClient(
            "http://ddu-rerank:8080",
            "Alibaba-NLP/gte-multilingual-reranker-base@" + RERANKER_REVISION,
            client,
        )
        with pytest.raises(ModelServiceError):
            await reranker.rerank("query", ["one", "two"])


@pytest.mark.asyncio
async def test_reranker_rejects_a_different_model_or_code_revision():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "ddu-rerank",
                        "revision": RERANKER_REVISION,
                        "capabilities": {
                            "code_revision": "unreviewed",
                            "score_semantics": "raw_ranking_signal",
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = RerankServiceClient(
            "http://ddu-rerank:8080",
            "Alibaba-NLP/gte-multilingual-reranker-base@" + RERANKER_REVISION,
            client,
        )
        with pytest.raises(ModelServiceError, match="pin"):
            await reranker.rerank("query", ["one"])


@pytest.mark.asyncio
async def test_reranker_batches_candidates_at_local_ai_limit_of_twenty():
    batch_sizes = []

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=reranker_metadata())
        documents = json.loads(request.read())["documents"]
        batch_sizes.append(len(documents))
        return httpx.Response(
            200,
            json={
                "model": "ddu-rerank",
                "revision": RERANKER_REVISION,
                "results": [
                    {"index": index, "relevance_score": float(index)}
                    for index, _ in enumerate(documents)
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = RerankServiceClient(
            "http://ddu-rerank:8080",
            "Alibaba-NLP/gte-multilingual-reranker-base@" + RERANKER_REVISION,
            client,
        )
        scores = await reranker.rerank("query", [f"passage {index}" for index in range(32)])

    assert batch_sizes == [20, 12]
    assert len(scores) == 32
    assert scores[0] == 0.5
    assert scores[20] == pytest.approx(1 / (1 + math.exp(-0)))
