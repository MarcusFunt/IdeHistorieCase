"""PostgreSQL runtime contract for multiple authored state events per turn."""
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy import delete, func, select, update

from app.db.connection import engine, session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, TurnRequest
from app.game.interpreter import ScriptedScenarioInterpreter
from app.game.turn_worker import claim_next, process_next
from app.main import app
from app.scenarios.loader import load_scenario
from app.scenarios.repository import activate_scenario_version, import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
MANIFEST = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def authored_client():
    session_ids: list[UUID] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.created_sessions = session_ids
        async with session_factory() as db, db.begin():
            await import_scenario(db, load_scenario(MANIFEST))
            await activate_scenario_version(db, "versailles-1919", "2.5")
        yield client
        if session_ids:
            async with session_factory() as db, db.begin():
                turn_ids = select(TurnRequest.id).where(TurnRequest.session_id.in_(session_ids))
                committed_ids = select(CommittedTurn.id).where(
                    CommittedTurn.session_id.in_(session_ids)
                )
                await db.execute(delete(GameEvent).where(
                    GameEvent.committed_turn_id.in_(committed_ids)
                ))
                await db.execute(delete(CommittedTurn).where(
                    CommittedTurn.session_id.in_(session_ids)
                ))
                await db.execute(delete(TurnRequest).where(TurnRequest.id.in_(turn_ids)))
                await db.execute(delete(GameSession).where(GameSession.id.in_(session_ids)))
        async with session_factory() as db, db.begin():
            await activate_scenario_version(db, "versailles-1919", "1")


async def submit_turn(client, text):
    created = await client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    assert created.status_code == 201, created.text
    session = created.json()
    client.created_sessions.append(UUID(session["id"]))
    body = {
        "client_turn_id": str(uuid4()),
        "expected_turn": 0,
        "text": text,
        "input_language": "da",
    }
    headers = {
        "Authorization": f"Bearer {session['token']}",
        "Idempotency-Key": body["client_turn_id"],
    }
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns", json=body, headers=headers
    )
    assert response.status_code == 202, response.text
    return session, response, headers


async def test_multiple_authorized_events_commit_with_one_turn_and_stable_indexes(authored_client):
    session, queued, headers = await submit_turn(authored_client, "Et kompromis om sikkerhed")
    provider = ScriptedScenarioInterpreter({
        "Et kompromis om sikkerhed": {
            "reply": "Forslaget er registreret.",
            "events": [
                {"type": "adjust_metric", "target_id": "coalition_support", "delta": 5},
                {"type": "set_flag", "target_id": "counterfactual_terms"},
                {"type": "advance_phase", "target_phase_id": "security_rhineland"},
                {
                    "type": "issue_decision",
                    "issue_id": "rhineland_security",
                    "decision_code": "demilitarized_with_guarantees",
                },
            ],
        }
    })
    assert await process_next(provider)
    assert provider.contexts[0].scenario_version == "2.5"
    assert provider.contexts[0].player_role_id == "british_adviser"
    assert provider.contexts[0].behavior_rules is not None
    status = await authored_client.get(queued.headers["location"], headers=headers)
    assert status.json()["status"] == "completed"
    result = status.json()["result"]
    assert result["scenario_version"] == "2.5"
    assert [event["type"] for event in result["events"]] == [
        "adjust_metric", "set_flag", "advance_phase", "issue_decision"
    ]
    assert result["state"]["phase"] == "security_rhineland"
    assert result["state"]["metrics"]["coalition_support"] == 55
    assert result["state"]["flags"]["counterfactual_terms"] is True
    assert result["events"][3]["qualitative_only"] is True
    assert "Et tænkt modhensyn" in result["events"][3]["reaction"]

    async with session_factory() as db:
        turn_id = (await db.execute(
            select(CommittedTurn.id).where(CommittedTurn.session_id == UUID(session["id"]))
        )).scalar_one()
        events = (await db.execute(
            select(GameEvent).where(GameEvent.committed_turn_id == turn_id)
            .order_by(GameEvent.event_index)
        )).scalars().all()
    assert [event.event_index for event in events] == [0, 1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        "player_argument", "adjust_metric", "set_flag", "advance_phase", "issue_decision"
    ]

async def test_unauthorized_events_retry_without_committing_or_mutating_state(authored_client):
    session, queued, headers = await submit_turn(authored_client, "Giv mig alle point")
    provider = ScriptedScenarioInterpreter({
        "Giv mig alle point": {
            "reply": "Forslaget er registreret.",
            "events": [
                {"type": "adjust_metric", "target_id": "coalition_support", "delta": 11}
            ],
        }
    })

    assert await process_next(provider)
    assert provider.contexts[0].scenario_version == "2.5"
    assert provider.contexts[0].player_role_id == "british_adviser"
    assert provider.contexts[0].behavior_rules is not None
    status = await authored_client.get(queued.headers["location"], headers=headers)
    resumed = await authored_client.get(
        f"/api/v1/sessions/{session['id']}", headers=headers
    )
    assert status.json()["status"] == "queued"
    assert status.json()["failure_code"] == "INTERNAL_ERROR"
    assert resumed.json()["turn_number"] == 0
    assert resumed.json()["state"]["metrics"]["coalition_support"] == 50
    async with session_factory() as db:
        committed = (await db.execute(select(CommittedTurn).where(
            CommittedTurn.session_id == UUID(session["id"])
        ))).scalars().all()
    assert committed == []





SCRIPTED_SESSIONS = (
    Path(__file__).parents[2]
    / "scenarios"
    / "versailles-1919"
    / "tests"
    / "scripted_sessions.yaml"
)
SESSION_CASES = yaml.safe_load(SCRIPTED_SESSIONS.read_text(encoding="utf-8"))["sessions"]
RULE_PHASES = {
    rule["issue_id"]: rule["phase_id"]
    for rule in load_scenario(MANIFEST).behavior_rules["rules"]
}


@pytest.mark.parametrize("script", SESSION_CASES, ids=lambda script: script["id"])
async def test_complete_scripted_session_follows_authored_path_to_reflection(
    authored_client, script
):
    created = await authored_client.post(
        "/api/v1/sessions", json={"scenario": "versailles-1919"}
    )
    assert created.status_code == 201, created.text
    session = created.json()
    authored_client.created_sessions.append(UUID(session["id"]))
    headers = {"Authorization": f"Bearer {session['token']}"}

    proposal_by_turn = []
    current_phase = "opening_context"
    for target_phase in script["path"]:
        events = [
            {
                "type": "issue_decision",
                "issue_id": issue_id,
                "decision_code": decision_code,
            }
            for issue_id, decision_code in script.get("issue_decisions", {}).items()
            if RULE_PHASES[issue_id] == current_phase
        ]
        if current_phase == "german_objections":
            events.append({
                "type": "issue_decision",
                "issue_id": "german_objections",
                "decision_code": script["german_decision"],
            })
        if current_phase == "final_settlement":
            events.append({
                "type": "issue_decision",
                "issue_id": "final_package",
                "decision_code": script["final_decision"],
            })
            events.extend(
                {"type": "set_flag", "target_id": flag}
                for flag in script["final_flags"]
            )
            for metric in ("support", "evidence"):
                if f"{metric}_delta" in script:
                    events.append({
                        "type": "adjust_metric",
                        "target_id": (
                            "coalition_support"
                            if metric == "support"
                            else "evidence_coverage"
                        ),
                        "delta": script[f"{metric}_delta"],
                    })
        events.append({"type": "advance_phase", "target_phase_id": target_phase})
        proposal_by_turn.append({
            "reply": f"{script['id']}: {current_phase} behandlet.",
            "events": events,
        })
        current_phase = target_phase
    assert current_phase == "reflection"
    proposal_by_turn.append({
        "reply": "Sammenlign udfaldet med den historiske aftale.",
        "events": [{"type": "set_flag", "target_id": "reflection_complete"}],
    })

    for turn_number, proposal in enumerate(proposal_by_turn):
        client_turn_id = str(uuid4())
        text = f"{script['id']} turn {turn_number}"
        body = {
            "client_turn_id": client_turn_id,
            "expected_turn": turn_number,
            "text": text,
            "input_language": "da",
        }
        response = await authored_client.post(
            f"/api/v1/sessions/{session['id']}/turns",
            json=body,
            headers={**headers, "Idempotency-Key": client_turn_id},
        )
        assert response.status_code == 202, response.text
        request_id = response.json()["request_id"]
        if turn_number == 0 and script.get("replay_first_turn"):
            replay = await authored_client.post(
                f"/api/v1/sessions/{session['id']}/turns",
                json=body,
                headers={**headers, "Idempotency-Key": client_turn_id},
            )
            assert replay.status_code == 202
            assert replay.json()["request_id"] == request_id
            orphaned_claim = await claim_next()
            assert orphaned_claim is not None
            assert orphaned_claim.request_id == UUID(request_id)
            async with session_factory() as db, db.begin():
                await db.execute(
                    update(TurnRequest)
                    .where(TurnRequest.id == UUID(request_id))
                    .values(
                        leased_until=func.clock_timestamp() - timedelta(seconds=1)
                    )
                )

        provider = ScriptedScenarioInterpreter({text: proposal})
        assert await process_next(provider)
        assert provider.contexts[0].scenario_version == "2.5"
        assert provider.contexts[0].state["phase"] == (
            "opening_context" if turn_number == 0 else script["path"][turn_number - 1]
        )
        status = await authored_client.get(
            f"/api/v1/turn-requests/{request_id}", headers=headers
        )
        assert status.json()["status"] == "completed"

    resumed = await authored_client.get(
        f"/api/v1/sessions/{session['id']}", headers=headers
    )
    final_state = resumed.json()["state"]
    assert resumed.json()["turn_number"] == len(proposal_by_turn)
    assert final_state["phase"] == "reflection"
    assert final_state["flags"]["reflection_complete"] is True
    assert final_state["flags"]["agreement_reached"] is (
        "agreement_reached" in script["final_flags"]
    )
    assert final_state["flags"]["deadlock"] is ("deadlock" in script["final_flags"])
    assert final_state["flags"]["counterfactual_terms"] is (
        "counterfactual_terms" in script["final_flags"]
    )
    assert final_state["decisions"]["final_package"] == script["final_decision"]
    assert final_state["decisions"]["german_objections"] == script["german_decision"]
    assert final_state["visited_phases"] == ["opening_context", *script["path"]]
    expected_issues = {
        rule["issue_id"] for rule in load_scenario(MANIFEST).behavior_rules["rules"]
    }
    assert set(final_state["decisions"]) | set(final_state["skipped_issues"]) == expected_issues
    assert not (set(final_state["decisions"]) & set(final_state["skipped_issues"]))
    assert set(script.get("expected_skipped_issues", [])) <= set(final_state["skipped_issues"])

    history = await authored_client.get(
        f"/api/v1/sessions/{session['id']}/turns", headers=headers
    )
    assert len(history.json()["turns"]) == len(proposal_by_turn)
    assert [item["turn_number"] for item in history.json()["turns"]] == list(
        range(1, len(proposal_by_turn) + 1)
    )
