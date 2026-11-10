"""Strict model proposal schema for a single schema-v2 session opening."""
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from app.scenarios.schema import Id, ShortText, StrictModel, Text

OpeningDate = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]


class HistoricalClaimProposal(StrictModel):
    """A factual statement explicitly tied to passages supplied for this opening."""

    claim: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    evidence_ids: list[Annotated[str, StringConstraints(min_length=36, max_length=36)]] = Field(
        min_length=1, max_length=4
    )

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("evidence_ids must be unique")
        return values


class OpeningProposal(StrictModel):
    """Bounded narrative output; initial session state is derived by application code."""

    language: Literal["da"]
    role_id: Id
    world_date: OpeningDate
    title: ShortText
    scene: Text
    immediate_tension: Text
    player_goal: Text
    player_prompt: Text
    present_actor_ids: list[Id] = Field(min_length=1, max_length=16)
    objective_ids: list[Id] = Field(min_length=1, max_length=20)
    simulation_label: Literal["simulated"]
    unresolved_threads: list[ShortText] = Field(max_length=10)
    historical_claims: list[HistoricalClaimProposal] = Field(max_length=10)

    @field_validator("world_date")
    @classmethod
    def valid_iso_calendar_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("world_date must be a valid ISO calendar date") from exc
        if parsed.isoformat() != value:
            raise ValueError("world_date must be a canonical ISO calendar date")
        return value

    @field_validator("present_actor_ids", "objective_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("IDs must be unique")
        return values
