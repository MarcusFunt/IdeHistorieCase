"""Strict authoring contract for open-ended, schema-v2 scenarios."""
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from app.scenarios.schema import Id, LongText, ShortText, SourceId, StrictModel, Text

StableToken = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]


class HistoricalPeriodV2(StrictModel):
    start: date
    end: date


class ScenarioMetadataV2(StrictModel):
    title: ShortText
    language: Literal["da"]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    subject: ShortText
    intended_audience: ShortText
    historical_period: HistoricalPeriodV2


class HistoricalContextV2(StrictModel):
    introduction: LongText
    counterfactual_disclaimer: Text


class ActorDefinitionV2(StrictModel):
    id: Id
    name: ShortText
    description: Text
    motivations: list[ShortText] = Field(default_factory=list, max_length=32)
    knowledge_constraints: list[Text] = Field(default_factory=list, max_length=32)


class RoleDefinitionV2(StrictModel):
    id: Id
    actor_id: Id
    title: ShortText
    playable: StrictBool
    introduction: Text
    knowledge_constraints: list[Text] = Field(default_factory=list, max_length=32)


class LearningObjectiveV2(StrictModel):
    id: Id
    description: Text
    concepts: list[ShortText] = Field(default_factory=list, max_length=32)
    observable_behaviors: list[Text] = Field(min_length=1, max_length=16)


class ChronologyV2(StrictModel):
    opening_date: date
    max_days_per_advance: StrictInt = Field(ge=1, le=365)


class InteractionGuidanceV2(StrictModel):
    style: StableToken
    freedom: Literal["open_ended"]
    emphasize: list[ShortText] = Field(min_length=1, max_length=32)


class ContentItemV2(StrictModel):
    key: Id
    kind: StableToken
    file: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    mode: Literal["gameplay", "postgame"]
    visibility: list[Id] = Field(default_factory=list, max_length=50)
    review_status: Literal["draft", "author_reviewed"]
    availability_date: date | None = None
    world_valid_from: date | None = None
    world_valid_to: date | None = None
    source_refs: list[SourceId] = Field(default_factory=list, max_length=256)


class CompletionPolicyV2(StrictModel):
    mode: Literal["goal_or_player_end", "objectives_complete", "player_end"]
    min_meaningful_decisions: StrictInt = Field(ge=0, le=100)
    max_turns: StrictInt = Field(ge=1, le=500)
    objective_ids: list[Id] = Field(default_factory=list, max_length=50)


class OptionalBeatV2(StrictModel):
    id: Id
    title: ShortText
    description: Text
    objective_ids: list[Id] = Field(default_factory=list, max_length=50)


class ScenarioDefinitionV2(StrictModel):
    schema_version: Literal[2]
    slug: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=80,
            pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        ),
    ]
    version: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    metadata: ScenarioMetadataV2
    historical_context: HistoricalContextV2
    actors: list[ActorDefinitionV2] = Field(min_length=1, max_length=50)
    roles: list[RoleDefinitionV2] = Field(min_length=1, max_length=50)
    default_role_id: Id
    learning_objectives: list[LearningObjectiveV2] = Field(min_length=1, max_length=50)
    chronology: ChronologyV2
    interaction_guidance: InteractionGuidanceV2
    content_items: list[ContentItemV2] = Field(min_length=1, max_length=128)
    completion_policy: CompletionPolicyV2
    optional_beats: list[OptionalBeatV2] = Field(default_factory=list, max_length=100)
    source_refs: list[SourceId] = Field(max_length=256)

    @model_validator(mode="after")
    def unique_local_lists(self):
        _require_unique(self.source_refs, "source_refs")
        for item in self.content_items:
            _require_unique(item.visibility, f"content item {item.key!r} visibility")
            _require_unique(item.source_refs, f"content item {item.key!r} source_refs")
        for objective in self.learning_objectives:
            _require_unique(objective.concepts, f"learning objective {objective.id!r} concepts")
        _require_unique(self.interaction_guidance.emphasize, "interaction_guidance.emphasize")
        for beat in self.optional_beats:
            _require_unique(beat.objective_ids, f"optional beat {beat.id!r} objective_ids")
        _require_unique(self.completion_policy.objective_ids, "completion_policy.objective_ids")
        return self


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
