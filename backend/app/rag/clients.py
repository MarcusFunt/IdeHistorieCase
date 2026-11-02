"""Injectable Text Embeddings Inference clients with strict response validation."""
from collections.abc import Sequence
from math import isfinite

import httpx

from app.rag.repository import EMBEDDING_DIMENSIONS

MAX_MODEL_BATCH = 32
MAX_PASSAGE_CHARS = 4000


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
        if any(not isinstance(text, str) or not text.strip() or len(text) > MAX_PASSAGE_CHARS for text in values):
            raise ValueError("embedding texts must be nonblank and at most 4000 characters")
        try:
            response = await self.client.post(
                f"{self.base_url}/embed",
                json={"inputs": values},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError("embedding service request or response failed") from exc
        if not isinstance(payload, list) or len(payload) != len(values):
            raise ModelServiceError("embedding response count does not match request count")
        vectors: list[list[float]] = []
        for index, vector in enumerate(payload):
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
            vectors.append([float(value) for value in vector])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


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
        try:
            response = await self.client.post(
                f"{self.base_url}/rerank",
                json={"query": query, "texts": texts, "raw_scores": False},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError("rerank service request or response failed") from exc
        if not isinstance(payload, list) or len(payload) != len(texts):
            raise ModelServiceError("rerank response count does not match request count")
        scores: list[float | None] = [None] * len(texts)
        for result in payload:
            if not isinstance(result, dict):
                raise ModelServiceError("rerank response entries must be objects")
            index, score = result.get("index"), result.get("score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(texts)
                or scores[index] is not None
            ):
                raise ModelServiceError("rerank response has duplicate or invalid indices")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not isfinite(score)
                or not 0.0 <= score <= 1.0
            ):
                raise ModelServiceError("rerank response has an invalid normalized score")
            scores[index] = float(score)
        if any(score is None for score in scores):
            raise ModelServiceError("rerank response omitted one or more passages")
        return [score for score in scores if score is not None]
