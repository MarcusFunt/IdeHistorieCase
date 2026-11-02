"""Strict request and corpus schemas for the historical RAG boundary."""
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_default=True)


class CorpusPassage(StrictModel):
    key: str = Field(min_length=2, max_length=96, pattern=r"^[a-z][a-z0-9_-]{1,95}$")
    source_identifier: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z][A-Z0-9_-]{1,31}$")
    source_title: str = Field(min_length=1, max_length=240)
    source_url: str = Field(min_length=10, max_length=2048)
    source_anchor: str = Field(min_length=1, max_length=240)
    passage_text: str = Field(min_length=1, max_length=4000)
    passage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_date: date
    in_world_available_from: date | None
    postgame_only: bool = False
    actor_ids: list[str] = Field(max_length=32)
    visibility_scope: Literal["public", "actor_limited", "confidential", "postgame"]
    rights_basis: str = Field(min_length=1, max_length=1000)
    review_status: Literal["pending"] = "pending"
    rights_status: Literal["pending"] = "pending"

    @field_validator("source_url")
    @classmethod
    def require_credential_free_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source_url must be a credential-free HTTPS URL")
        return value

    @field_validator("actor_ids")
    @classmethod
    def require_unique_actor_ids(cls, value: list[str]) -> list[str]:
        if any(not actor or len(actor) > 64 for actor in value) or len(value) != len(set(value)):
            raise ValueError("actor_ids must contain unique nonempty actor IDs")
        return value

    @model_validator(mode="after")
    def check_passage_hash_and_availability(self):
        import hashlib

        actual = hashlib.sha256(self.passage_text.encode("utf-8")).hexdigest()
        if self.passage_sha256 != actual:
            raise ValueError("passage SHA-256 does not match exact UTF-8 passage_text")
        if self.postgame_only:
            if self.in_world_available_from is not None or self.visibility_scope != "postgame":
                raise ValueError("postgame-only passages require postgame scope and no in-world date")
        else:
            if not self.actor_ids:
                raise ValueError("in-world passages require at least one visible actor")
            if (
                self.in_world_available_from is None
                or self.in_world_available_from < self.document_date
            ):
                raise ValueError("in-world availability cannot precede the document date")
            if self.visibility_scope == "postgame":
                raise ValueError("postgame scope requires postgame_only=true")
        return self


class RagCorpus(StrictModel):
    schema_version: Literal[1]
    scenario_slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scenario_version: str = Field(min_length=1, max_length=32)
    phase_world_dates: dict[str, date] = Field(min_length=1, max_length=50)
    passages: list[CorpusPassage] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def check_unique_bounded_passages(self):
        keys = [passage.key for passage in self.passages]
        if len(keys) != len(set(keys)):
            raise ValueError("passage keys must be unique within a scenario version")
        total_chars = sum(len(passage.passage_text) for passage in self.passages)
        if total_chars > 64_000:
            raise ValueError("corpus exceeds maximum of 64000 passage characters")
        return self


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value
