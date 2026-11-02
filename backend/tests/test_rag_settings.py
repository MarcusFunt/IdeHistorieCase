"""RAG model service settings remain private HTTP endpoints and fail closed."""

import pytest
from pydantic import ValidationError

from app.core.settings import Settings


@pytest.mark.parametrize("url", ["", "ftp://embed:8080", "http://", "http://u:p@embed:8080"])
def test_embedding_service_url_must_be_valid_http_without_credentials(url):
    with pytest.raises(ValidationError, match="rag_embed_base_url"):
        Settings(rag_embed_base_url=url)


def test_reranker_cannot_be_enabled_without_a_service_url():
    with pytest.raises(ValidationError, match="rag_rerank_base_url"):
        Settings(rag_rerank_enabled=True, rag_rerank_base_url=None)


def test_reranker_url_is_optional_when_reranking_is_disabled():
    settings = Settings(rag_rerank_enabled=False, rag_rerank_base_url=None)

    assert settings.rag_rerank_enabled is False
    assert settings.rag_rerank_base_url is None


def test_default_retrieval_model_identities_include_the_reviewed_revisions():
    settings = Settings()

    assert settings.rag_embed_model_id == (
        "Qwen/Qwen3-Embedding-0.6B@97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    )
    assert settings.rag_rerank_model_id == (
        "Alibaba-NLP/gte-multilingual-reranker-base@8215cf04918ba6f7b6a62bb44238ce2953d8831c"
        "+code@40ced75c3017eb27626c9d4ea981bde21a2662f4"
    )
