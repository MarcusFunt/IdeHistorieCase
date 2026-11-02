"""Bounded labeled retrieval evaluation manifest validation."""
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from app.rag.schemas import StrictModel

EvaluationLabel = Literal[
    "answerable",
    "actor_visibility",
    "date_leak",
    "confidentiality",
    "rights",
    "unsupported_claim",
]


class SimulatedReview(StrictModel):
    review_status: Literal["approved", "rejected", "revoked"]
    rights_status: Literal["approved", "pending", "denied", "restricted"]


class EvaluationCase(StrictModel):
    id: str = Field(min_length=2, max_length=96, pattern=r"^[a-z][a-z0-9_-]{1,95}$")
    labels: list[EvaluationLabel] = Field(min_length=1, max_length=6)
    query: str = Field(min_length=1, max_length=2000)
    actor_id: str | None = Field(default=None, max_length=64)
    world_date: date
    mode: Literal["gameplay", "postgame", "evaluator"]
    expected_status: Literal["ok", "RAG_NO_EVIDENCE"]
    expected_passage_keys: list[str] = Field(max_length=10)
    blocked_passage_keys: list[str] = Field(default_factory=list, max_length=10)
    simulated_review: SimulatedReview

    @model_validator(mode="after")
    def validate_expected_retrieval(self):
        if len(self.labels) != len(set(self.labels)):
            raise ValueError("evaluation labels must be unique")
        if len(self.expected_passage_keys) != len(set(self.expected_passage_keys)):
            raise ValueError("expected passage keys must be unique")
        if len(self.blocked_passage_keys) != len(set(self.blocked_passage_keys)):
            raise ValueError("blocked passage keys must be unique")
        if set(self.expected_passage_keys) & set(self.blocked_passage_keys):
            raise ValueError("a passage cannot be both expected and blocked")
        if (self.expected_status == "ok") != bool(self.expected_passage_keys):
            raise ValueError("ok cases require expected passages; no-evidence cases require none")
        if set(self.labels) & {"date_leak", "confidentiality", "rights", "unsupported_claim"}:
            if self.expected_status != "RAG_NO_EVIDENCE" or not self.blocked_passage_keys:
                raise ValueError("negative evaluation cases require abstention and blocked passages")
        return self


class RagEvaluationSet(StrictModel):
    schema_version: Literal[1]
    scenario_slug: str = Field(min_length=1, max_length=80)
    scenario_version: str = Field(min_length=1, max_length=32)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_unique_case_ids(self):
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case IDs must be unique")
        return self


def load_evaluation_set(path: Path) -> RagEvaluationSet:
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("evaluation manifest exceeds maximum of 262144 bytes")
    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"duplicate YAML key: {key}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    for event in yaml.parse(raw):
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
            raise ValueError("YAML aliases and anchors are not allowed")
    return RagEvaluationSet.model_validate(yaml.load(raw, Loader=UniqueKeyLoader))
