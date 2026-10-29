"""Small provider boundary for semantic scenario proposals."""
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ScenarioTurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str
    scenario_version: str
    player_role_id: str | None
    state: dict
    definition: dict | None
    behavior_rules: dict | None
    evidence_policy: dict | None


class ScenarioInterpreter(Protocol):
    async def interpret(self, text: str, context: ScenarioTurnContext) -> dict:
        """Return a candidate proposal; the application validates every event."""


class ScriptedScenarioInterpreter:
    """Deterministic fake interpreter for reproducible negotiation tests."""

    def __init__(self, proposals: Mapping[str, dict]):
        self._proposals = dict(proposals)
        self.contexts: list[ScenarioTurnContext] = []

    async def interpret(self, text: str, context: ScenarioTurnContext) -> dict:
        self.contexts.append(context)
        return self._proposals.get(
            text,
            {"reply": "Forslaget er registreret til den kvalitative øvelse.", "events": []},
        )
