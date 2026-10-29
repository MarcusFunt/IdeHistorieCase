"""PostgreSQL API contract for scenario discovery and role-aware sessions."""
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.connection import engine, session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, TurnRequest
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
async def scenario_client():
    created_sessions: list[UUID] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.created_sessions = created_sessions
        async with session_factory() as db, db.begin():
            await import_scenario(db, load_scenario(MANIFEST))
            await activate_scenario_version(db, "versailles-1919", "1")
        yield client
        if created_sessions:
            async with session_factory() as db, db.begin():
                turn_ids = select(CommittedTurn.id).where(CommittedTurn.session_id.in_(created_sessions))
                await db.execute(delete(GameEvent).where(GameEvent.committed_turn_id.in_(turn_ids)))
                await db.execute(delete(CommittedTurn).where(CommittedTurn.session_id.in_(created_sessions)))
                await db.execute(delete(TurnRequest).where(TurnRequest.session_id.in_(created_sessions)))
                await db.execute(delete(GameSession).where(GameSession.id.in_(created_sessions)))
        async with session_factory() as db, db.begin():
            await activate_scenario_version(db, "versailles-1919", "1")


async def test_discovery_lists_active_and_authored_metadata_and_details_hide_source_refs(scenario_client):
    async with session_factory() as db, db.begin():
        await activate_scenario_version(db, "versailles-1919", "2.5")
    listing = await scenario_client.get("/api/v1/scenarios")
    assert listing.status_code == 200
    versions = [row for row in listing.json() if row["slug"] == "versailles-1919"]
    assert versions[0]["version"] == "2.5" and versions[0]["is_active"] is True
    assert versions[0]["is_authored"] is True
    assert versions[0]["title"]
    assert "british_adviser" in versions[0]["supported_roles"]
    legacy = next(row for row in versions if row["version"] == "1")
    assert legacy["is_authored"] is False and legacy["supported_roles"] == []

    detail = await scenario_client.get("/api/v1/scenarios/versailles-1919")
    assert detail.status_code == 200
    body = detail.json()
    assert body["version"] == "2.5"
    assert body["roles"][0]["id"] == "british_adviser"
    assert len(body["learning_objectives"]) == 8
    assert len(body["dilemmas"]) == 12
    assert "source_refs" not in body
    assert (await scenario_client.get("/api/v1/scenarios/missing")).status_code == 404
    async with session_factory() as db, db.begin():
        await activate_scenario_version(db, "versailles-1919", "1")
    legacy_detail = await scenario_client.get("/api/v1/scenarios/versailles-1919")
    assert legacy_detail.status_code == 409
    assert "unauthored" in legacy_detail.json()["detail"].lower()


async def test_sessions_resolve_active_version_role_and_keep_old_pin(scenario_client):
    old = await scenario_client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    assert old.status_code == 201
    old_data = old.json()
    scenario_client.created_sessions.append(UUID(old_data["id"]))
    assert old_data["scenario_version"] == "1" and old_data["role_id"] is None

    async with session_factory() as db, db.begin():
        await activate_scenario_version(db, "versailles-1919", "2.5")
    default = await scenario_client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    assert default.status_code == 201
    default_data = default.json()
    scenario_client.created_sessions.append(UUID(default_data["id"]))
    assert default_data["scenario_version"] == "2.5"
    assert default_data["role_id"] == "british_adviser"

    requested = await scenario_client.post(
        "/api/v1/sessions", json={"scenario": "versailles-1919", "role_id": "british_adviser"}
    )
    assert requested.status_code == 201
    requested_data = requested.json()
    scenario_client.created_sessions.append(UUID(requested_data["id"]))
    assert requested_data["role_id"] == "british_adviser"

    async with session_factory() as db:
        before = (await db.execute(select(GameSession.id))).scalars().all()
    invalid = await scenario_client.post(
        "/api/v1/sessions", json={"scenario": "versailles-1919", "role_id": "germany"}
    )
    assert invalid.status_code == 422
    async with session_factory() as db:
        after = (await db.execute(select(GameSession.id))).scalars().all()
    assert len(after) == len(before)

    resumed_old = await scenario_client.get(
        f"/api/v1/sessions/{old_data['id']}", headers={"Authorization": f"Bearer {old_data['token']}"}
    )
    resumed_new = await scenario_client.get(
        f"/api/v1/sessions/{default_data['id']}",
        headers={"Authorization": f"Bearer {default_data['token']}"},
    )
    assert resumed_old.json()["scenario_version"] == "1"
    assert resumed_old.json()["player_role_id"] is None
    assert resumed_new.json()["scenario"] == "versailles-1919"
    assert resumed_new.json()["scenario_version"] == "2.5"
    assert resumed_new.json()["player_role_id"] == "british_adviser"
