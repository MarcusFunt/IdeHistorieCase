"""PostgreSQL API contract for scenario discovery and role-aware sessions."""
import os
import secrets
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.db.connection import engine, session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, ScenarioVersion, TurnRequest
from app.main import app
from app.scenarios.loader import load_scenario
from app.scenarios.repository import activate_scenario_version, import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
MANIFEST = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
MANIFEST_V2 = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "3.0-ai.yaml"


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
    assert old_data["runtime_version"] == "v1_legacy"
    assert old_data["lifecycle_status"] == "ready"

    key_only_v1 = await scenario_client.post(
        "/api/v1/sessions",
        json={"scenario": "versailles-1919"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert key_only_v1.status_code == 201
    scenario_client.created_sessions.append(UUID(key_only_v1.json()["id"]))

    supplied_capability = "z" * 43
    paired_headers_v1 = await scenario_client.post(
        "/api/v1/sessions",
        json={"scenario": "versailles-1919"},
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Session-Capability": supplied_capability,
        },
    )
    assert paired_headers_v1.status_code == 201
    assert paired_headers_v1.json()["token"] != supplied_capability
    scenario_client.created_sessions.append(UUID(paired_headers_v1.json()["id"]))

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
    assert resumed_old.json()["runtime_version"] == "v1_legacy"
    assert resumed_old.json()["lifecycle_status"] == "ready"
    assert resumed_old.json()["initialization_status"] == "not_applicable"
    assert resumed_old.json()["initialization"]["status"] == "not_applicable"
    assert resumed_old.json()["initialization"]["opening"] is None
    assert resumed_new.json()["scenario"] == "versailles-1919"
    assert resumed_new.json()["scenario_version"] == "2.5"
    assert resumed_new.json()["player_role_id"] == "british_adviser"


async def test_active_v2_session_queues_initialization_without_becoming_playable(
    scenario_client, tmp_path
):
    bundle = tmp_path / "versailles-1919"
    bundle.mkdir()
    shutil.copy2(MANIFEST_V2, bundle / MANIFEST_V2.name)
    shutil.copytree(MANIFEST_V2.parent / "notes", bundle / "notes")
    v2_version = f"api-test-{uuid4().hex[:12]}"
    path = bundle / MANIFEST_V2.name
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'version: "3.0-ai"', f'version: "{v2_version}"'
        ),
        encoding="utf-8",
    )
    async with session_factory() as db, db.begin():
        imported = await import_scenario(db, load_scenario(path))
        await db.execute(
            update(ScenarioVersion)
            .where(ScenarioVersion.slug == "versailles-1919")
            .values(is_active=False)
        )
        imported.scenario.is_active = True

    listing = await scenario_client.get("/api/v1/scenarios")
    v2_row = next(
        row for row in listing.json()
        if row["slug"] == "versailles-1919" and row["version"] == v2_version
    )
    assert v2_row["schema_version"] == 2
    assert v2_row["default_language"] == "da"
    assert v2_row["supported_roles"] == ["british_adviser"]

    detail = await scenario_client.get("/api/v1/scenarios/versailles-1919")
    assert detail.status_code == 200
    body = detail.json()
    assert body["version"] == v2_version
    assert body["schema_version"] == 2
    assert body["metadata"]["language"] == "da"
    assert body["chronology"]["opening_date"] == "1919-04-01"
    assert "phases" not in body and "dilemmas" not in body

    request_key = uuid4()
    capability = secrets.token_urlsafe(32)
    create = await scenario_client.post(
        "/api/v1/sessions",
        json={"scenario": "versailles-1919"},
        headers={
            "Idempotency-Key": str(request_key),
            "X-Session-Capability": capability,
        },
    )
    assert create.status_code == 201
    session = create.json()
    scenario_client.created_sessions.append(UUID(session["id"]))
    assert session["scenario_version"] == v2_version
    assert session["initialization_status"] == "queued"
    assert session["lifecycle_status"] == "uninitialized"
    assert session["runtime_version"] == "v2_ai"

    initialization = await scenario_client.get(
        f"/api/v1/sessions/{session['id']}/initialization",
        headers={"Authorization": f"Bearer {capability}"},
    )
    assert initialization.status_code == 200
    assert initialization.json()["status"] == "queued"
    assert initialization.json()["lifecycle_status"] == "uninitialized"
    assert initialization.json()["runtime_version"] == "v2_ai"
    assert initialization.json()["opening"] is None

    turn_id = uuid4()
    blocked_turn = await scenario_client.post(
        f"/api/v1/sessions/{session['id']}/turns",
        json={
            "client_turn_id": str(turn_id),
            "expected_turn": 0,
            "text": "Undersøg situationen.",
            "input_language": "da",
        },
        headers={
            "Authorization": f"Bearer {capability}",
            "Idempotency-Key": str(turn_id),
        },
    )
    assert blocked_turn.status_code == 409
