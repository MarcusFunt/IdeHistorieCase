"""Strict opening validation and real-provider orchestration contract."""
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.ai.llm import StructuredCompletion, StructuredOutputError
from app.runtime.context import SessionPromptContext
from app.runtime.initializer import (
    OPENING_PROMPT_VERSION,
    OPENING_SCHEMA_VERSION,
    OpeningRejected,
    generate_opening,
)
from app.scenarios.loader import load_scenario

RELEASE = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "3.0-ai.yaml"


class ScriptedGenerator:
    model_alias = "ddu-narrator"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        result = self.outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def fixtures():
    definition = load_scenario(RELEASE).definition
    role = next(role for role in definition.roles if role.id == definition.default_role_id)
    evidence_id = "a6a00000-0000-4000-8000-000000000001"
    context = SessionPromptContext(
        payload={
            "pinned_scenario": {
                "slug": definition.slug,
                "version": definition.version,
                "schema_version": 2,
                "release_id": "ab4f0000-0000-4000-8000-000000000001",
            },
            "selected_role": {"id": role.id, "actor_id": role.actor_id, "title": role.title},
            "learning_objectives": [item.model_dump(mode="json") for item in definition.learning_objectives],
            "evidence": {
                "status": "evidence_included",
                "passages": [
                    {
                        "passage_id": evidence_id,
                        "passage_sha256": "b" * 64,
                        "citation_ready": True,
                        "review": {"revision": 3},
                    }
                ],
            },
        },
        world_date=definition.chronology.opening_date,
        evidence_status="evidence_included",
        serialized_chars=1000,
        stage="initialization",
        mode="gameplay",
        metadata={"evidence": {"status": "evidence_included"}},
    )
    proposal = {
        "language": "da",
        "role_id": role.id,
        "world_date": definition.chronology.opening_date.isoformat(),
        "title": "Et afgørende møde",
        "scene": "Delegationerne samles for at drøfte de næste skridt.",
        "immediate_tension": "Sikkerhed og genopbygning trækker i hver sin retning.",
        "player_goal": "Find et forslag, der kan samle parterne.",
        "player_prompt": "Hvad vil du foreslå?",
        "present_actor_ids": [role.actor_id],
        "objective_ids": [definition.learning_objectives[0].id],
        "simulation_label": "simulated",
        "unresolved_threads": ["Hvordan kan garantierne håndhæves?"],
        "historical_claims": [
            {
                "claim": "En godkendt kilde belyser forhandlingernes rammer.",
                "evidence_ids": [evidence_id],
            }
        ],
    }
    return definition, role, context, proposal


def completion(payload, *, output_hash=None):
    return StructuredCompletion(
        output=payload,
        requested_model="ddu-narrator",
        served_model="ddu-narrator",
        model_revision=None,
        provider_request_id="req-opening-1",
        prompt_tokens=420,
        completion_tokens=180,
        duration_ms=1250,
        raw_output_sha256=output_hash or sha256(b"valid opening json").hexdigest(),
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_valid_opening_is_pinned_and_initial_state_is_derived():
    definition, role, context, proposal = fixtures()
    session_id = uuid4()
    generator = ScriptedGenerator([completion(proposal)])

    async def authorize_repair():
        raise AssertionError("a valid opening must not request repair")

    result = await generate_opening(
        generator,
        definition=definition,
        context=context,
        role_id=role.id,
        session_id=session_id,
        attempt_number=1,
        repair_used=False,
        authorize_repair=authorize_repair,
    )

    assert result.opening["world_date"] == definition.chronology.opening_date.isoformat()
    assert result.opening["simulation_label"] == "simulated"
    assert result.initial_state["world_date"] == context.world_date.isoformat()
    assert result.initial_state["present_actor_ids"] == [role.actor_id]
    assert result.initial_state["open_threads"][0]["status"] == "open"
    assert result.provenance["prompt_version"] == OPENING_PROMPT_VERSION
    assert result.provenance["schema_version"] == OPENING_SCHEMA_VERSION
    assert result.provenance["eligible_evidence"][0]["passage_id"] == (
        context.payload["evidence"]["passages"][0]["passage_id"]
    )
    assert len(generator.calls) == 1
    assert generator.calls[0]["enable_thinking"] is None
    assert generator.calls[0]["max_tokens"] == 5632
    assert "opening_authorizations" in generator.calls[0]["user_prompt"]
    assert role.actor_id in generator.calls[0]["user_prompt"]


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        (lambda data: data.update(role_id="unexpected_role"), "role_id_mismatch"),
        (lambda data: data.update(present_actor_ids=["unknown-actor"]), "unknown_actor_id"),
        (lambda data: data.update(world_date="1919-03-01"), "opening_date_mismatch"),
        (
            lambda data: data["historical_claims"][0].update(
                evidence_ids=["a6a00000-0000-4000-8000-000000000099"]
            ),
            "unsupported_evidence_id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_opening_is_repaired_once_with_bounded_validation_errors(
    change, expected_code
):
    definition, role, context, proposal = fixtures()
    invalid = {**proposal, "historical_claims": [dict(item) for item in proposal["historical_claims"]]}
    change(invalid)
    generator = ScriptedGenerator([completion(invalid), completion(proposal)])
    repairs = 0

    async def authorize_repair():
        nonlocal repairs
        repairs += 1
        return True

    result = await generate_opening(
        generator,
        definition=definition,
        context=context,
        role_id=role.id,
        session_id=uuid4(),
        attempt_number=1,
        repair_used=False,
        authorize_repair=authorize_repair,
    )

    assert result.opening["role_id"] == role.id
    assert repairs == 1
    assert len(generator.calls) == 2
    assert generator.calls[0]["enable_thinking"] is None
    assert generator.calls[1]["enable_thinking"] is False
    repair_prompt = generator.calls[1]["user_prompt"]
    assert expected_code in repair_prompt
    if expected_code == "role_id_mismatch":
        assert f"role_id skal være præcis {role.id}" in repair_prompt
    assert len(repair_prompt) < 20_000


@pytest.mark.asyncio
async def test_schema_repair_prompt_names_safe_missing_field_path():
    definition, role, context, proposal = fixtures()
    invalid = dict(proposal)
    invalid.pop("player_prompt")
    generator = ScriptedGenerator([completion(invalid), completion(proposal)])

    async def authorize_repair():
        return True

    await generate_opening(
        generator,
        definition=definition,
        context=context,
        role_id=role.id,
        session_id=uuid4(),
        attempt_number=1,
        repair_used=False,
        authorize_repair=authorize_repair,
    )

    assert "schema_invalid:player_prompt:missing" in generator.calls[1]["user_prompt"]


@pytest.mark.asyncio
async def test_truncated_completion_can_use_the_single_repair():
    definition, role, context, proposal = fixtures()
    generator = ScriptedGenerator(
        [StructuredOutputError("truncated_output", raw_output='{"scene":"cut'), completion(proposal)]
    )
    repairs = 0

    async def authorize_repair():
        nonlocal repairs
        repairs += 1
        return True

    result = await generate_opening(
        generator,
        definition=definition,
        context=context,
        role_id=role.id,
        session_id=uuid4(),
        attempt_number=2,
        repair_used=False,
        authorize_repair=authorize_repair,
    )

    assert result.opening["title"] == proposal["title"]
    assert repairs == 1
    assert result.attempt_metadata[0]["output_sha256"] == sha256(
        b'{"scene":"cut'
    ).hexdigest()


@pytest.mark.asyncio
async def test_invalid_repair_is_terminal_and_does_not_request_another_repair():
    definition, role, context, proposal = fixtures()
    invalid = {**proposal, "role_id": "wrong-role"}
    generator = ScriptedGenerator([completion(invalid), completion(invalid)])
    repairs = 0

    async def authorize_repair():
        nonlocal repairs
        repairs += 1
        return True

    with pytest.raises(OpeningRejected) as rejected:
        await generate_opening(
            generator,
            definition=definition,
            context=context,
            role_id=role.id,
            session_id=uuid4(),
            attempt_number=1,
            repair_used=False,
            authorize_repair=authorize_repair,
        )

    assert rejected.value.failure_code == "MODEL_OUTPUT_INVALID"
    assert repairs == 1
    assert len(generator.calls) == 2


@pytest.mark.asyncio
async def test_already_used_repair_budget_fails_without_another_model_call():
    definition, role, context, proposal = fixtures()
    generator = ScriptedGenerator([completion({**proposal, "world_date": "1919-03-01"})])
    authorize_repair = AsyncMock(return_value=True)

    with pytest.raises(OpeningRejected) as rejected:
        await generate_opening(
            generator,
            definition=definition,
            context=context,
            role_id=role.id,
            session_id=uuid4(),
            attempt_number=4,
            repair_used=True,
            authorize_repair=authorize_repair,
        )

    assert rejected.value.failure_code == "MODEL_OUTPUT_INVALID"
    assert not authorize_repair.await_count
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_provider_unavailability_is_not_misreported_as_invalid_output():
    definition, role, context, _proposal = fixtures()
    generator = ScriptedGenerator([httpx.ConnectError("offline")])
    authorize_repair = AsyncMock(return_value=True)

    with pytest.raises(httpx.ConnectError):
        await generate_opening(
            generator,
            definition=definition,
            context=context,
            role_id=role.id,
            session_id=uuid4(),
            attempt_number=1,
            repair_used=False,
            authorize_repair=authorize_repair,
        )

    assert not authorize_repair.await_count
