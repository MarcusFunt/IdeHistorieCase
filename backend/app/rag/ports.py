"""Injected model-service contracts used by ingestion and retrieval."""
from collections.abc import Sequence
from typing import Protocol


class DocumentEmbedder(Protocol):
    model_id: str

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one 1024-dimensional vector per input text, in the same order."""

    async def embed_query(self, text: str) -> list[float]:
        """Return one 1024-dimensional query vector."""


class PassageReranker(Protocol):
    model_id: str

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """Return one finite score per passage, in input order."""
