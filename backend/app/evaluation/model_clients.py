from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

QWEN_MODEL = "Qwen/Qwen3-Embedding-0.6B"
QWEN_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QWEN_QUERY_INSTRUCTION_VERSION = "qwen3-question-to-passages-en-v1"
GTE_MODEL = "Alibaba-NLP/gte-multilingual-reranker-base"
GTE_REVISION = "8215cf04918ba6f7b6a62bb44238ce2953d8831c"
GTE_CODE_REVISION = "40ced75c3017eb27626c9d4ea981bde21a2662f4"
EMBEDDING_DIMENSION = 1024
EMBEDDING_BATCH_SIZE = 8
RERANKING_BATCH_SIZE = 20


@dataclass(frozen=True)
class ModelIdentity:
    alias: str
    task: str
    revision: str
    capabilities: dict[str, Any]
    limits: dict[str, Any]
    health_ms: float
    ready_ms: float
    models_ms: float
    preflight_completed_at_utc: str


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    model: str
    revision: str
    prompt_tokens: int
    total_tokens: int
    elapsed_ms: float


@dataclass(frozen=True)
class RerankingBatch:
    ranked_indexes: list[int]
    scores_by_index: dict[int, float]
    model: str
    revision: str
    prompt_tokens: int
    total_tokens: int
    elapsed_ms: float


class LiteClientError(RuntimeError):
    pass


class _BaseLiteClient:
    def __init__(
        self,
        base_url: str,
        expected_alias: str,
        expected_task: str,
        expected_revision: str,
        http: httpx.Client,
        required_capabilities: dict[str, Any],
        minimum_limits: dict[str, int],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.expected_alias = expected_alias
        self.expected_task = expected_task
        self.expected_revision = expected_revision
        self.http = http
        self.required_capabilities = required_capabilities
        self.minimum_limits = minimum_limits
        self.identity: ModelIdentity | None = None

    def preflight(self) -> ModelIdentity:
        timings: dict[str, float] = {}
        for route, name in (("/health", "health"), ("/ready", "ready")):
            started = time.perf_counter()
            response = self.http.get(f"{self.base_url}{route}")
            timings[name] = (time.perf_counter() - started) * 1000
            if response.status_code != 200:
                raise LiteClientError(f"{self.expected_alias} {route} returned {response.status_code}")

        started = time.perf_counter()
        response = self.http.get(f"{self.base_url}/v1/models")
        timings["models"] = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        try:
            document = response.json()
        except ValueError as error:
            raise LiteClientError(f"{self.expected_alias} model metadata is not JSON") from error
        records = document.get("data") if isinstance(document, dict) else None
        if not isinstance(records, list):
            raise LiteClientError(f"{self.expected_alias} model metadata has no data list")
        record = next(
            (
                item for item in records
                if isinstance(item, dict) and item.get("id") == self.expected_alias
            ),
            None,
        )
        if record is None:
            raise LiteClientError(f"Expected model alias {self.expected_alias} was not served")
        revision = record.get("revision")
        task = record.get("task")
        if revision != self.expected_revision:
            raise LiteClientError(
                f"{self.expected_alias} revision mismatch: expected {self.expected_revision}, "
                f"received {revision!r}"
            )
        if task != self.expected_task:
            raise LiteClientError(
                f"{self.expected_alias} task mismatch: expected {self.expected_task}, "
                f"received {task!r}"
            )
        capabilities = record.get("capabilities")
        limits = record.get("limits")
        if not isinstance(capabilities, dict) or not isinstance(limits, dict):
            raise LiteClientError(f"{self.expected_alias} metadata omitted capabilities or limits")
        for name, expected in self.required_capabilities.items():
            if capabilities.get(name) != expected:
                raise LiteClientError(
                    f"{self.expected_alias} capability {name} mismatch: expected {expected!r}, "
                    f"received {capabilities.get(name)!r}"
                )
        for name, minimum in self.minimum_limits.items():
            actual = limits.get(name)
            if isinstance(actual, bool) or not isinstance(actual, int) or actual < minimum:
                raise LiteClientError(
                    f"{self.expected_alias} limit {name} must be at least {minimum}, "
                    f"received {actual!r}"
                )
        self.identity = ModelIdentity(
            alias=self.expected_alias,
            task=task,
            revision=revision,
            capabilities=capabilities,
            limits=limits,
            health_ms=timings["health"],
            ready_ms=timings["ready"],
            models_ms=timings["models"],
            preflight_completed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        return self.identity


class LiteEmbeddingClient(_BaseLiteClient):
    def __init__(
        self,
        base_url: str,
        expected_revision: str = QWEN_REVISION,
        http: httpx.Client | None = None,
    ) -> None:
        self._owns_http = http is None
        super().__init__(
            base_url,
            "ddu-embed",
            "embedding",
            expected_revision,
            http or httpx.Client(timeout=httpx.Timeout(60.0), trust_env=False),
            required_capabilities={
                "dimensions": EMBEDDING_DIMENSION,
                "normalized": True,
                "dtype": "float32",
                "input_types": ["query", "document"],
                "query_instruction_version": QWEN_QUERY_INSTRUCTION_VERSION,
            },
            minimum_limits={"max_batch_size": EMBEDDING_BATCH_SIZE},
        )

    def embed_queries(self, texts: list[str]) -> EmbeddingBatch:
        return self._embed(texts, "query")

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        return self._embed(texts, "document")

    def _embed(self, texts: list[str], input_type: str) -> EmbeddingBatch:
        if not texts or len(texts) > EMBEDDING_BATCH_SIZE:
            raise LiteClientError("Embedding batch must contain 1–8 strings")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise LiteClientError("Embedding inputs must be non-empty strings")
        if self.identity is None:
            self.preflight()
        started = time.perf_counter()
        response = self.http.post(
            f"{self.base_url}/v1/embeddings",
            json={
                "model": self.expected_alias,
                "input": texts,
                "input_type": input_type,
                "encoding_format": "float",
            },
        )
        response.raise_for_status()
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            document = response.json()
        except ValueError as error:
            raise LiteClientError("Embedding response is not JSON") from error
        if not isinstance(document, dict) or document.get("model") != self.expected_alias:
            raise LiteClientError("Embedding response model alias mismatch")
        records = document.get("data")
        if not isinstance(records, list) or len(records) != len(texts):
            raise LiteClientError("Embedding response count mismatch")
        if any(not isinstance(item, dict) for item in records):
            raise LiteClientError("Embedding response record is malformed")
        if [item.get("index") for item in records] != list(range(len(texts))):
            raise LiteClientError("Embedding response indexes are missing or out of order")

        vectors: list[list[float]] = []
        for item in records:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSION:
                raise LiteClientError("Embedding dimension mismatch")
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError, OverflowError) as error:
                raise LiteClientError("Embedding contains a non-numeric value") from error
            if not all(math.isfinite(value) for value in values):
                raise LiteClientError("Embedding contains a non-finite value")
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or abs(norm - 1.0) > 1e-4:
                raise LiteClientError("Embedding is not L2-normalized")
            vectors.append(values)

        usage = document.get("usage")
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if (
            not isinstance(tokens, int)
            or isinstance(tokens, bool)
            or tokens < 0
            or not isinstance(total_tokens, int)
            or isinstance(total_tokens, bool)
            or total_tokens < tokens
        ):
            raise LiteClientError("Embedding token usage is malformed")
        assert self.identity is not None
        if input_type == "query":
            instruction_version = self.identity.capabilities.get("query_instruction_version")
            if instruction_version != QWEN_QUERY_INSTRUCTION_VERSION:
                raise LiteClientError("Qwen query instruction profile mismatch")
        return EmbeddingBatch(
            vectors=vectors,
            model=QWEN_MODEL,
            revision=self.identity.revision,
            prompt_tokens=tokens,
            total_tokens=total_tokens,
            elapsed_ms=elapsed_ms,
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()


class LiteRerankerClient(_BaseLiteClient):
    def __init__(
        self,
        base_url: str,
        expected_revision: str = GTE_REVISION,
        http: httpx.Client | None = None,
    ) -> None:
        self._owns_http = http is None
        super().__init__(
            base_url,
            "ddu-rerank",
            "rerank",
            expected_revision,
            http or httpx.Client(timeout=httpx.Timeout(60.0), trust_env=False),
            required_capabilities={
                "score_semantics": "raw_ranking_signal",
                "code_revision": GTE_CODE_REVISION,
            },
            minimum_limits={"max_candidate_pairs": RERANKING_BATCH_SIZE},
        )

    def rerank(self, query: str, documents: list[str], top_n: int) -> RerankingBatch:
        if not query.strip() or not documents or len(documents) > RERANKING_BATCH_SIZE:
            raise LiteClientError("Reranking needs a query and 1–20 documents")
        if not 1 <= top_n <= len(documents):
            raise LiteClientError("top_n must be between 1 and the candidate count")
        if any(not isinstance(text, str) or not text.strip() for text in documents):
            raise LiteClientError("Reranking documents must be non-empty strings")
        if self.identity is None:
            self.preflight()
        started = time.perf_counter()
        response = self.http.post(
            f"{self.base_url}/v1/rerank",
            json={
                "model": self.expected_alias,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
        )
        response.raise_for_status()
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            document = response.json()
        except ValueError as error:
            raise LiteClientError("Reranking response is not JSON") from error
        if not isinstance(document, dict):
            raise LiteClientError("Reranking response must be an object")
        if document.get("model") != self.expected_alias:
            raise LiteClientError("Reranking response model alias mismatch")
        revision = document.get("revision")
        if revision != self.expected_revision or self.identity.revision != revision:
            raise LiteClientError("Reranking response revision mismatch")
        records = document.get("results")
        if not isinstance(records, list) or len(records) != top_n:
            raise LiteClientError("Reranking result count mismatch")
        ranked_indexes: list[int] = []
        scores_by_index: dict[int, float] = {}
        for item in records:
            if not isinstance(item, dict):
                raise LiteClientError("Reranking result record is malformed")
            index = item.get("index")
            score = item.get("relevance_score")
            if not isinstance(index, int) or not 0 <= index < len(documents):
                raise LiteClientError("Reranking index is outside the submitted pool")
            if index in scores_by_index:
                raise LiteClientError("Reranking response contains a duplicate index")
            try:
                numeric_score = float(score)
            except (TypeError, ValueError, OverflowError) as error:
                raise LiteClientError("Reranking score is not numeric") from error
            if not math.isfinite(numeric_score):
                raise LiteClientError("Reranking score is non-finite")
            ranked_indexes.append(index)
            scores_by_index[index] = numeric_score
        usage = document.get("usage")
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if (
            not isinstance(tokens, int)
            or isinstance(tokens, bool)
            or tokens < 0
            or not isinstance(total_tokens, int)
            or isinstance(total_tokens, bool)
            or total_tokens < tokens
        ):
            raise LiteClientError("Reranking token usage is malformed")
        assert self.identity is not None
        return RerankingBatch(
            ranked_indexes=ranked_indexes,
            scores_by_index=scores_by_index,
            model=GTE_MODEL,
            revision=revision,
            prompt_tokens=tokens,
            total_tokens=total_tokens,
            elapsed_ms=elapsed_ms,
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
