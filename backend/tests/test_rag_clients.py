"""Injected HTTP clients validate the TEI embedding and reranking boundaries."""
import math

import httpx
import pytest

from app.rag.clients import EmbeddingServiceClient, ModelServiceError, RerankServiceClient


def vector(value=1.0):
    return [value] + [0.0] * 1023


@pytest.mark.asyncio
async def test_embedding_client_sends_batched_tei_payload_and_validates_vectors():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[vector(1.0), vector(0.5)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient(
            "http://ddu-embed:8080", "embed-model-v1", client, timeout_seconds=7.5
        )
        vectors = await embedder.embed_documents(["first", "second"])

    assert requests[0].url.path == "/embed"
    assert requests[0].read() == b'{"inputs":["first","second"]}'
    assert requests[0].extensions["timeout"]["read"] == 7.5
    assert vectors[0][0] == 1.0 and len(vectors) == 2


@pytest.mark.parametrize("response", [[[1.0, 2.0]], [vector(float("nan"))], {"embedding": vector()}])
@pytest.mark.asyncio
async def test_embedding_client_rejects_wrong_dimensions_nonfinite_and_wrong_shapes(response):
    async def handler(_request):
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = EmbeddingServiceClient("http://ddu-embed:8080", "embed-model-v1", client)
        with pytest.raises(ModelServiceError):
            await embedder.embed_documents(["query"])


@pytest.mark.asyncio
async def test_reranker_maps_unsorted_tei_results_back_to_input_order():
    async def handler(request):
        assert request.url.path == "/rerank"
        assert request.extensions["timeout"]["read"] == 13.5
        assert request.read() == (
            b'{"query":"what happened","texts":["first","second"],"raw_scores":false}'
        )
        return httpx.Response(
            200,
            json=[{"index": 1, "score": 0.91}, {"index": 0, "score": 0.22}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = RerankServiceClient(
            "http://ddu-rerank:8080", "rerank-v1", client, timeout_seconds=13.5
        )
        scores = await reranker.rerank("what happened", ["first", "second"])

    assert scores == [0.22, 0.91]


@pytest.mark.parametrize(
    "response",
    [
        [{"index": 0, "score": 0.1}, {"index": 0, "score": 0.2}],
        [{"index": 2, "score": 0.5}],
        [{"index": 0, "score": math.inf}, {"index": 1, "score": 0.4}],
        {"scores": [0.1, 0.4]},
    ],
)
@pytest.mark.asyncio
async def test_reranker_rejects_duplicate_out_of_range_nonfinite_or_malformed_scores(response):
    async def handler(_request):
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reranker = RerankServiceClient("http://ddu-rerank:8080", "rerank-v1", client)
        with pytest.raises(ModelServiceError):
            await reranker.rerank("query", ["one", "two"])
