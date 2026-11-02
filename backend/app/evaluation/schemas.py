from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

Language = Literal["da", "en"]
QueryKind = Literal["retrospective_qa", "simulation"]
SplitName = Literal["pilot", "heldout"]
ReviewState = Literal["draft", "reviewed", "adjudicated"]
ReviewMethod = Literal["human", "agent", "mixed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRecord(StrictModel):
    source_id: str = Field(min_length=2, max_length=32)
    title: str = Field(min_length=1)
    institution: str = Field(min_length=1)
    url: HttpUrl
    document_created_on: date | None
    source_published_on: date | None
    accessed_on: date
    source_kind: Literal["primary", "secondary"]
    rights_note: str
    notes: str | None = None


class ActorKnowledge(StrictModel):
    actor_id: str = Field(min_length=1, max_length=80)
    available_on: date
    unavailable_after: date | None = None
    basis: str = Field(min_length=1)


class PassageRecord(StrictModel):
    passage_id: str = Field(min_length=2, max_length=64)
    source_id: str = Field(min_length=2, max_length=32)
    text: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor: str = Field(min_length=1)
    source_document_created_on: date | None
    source_published_on: date | None
    actor_knowledge: list[ActorKnowledge] = Field(default_factory=list)
    text_kind: Literal["source_text", "editorial_note"] = "source_text"


class EvaluationQuery(StrictModel):
    query_id: str = Field(min_length=2, max_length=64)
    split: SplitName
    language: Language
    text: str = Field(min_length=3, max_length=1000)
    topic: str = Field(min_length=1, max_length=100)
    kind: QueryKind
    answerable: bool
    actor_id: str | None = None
    as_of: date | None = None
    source_creation_cutoff: date | None = None
    publication_cutoff: date | None = None
    notes: str | None = None


class EvidenceGroup(StrictModel):
    group_id: str = Field(min_length=1, max_length=64)
    supporting_passage_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1)


class QueryJudgment(StrictModel):
    query_id: str = Field(min_length=2, max_length=64)
    relevance: dict[str, int] = Field(default_factory=dict)
    required_evidence_groups: list[EvidenceGroup] = Field(default_factory=list)
    review_state: ReviewState = "draft"
    review_method: ReviewMethod | None = None
    reviewer_ids: list[str] = Field(default_factory=list)
    adjudication_note: str | None = None

    @model_validator(mode="after")
    def require_unique_evidence_groups(self) -> "QueryJudgment":
        group_ids = [group.group_id for group in self.required_evidence_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("required evidence group IDs must be unique")
        for group in self.required_evidence_groups:
            if len(group.supporting_passage_ids) != len(set(group.supporting_passage_ids)):
                raise ValueError(f"passage IDs must be unique within evidence group {group.group_id}")
        return self


class ModelPins(StrictModel):
    embedding_model: str
    embedding_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    embedding_query_instruction_version: str
    reranker_model: str
    reranker_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    reranker_code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class SplitTarget(StrictModel):
    target_queries: int = Field(ge=0)
    actual_queries: int = Field(ge=0)
    reviewed_queries: int = Field(ge=0)


class EvaluationManifest(StrictModel):
    schema_version: int = Field(ge=1)
    dataset_version: str = Field(min_length=1)
    status: Literal["draft", "reviewed"]
    bm25_k1: float = Field(gt=0)
    bm25_b: float = Field(ge=0, le=1)
    rrf_k: int = Field(gt=0)
    candidate_pool_size: int = Field(ge=1, le=20)
    targets: dict[SplitName, SplitTarget]
    model_pins: ModelPins
    source_review_state: ReviewState
    source_review_method: ReviewMethod | None = None
    source_reviewer_ids: list[str] = Field(default_factory=list)
    judgment_scale: list[int] = Field(min_length=4, max_length=4)
    file_hashes: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_both_splits_and_grades(self) -> "EvaluationManifest":
        if set(self.targets) != {"pilot", "heldout"}:
            raise ValueError("targets must define exactly the pilot and heldout splits")
        if self.judgment_scale != [0, 1, 2, 3]:
            raise ValueError("judgment_scale must be exactly [0, 1, 2, 3]")
        return self


class EvaluationDataset(StrictModel):
    manifest: EvaluationManifest
    sources: dict[str, SourceRecord]
    passages: dict[str, PassageRecord]
    queries: list[EvaluationQuery]
    judgments: dict[str, QueryJudgment]
    split: SplitName


class ValidationSummary(StrictModel):
    split: SplitName | None = None
    source_count: int
    passage_count: int
    pilot_query_count: int
    heldout_query_count: int
    judgment_count: int
    reviewed_judgment_count: int
    judged_query_coverage: float
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors
