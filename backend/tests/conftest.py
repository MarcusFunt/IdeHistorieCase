"""Shared isolated v2 turn fixtures; PostgreSQL tests require DDU_TEST_DB=1."""
from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy import delete, select

from app.db.connection import engine, session_factory
from app.db.models import (
    CommittedTurn,
    GameEvent,
    GameSession,
    SessionInitializationJob,
    TurnRequest,
)
from app.main import app
from app.scenarios.loader import load_scenario
from app.scenarios.repository import activate_scenario_version, import_scenario

SOURCE_RELEASE = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry/1.0.yaml"


@dataclass
class TurnHarness:
    client: httpx.AsyncClient
    slug: str
    definition: object
    created_sessions: set[UUID]

    async def create_ready_session(self) -> dict:
        capability = secrets.token_urlsafe(32)
        response = await self.client.post(
            "/api/v1/sessions",
            json={"scenario": self.slug},
            headers={
                "Idempotency-Key": str(uuid4()),
                "X-Session-Capability": capability,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        session_id = UUID(body["id"])
        self.created_sessions.add(session_id)
        role = next(role for role in self.definition.roles if role.id == body["role_id"])
        state = {
            "runtime_version": "v2_ai",
            "world_date": self.definition.chronology.opening_date.isoformat(),
            "turn_number": 0,
            "scene": {"title": "Undersøgelse", "description": "Sagen undersøges."},
            "player_goal": "Undersøg arbejdsvilkårene.",
            "player_prompt": "Hvad vil du gøre?",
            "present_actor_ids": [role.actor_id],
            "objective_ids": [item.id for item in self.definition.learning_objectives],
            "simulation_label": "simulated",
            "open_threads": [],
            "historical_claims": [],
            "decisions": [],
            "simulation_events": [],
            "relations": [],
        }
        async with session_factory() as db, db.begin():
            session = await db.get(GameSession, session_id)
            job = await db.get(SessionInitializationJob, body["initialization_job_id"])
            assert session is not None and job is not None
            session.state = state
            job.status = "ready"
            job.result = {
                "opening": {"scene": state["scene"]},
                "initial_state": state,
                "attempt_metadata": [],
            }
        body["state"] = state
        return body

    async def submit_turn(
        self,
        session: dict,
        text: str,
        *,
        expected_turn: int = 0,
        client_turn_id: UUID | None = None,
    ) -> httpx.Response:
        turn_id = client_turn_id or uuid4()
        return await self.client.post(
            f"/api/v1/sessions/{session['id']}/turns",
            json={
                "client_turn_id": str(turn_id),
                "expected_turn": expected_turn,
                "text": text,
                "input_language": "da",
            },
            headers={
                "Authorization": f"Bearer {session['token']}",
                "Idempotency-Key": str(turn_id),
            },
        )


@pytest_asyncio.fixture
async def turn_harness(tmp_path):
    if os.getenv("DDU_TEST_DB") != "1":
        pytest.skip("isolated PostgreSQL not configured; set DDU_TEST_DB=1")
    slug = f"turn-test-{uuid4().hex[:12]}"
    target = tmp_path / slug
    target.mkdir()
    shutil.copytree(SOURCE_RELEASE.parent / "notes", target / "notes")
    raw = yaml.safe_load(SOURCE_RELEASE.read_text(encoding="utf-8"))
    raw["slug"] = slug
    raw["source_refs"] = []
    raw["content_items"][0]["source_refs"] = []
    manifest = target / SOURCE_RELEASE.name
    manifest.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    loaded = load_scenario(manifest)
    session_ids: set[UUID] = set()
    try:
        async with session_factory() as db, db.begin():
            imported = await import_scenario(db, loaded)
            await activate_scenario_version(db, slug, imported.scenario.version)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield TurnHarness(client, slug, loaded.definition, session_ids)
    finally:
        if session_ids:
            async with session_factory() as db, db.begin():
                committed_ids = await db.execute(
                    select(CommittedTurn.id).where(
                        CommittedTurn.session_id.in_(session_ids)
                    )
                )
                await db.execute(
                    delete(GameEvent).where(
                        GameEvent.committed_turn_id.in_(committed_ids.scalars().all())
                    )
                )
                await db.execute(delete(CommittedTurn).where(CommittedTurn.session_id.in_(session_ids)))
                await db.execute(delete(TurnRequest).where(TurnRequest.session_id.in_(session_ids)))
                await db.execute(
                    delete(SessionInitializationJob).where(
                        SessionInitializationJob.session_id.in_(session_ids)
                    )
                )
                await db.execute(delete(GameSession).where(GameSession.id.in_(session_ids)))
        # Published scenario releases are immutable by design. Their randomized test
        # slugs remain in this disposable database and disappear with the database.
        await engine.dispose()
