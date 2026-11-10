"""V2 model turns validate, commit, and reload through the durable API."""
import asyncio
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update

from app.ai.llm import StructuredCompletion
from app.core.settings import Settings
from app.db.connection import session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, TurnRequest
from app.game.turn_worker import process_next
from app.runtime.orchestrator import TurnOrchestrator


class ScriptedGenerator:
    model_alias = "test-narrator"

    def __init__(self, outputs, *, delay=0):
        self.outputs = list(outputs)
        self.delay = delay
        self.calls = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        output = self.outputs.pop(0)
        return StructuredCompletion(
            output=output,
            requested_model=self.model_alias,
            served_model=self.model_alias,
            model_revision="test-revision",
            provider_request_id=f"request-{len(self.calls)}",
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=20,
            raw_output_sha256="a" * 64,
            finish_reason="stop",
        )


class NoEvidenceRetriever:
    async def search(self, query, context, *, top_k):
        raise AssertionError("scenario fixture should not expose eligible source passages")


def proposal(role, objective):
    return {
        "schema_version": 1,
        "language": "da",
        "narration": "Du gennemgår materialet og beder om lønregnskaberne.",
        "dialogue": [
            {"actor_id": role.actor_id, "text": "Jeg finder de relevante mapper."}
        ],
        "outcome_summary": "Undersøgelsen fortsætter med nye spørgsmål.",
        "state_operations": [
            {"type": "record_decision", "summary": "Undersøg lønregnskaberne."},
            {
                "type": "open_thread",
                "thread_id": "wage_records",
                "summary": "Find lønregnskaberne.",
            },
            {
                "type": "record_simulation_event",
                "summary": "En simuleret undersøgelse går i gang.",
                "actor_ids": [role.actor_id],
            },
        ],
        "historical_claims": [],
        "author_context_references": [],
        "player_claims": [],
        "learning_observations": [
            {"objective_id": objective.id, "observation": "Spilleren efterspørger belæg."}
        ],
        "scene_status": "continue",
        "next_player_prompt": "Hvad vil du undersøge bagefter?",
    }


def quick_settings():
    return Settings(
        db_host="localhost",
        db_name="turn_test",
        db_user="turn_test",
        db_password="turn_test",
        turn_lease_seconds=2,
        turn_lease_renew_interval_seconds=0.2,
        turn_context_timeout_seconds=2,
        turn_inference_timeout_seconds=2,
        turn_total_timeout_seconds=6,
    )


@pytest.mark.asyncio
async def test_v2_turn_projects_committed_events_and_survives_reload(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    assert response.status_code == 202, response.text
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])]
    )
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )

    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.status_code == 200
    result = status.json()["result"]
    assert status.json()["status"] == "completed"
    assert result["turn_number"] == 1
    assert result["reply"] == "Du gennemgår materialet og beder om lønregnskaberne."
    assert result["provider"]["model_revision"] == "test-revision"

    resumed = await turn_harness.client.get(
        f"/api/v1/sessions/{session['id']}", headers=headers
    )
    assert resumed.json()["turn_number"] == 1
    assert resumed.json()["state"]["open_threads"][0]["id"] == "wage_records"
    assert resumed.json()["state"]["decisions"][0]["player_action"] == "Undersøg regnskaberne."
    history = await turn_harness.client.get(
        f"/api/v1/sessions/{session['id']}/turns", headers=headers
    )
    assert history.json()["turns"] == [result]

    async with session_factory() as db:
        committed = (
            await db.execute(
                select(CommittedTurn).where(CommittedTurn.request_id == UUID(status.json()["request_id"]))
            )
        ).scalar_one()
        logged = (
            await db.execute(
                select(GameEvent)
                .where(GameEvent.committed_turn_id == committed.id)
                .order_by(GameEvent.event_index)
            )
        ).scalars().all()
        stored_session = await db.get(GameSession, UUID(session["id"]))
    assert logged[0].event_type == "player_argument"
    assert [event.event_type for event in logged[1:]] == [
        "learning_observation_recorded",
        "decision_recorded",
        "thread_opened",
        "simulation_event_recorded",
    ]
    assert stored_session.state == result["state"]


@pytest.mark.asyncio
async def test_delayed_provider_outlives_initial_lease_but_only_commits_once(turn_harness):
    settings = quick_settings().model_copy(
        update={
            "turn_lease_seconds": 1,
            "turn_lease_renew_interval_seconds": 0.1,
            "turn_total_timeout_seconds": 5,
        }
    )
    session = await turn_harness.create_ready_session()
    turn = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])], delay=1.4
    )
    orchestrator = TurnOrchestrator(
        generator=generator, retrieval_service=NoEvidenceRetriever(), settings=settings
    )

    assert await process_next(orchestrator=orchestrator, settings=settings)
    request = await turn_harness.client.get(
        turn.headers["location"],
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert request.json()["status"] == "completed"
    async with session_factory() as db:
        assert len(
            (
                await db.execute(
                    select(CommittedTurn).where(
                        CommittedTurn.session_id == UUID(session["id"])
                    )
                )
            ).scalars().all()
        ) == 1


@pytest.mark.asyncio
async def test_invalid_output_uses_one_durable_repair_then_fails_without_mutation(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    invalid = {**proposal(role, turn_harness.definition.learning_objectives[0]), "extra": True}
    generator = ScriptedGenerator([invalid, invalid])
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )

    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "failed"
    assert status.json()["failure_code"] == "MODEL_OUTPUT_INVALID"
    assert len(generator.calls) == 2
    async with session_factory() as db:
        request = await db.get(TurnRequest, UUID(status.json()["request_id"]))
        stored = await db.get(GameSession, UUID(session["id"]))
        assert request.repair_used is True
        assert request.failure_code == "MODEL_OUTPUT_INVALID"
        assert stored.turn_number == 0
        assert stored.state == session["state"]


@pytest.mark.asyncio
async def test_missing_real_orchestrator_fails_retryably_without_a_canned_reply(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg situationen.")
    assert response.status_code == 202

    assert await process_next(settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "queued"
    assert status.json()["failure_code"] == "INFERENCE_UNAVAILABLE"
    assert status.json()["result"] is None
    async with session_factory() as db:
        stored = await db.get(GameSession, UUID(session["id"]))
        committed = (
            await db.execute(
                select(CommittedTurn).where(
                    CommittedTurn.session_id == UUID(session["id"])
                )
            )
        ).scalars().all()
    assert stored.turn_number == 0
    assert committed == []


@pytest.mark.asyncio
async def test_unreachable_real_provider_returns_retryable_unavailable(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg situationen.")
    assert response.status_code == 202

    class UnreachableGenerator:
        async def generate_structured(self, **kwargs):
            raise httpx.ConnectError("unreachable")

    orchestrator = TurnOrchestrator(
        generator=UnreachableGenerator(),
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    status = await turn_harness.client.get(
        response.headers["location"],
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert status.json()["status"] == "queued"
    assert status.json()["failure_code"] == "INFERENCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_duplicate_submission_and_provider_retry_create_one_turn(turn_harness):
    session = await turn_harness.create_ready_session()
    client_turn_id = UUID(int=17)
    first = await turn_harness.submit_turn(
        session,
        "Sammenlign lønnen med arbejdstiden.",
        client_turn_id=client_turn_id,
    )
    repeated = await turn_harness.submit_turn(
        session,
        "Sammenlign lønnen med arbejdstiden.",
        client_turn_id=client_turn_id,
    )
    assert first.status_code == repeated.status_code == 202
    assert first.json()["request_id"] == repeated.json()["request_id"]

    class BrokenGenerator:
        async def generate_structured(self, **kwargs):
            raise RuntimeError("provider is temporarily unavailable")

    orchestrator = TurnOrchestrator(
        generator=BrokenGenerator(),
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    failed_attempt = await turn_harness.client.get(first.headers["location"], headers=headers)
    assert failed_attempt.json()["status"] == "queued"
    assert failed_attempt.json()["failure_code"] == "PROVIDER_FAILURE"

    async with session_factory() as db, db.begin():
        await db.execute(
            update(TurnRequest)
            .where(TurnRequest.id == UUID(first.json()["request_id"]))
            .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
        )

    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])]
    )
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    completed = await turn_harness.client.get(first.headers["location"], headers=headers)
    assert completed.json()["status"] == "completed"
    async with session_factory() as db:
        committed = (
            await db.execute(
                select(CommittedTurn).where(
                    CommittedTurn.session_id == UUID(session["id"])
                )
            )
        ).scalars().all()
        assert len(committed) == 1
