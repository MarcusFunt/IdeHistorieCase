"""Pinned clients for the Local-AI-API-LITE retrieval API."""

from collections.abc import Sequence
from math import exp, isfinite

import httpx

from app.rag.repository import EMBEDDING_DIMENSIONS

MAX_MODEL_BATCH = 32
MAX_EMBEDDING_API_BATCH = 8
MAX_RERANKING_API_BATCH = 20
MAX_PASSAGE_CHARS = 4000
EMBEDDING_API_MODEL = "ddu-embed"
EMBEDDING_MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
EMBEDDING_QUERY_INSTRUCTION_VERSION = "qwen3-question-to-passages-en-v1"
RERANKING_API_MODEL = "ddu-rerank"
RERANKING_MODEL_REVISION = "8215cf04918ba6f7b6a62bb44238ce2953d8831c"
RERANKING_CODE_REVISION = "40ced75c3017eb27626c9d4ea981bde21a2662f4"


async def _verify_model_pin(
    base_url: str,
    api_model: str,
    revision: str,
    client: httpx.AsyncClient,
    *,
    timeout_seconds: float,
    required_capabilities: dict[str, object],
) -> None:
    try:
        response = await client.get(f"{base_url}/v1/models", timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelServiceError("retrieval model identity request failed") from exc
    entries = payload.get("data") if isinstance(payload, dict) else None
    metadata = (
        next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("id") == api_model
            ),
            None,
        )
        if isinstance(entries, list)
        else None
    )
    capabilities = metadata.get("capabilities") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("revision") != revision
        or not isinstance(capabilities, dict)
        or any(capabilities.get(key) != value for key, value in required_capabilities.items())
    ):
        raise ModelServiceError(
            "retrieval service model pin does not match the reviewed configuration"
        )


class ModelServiceError(RuntimeError):
    """An inference endpoint failed or returned data outside its contract."""


class EmbeddingServiceClient:
    def __init__(
        self,
        base_url: str,
        model_id: str,
        client: httpx.AsyncClient,
        *,
        dimensions: int = EMBEDDING_DIMENSIONS,
        timeout_seconds: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id.strip()
        self.client = client
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("embedding base_url must use HTTP or HTTPS")
        if not self.model_id or len(self.model_id) > 160:
            raise ValueError("embedding model_id must contain 1 to 160 characters")
        if dimensions < 1 or dimensions > 4096:
            raise ValueError("embedding dimensions must be between 1 and 4096")
        if not 0.1 <= timeout_seconds <= 120.0:
            raise ValueError("embedding timeout must be between 0.1 and 120 seconds")

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        values = list(texts)
        if not values or len(values) > MAX_MODEL_BATCH:
            raise ValueError(f"embedding batch must contain 1 to {MAX_MODEL_BATCH} texts")
        if any(
            not isinstance(text, str) or not text.strip() or len(text) > MAX_PASSAGE_CHARS
            for text in values
        ):
            raise ValueError("embedding texts must be nonblank and at most 4000 characters")
        await _verify_model_pin(
            self.base_url,
            EMBEDDING_API_MODEL,
            EMBEDDING_MODEL_REVISION,
            self.client,
            timeout_seconds=self.timeout_seconds,
            required_capabilities={
                "dimensions": self.dimensions,
                "input_types": ["query", "document"],
                "query_instruction_version": EMBEDDING_QUERY_INSTRUCTION_VERSION,
            },
        )
        vectors: list[list[float]] = []
        for start in range(0, len(values), MAX_EMBEDDING_API_BATCH):
            chunk = values[start : start + MAX_EMBEDDING_API_BATCH]
            vectors.extend(await self._embed(chunk, input_type="document"))
        return vectors

    async def _embed(self, values: list[str], *, input_type: str) -> list[list[float]]:
        try:
            response = await self.client.post(
                f"{self.base_url}/v1/embeddings",
                json={
                    "model": EMBEDDING_API_MODEL,
                    "input": values if len(values) > 1 else values[0],
                    "input_type": input_type,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError("embedding service request or response failed") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("model") != EMBEDDING_API_MODEL
            or not isinstance(payload.get("data"), list)
            or len(payload["data"]) != len(values)
        ):
            raise ModelServiceError("embedding response count does not match request count")
        indexed_vectors: list[list[float] | None] = [None] * len(values)
        for result in payload["data"]:
            if not isinstance(result, dict):
                raise ModelServiceError("embedding response entries must be objects")
            index, vector = result.get("index"), result.get("embedding")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(values)
                or indexed_vectors[index] is not None
            ):
                raise ModelServiceError("embedding response has duplicate or invalid indices")
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise ModelServiceError(f"embedding {index} has an unexpected vector shape")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                for value in vector
            ):
                raise ModelServiceError(f"embedding {index} contains invalid numeric values")
            if not any(value != 0 for value in vector):
                raise ModelServiceError(f"embedding {index} is the zero vector")
            indexed_vectors[index] = [float(value) for value in vector]
        if any(vector is None for vector in indexed_vectors):
            raise ModelServiceError("embedding response omitted one or more inputs")
        return [vector for vector in indexed_vectors if vector is not None]

    async def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PASSAGE_CHARS:
            raise ValueError("embedding query must be nonblank and at most 4000 characters")
        await _verify_model_pin(
            self.base_url,
            EMBEDDING_API_MODEL,
            EMBEDDING_MODEL_REVISION,
            self.client,
            timeout_seconds=self.timeout_seconds,
            required_capabilities={
                "dimensions": self.dimensions,
                "input_types": ["query", "document"],
                "query_instruction_version": EMBEDDING_QUERY_INSTRUCTION_VERSION,
            },
        )
        return (await self._embed([text], input_type="query"))[0]


class RerankServiceClient:
    def __init__(
        self,
        base_url: str,
        model_id: str,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id.strip()
        self.client = client
        self.timeout_seconds = timeout_seconds
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("rerank base_url must use HTTP or HTTPS")
        if not self.model_id or len(self.model_id) > 160:
            raise ValueError("rerank model_id must contain 1 to 160 characters")
        if not 0.1 <= timeout_seconds <= 120.0:
            raise ValueError("rerank timeout must be between 0.1 and 120 seconds")

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        texts = list(passages)
        if not query.strip() or len(query) > 2000:
            raise ValueError("rerank query must be nonblank and at most 2000 characters")
        if not texts or len(texts) > MAX_MODEL_BATCH:
            raise ValueError(f"rerank batch must contain 1 to {MAX_MODEL_BATCH} passages")
        if any(not text.strip() or len(text) > MAX_PASSAGE_CHARS for text in texts):
            raise ValueError("rerank passages must be nonblank and at most 4000 characters")
        await _verify_model_pin(
            self.base_url,
            RERANKING_API_MODEL,
            RERANKING_MODEL_REVISION,
            self.client,
            timeout_seconds=self.timeout_seconds,
            required_capabilities={
                "code_revision": RERANKING_CODE_REVISION,
                "score_semantics": "raw_ranking_signal",
            },
        )
        scores: list[float] = []
        for start in range(0, len(texts), MAX_RERANKING_API_BATCH):
            chunk = texts[start : start + MAX_RERANKING_API_BATCH]
            scores.extend(await self._rerank_chunk(query, chunk))
        return scores

    async def _rerank_chunk(self, query: str, texts: list[str]) -> list[float]:
        try:
            response = await self.client.post(
                f"{self.base_url}/v1/rerank",
                json={"model": RERANKING_API_MODEL, "query": query, "documents": texts},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError("rerank service request or response failed") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("model") != RERANKING_API_MODEL
            or payload.get("revision") != RERANKING_MODEL_REVISION
            or not isinstance(payload.get("results"), list)
            or len(payload["results"]) != len(texts)
        ):
            raise ModelServiceError("rerank response count does not match request count")
        raw_scores: list[float | None] = [None] * len(texts)
        for result in payload["results"]:
            if not isinstance(result, dict):
                raise ModelServiceError("rerank response entries must be objects")
            index, score = result.get("index"), result.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(texts)
                or raw_scores[index] is not None
            ):
                raise ModelServiceError("rerank response has duplicate or invalid indices")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not isfinite(score)
            ):
                raise ModelServiceError("rerank response has an invalid raw score")
            raw_scores[index] = float(score)
        if any(score is None for score in raw_scores):
            raise ModelServiceError("rerank response omitted one or more passages")
        # The Local-AI reranker returns raw logits; sigmoid preserves ordering and
        # maps the signal into the Cases retrieval threshold's documented [0, 1] range.
        return [_sigmoid(score) for score in raw_scores if score is not None]


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + exp(-value)) if value >= 0 else exp(value) / (1.0 + exp(value))
