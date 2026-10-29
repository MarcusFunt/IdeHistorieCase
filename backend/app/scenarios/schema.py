"""Typed authoring contract for immutable historical scenario manifests."""
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
)

Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=32, pattern=r"^[A-Z][A-Z0-9_-]{1,31}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_default=True)


class HistoricalPeriod(StrictModel):
    start_year: StrictInt = Field(ge=1, le=9999)
    end_year: StrictInt = Field(ge=1, le=9999)


class ScenarioMetadata(StrictModel):
    title: ShortText
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    default_language: Literal["da"]
    supported_languages: list[Literal["da"]] = Field(min_length=1, max_length=1)
    historical_period: HistoricalPeriod
    subject: ShortText
    tags: list[ShortText] = Field(min_length=1, max_length=32)
    intended_audience: ShortText

    @field_validator("supported_languages", "tags")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("values must be unique")
        return values

    @field_validator("supported_languages")
    @classmethod
    def default_language_supported(cls, values: list[str], info):
        if info.data.get("default_language") not in values:
            raise ValueError("default_language must occur in supported_languages")
        return values


class HistoricalContext(StrictModel):
    introduction: LongText
    counterfactual_disclaimer: Text


class ActorDefinition(StrictModel):
    id: Id
    name: ShortText
    description: Text


class RoleDefinition(StrictModel):
    id: Id
    actor_id: Id
    title: ShortText
    playable: StrictBool
    introduction: Text


class LearningObjective(StrictModel):
    id: Id
    description: Text
    concepts: list[ShortText] = Field(min_length=1, max_length=32)
    observable_behaviors: list[Text] = Field(min_length=1, max_length=16)


class StateVariableBase(StrictModel):
    id: Id
    description: Text


class IntegerMetricDefinition(StateVariableBase):
    kind: Literal["integer_metric"]
    initial: StrictInt
    min_value: StrictInt
    max_value: StrictInt

    @field_validator("max_value")
    @classmethod
    def valid_range(cls, value: int, info) -> int:
        minimum = info.data.get("min_value")
        initial = info.data.get("initial")
        if minimum is not None and minimum > value:
            raise ValueError("range requires min_value <= max_value")
        if minimum is not None and initial is not None and not minimum <= initial <= value:
            raise ValueError("range requires min_value <= initial <= max_value")
        return value


class BooleanFlagDefinition(StateVariableBase):
    kind: Literal["boolean_flag"]
    initial: StrictBool


StateVariableDefinition = Annotated[
    Union[IntegerMetricDefinition, BooleanFlagDefinition], Field(discriminator="kind")
]


class PhaseDefinition(StrictModel):
    id: Id
    title: ShortText
    is_start: StrictBool
    is_terminal: StrictBool
    allowed_next_phase_ids: list[Id] = Field(max_length=32)

    @field_validator("allowed_next_phase_ids")
    @classmethod
    def unique_next_phases(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("allowed_next_phase_ids must be unique")
        return values


class DilemmaDefinition(StrictModel):
    id: Id
    phase_id: Id
    prompt: Text
    relevant_actor_ids: list[Id] = Field(min_length=1, max_length=32)
    learning_objective_ids: list[Id] = Field(min_length=1, max_length=32)

    @field_validator("relevant_actor_ids", "learning_objective_ids")
    @classmethod
    def unique_references(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("references must be unique")
        return values


class AdjustMetricPermission(StrictModel):
    type: Literal["adjust_metric"]
    target_id: Id
    min_delta: StrictInt
    max_delta: StrictInt

    @field_validator("max_delta")
    @classmethod
    def valid_delta_range(cls, value: int, info) -> int:
        minimum = info.data.get("min_delta")
        if minimum is not None and minimum > value:
            raise ValueError("min_delta must be <= max_delta")
        return value


class SetFlagPermission(StrictModel):
    type: Literal["set_flag"]
    target_id: Id


class AdvancePhasePermission(StrictModel):
    type: Literal["advance_phase"]


AllowedEventDefinition = Annotated[
    Union[AdjustMetricPermission, SetFlagPermission, AdvancePhasePermission],
    Field(discriminator="type"),
]


class ScenarioDefinition(StrictModel):
    schema_version: Literal[1]
    slug: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    version: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    metadata: ScenarioMetadata
    historical_context: HistoricalContext
    actors: list[ActorDefinition] = Field(min_length=1, max_length=50)
    roles: list[RoleDefinition] = Field(min_length=1, max_length=50)
    default_role_id: Id
    learning_objectives: list[LearningObjective] = Field(min_length=1, max_length=50)
    state_variables: list[StateVariableDefinition] = Field(max_length=64)
    phases: list[PhaseDefinition] = Field(min_length=1, max_length=50)
    dilemmas: list[DilemmaDefinition] = Field(min_length=1, max_length=100)
    allowed_events: list[AllowedEventDefinition] = Field(max_length=128)
    source_refs: list[SourceId] = Field(max_length=256)

    @field_validator("source_refs")
    @classmethod
    def unique_source_refs(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("source_refs must be unique")
        return values
