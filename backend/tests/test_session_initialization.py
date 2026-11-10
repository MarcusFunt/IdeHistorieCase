"""Durable, version-pinned initialization lifecycle against isolated PostgreSQL."""
import asyncio
import os
import secrets
import shutil
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy import delete, func, select, update

from app.db.connection import engine, session_factory
from app.db.models import GameSession, SessionInitializationJob
from app.game.initialization import (
    claim_next,
    complete_claim,
    fail_claim,
    mark_repair_attempt,
    renew_claim,
)
from app.main import app
from app.scenarios.loader import load_scenario
from app.scenarios.repository import activate_scenario_version, import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
SOURCE_RELEASE = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry/1.0.yaml"


def make_release(tmp_path: Path) -> tuple[Path, str]:
    slug = f"initialization-test-{uuid4().hex[:10]}"
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
    return manifest, slug


@pytest_asyncio.fixture
async def v2_client(tmp_path):
    session_ids: set[UUID] = set()
    try:
        manifest, slug = make_release(tmp_path)
        loaded = load_scenario(manifest)
        async with session_factory() as db, db.begin():
            imported = await import_scenario(db, loaded)
            await activate_scenario_version(db, slug, imported.scenario.version)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.created_sessions = session_ids
            yield client, slug
    finally:
        if session_ids:
            async with session_factory() as db, db.begin():
                await db.execute(
                    delete(SessionInitializationJob).where(
                        SessionInitializationJob.session_id.in_(session_ids)
                    )
                )
                await db.execute(
                    delete(GameSession).where(GameSession.id.in_(session_ids))
                )
        await engine.dispose()


def create_headers(*, request_key: UUID | None = None, capability: str | None = None) -> dict:
    headers = {}
    if request_key is not None:
        headers["Idempotency-Key"] = str(request_key)
    if capability is not None:
        headers["X-Session-Capability"] = capability
    return headers


async def create_v2_session(client, slug: str, *, request_key=None, capability=None):
    request_key = request_key or uuid4()
    capability = capability or secrets.token_urlsafe(32)
    response = await client.post(
        "/api/v1/sessions",
        json={"scenario": slug},
        headers=create_headers(request_key=request_key, capability=capability),
    )
    assert response.status_code == 201, response.text
    client.created_sessions.add(UUID(response.json()["id"]))
    return response


async def set_job_due(job_id: UUID) -> None:
    async with session_factory() as db, db.begin():
        await db.execute(
            update(SessionInitializationJob)
            .where(SessionInitializationJob.id == job_id)
            .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
        )


async def test_duplicate_create_retries_reuse_the_pinned_session_and_job(v2_client):
    client, slug = v2_client
    missing_idempotency = await client.post("/api/v1/sessions", json={"scenario": slug})
    assert missing_idempotency.status_code == 422

    request_key = uuid4()
    capability = "c" * 43
    payload = {"scenario": slug}

    first, retry = await asyncio.gather(
        client.post(
            "/api/v1/sessions",
            json=payload,
            headers=create_headers(request_key=request_key, capability=capability),
        ),
        client.post(
            "/api/v1/sessions",
            json=payload,
            headers=create_headers(request_key=request_key, capability=capability),
        ),
    )

    assert first.status_code == retry.status_code == 201
    bodies = [first.json(), retry.json()]
    client.created_sessions.update(UUID(body["id"]) for body in bodies)
    assert bodies[0]["id"] == bodies[1]["id"]
    assert sorted(body["token"] is None for body in bodies) == [False, True]
    assert next(body["token"] for body in bodies if body["token"] is not None) == capability
    assert all(body["initialization_status"] == "queued" for body in bodies)
    assert all(body["lifecycle_status"] == "uninitialized" for body in bodies)
    assert all(body["runtime_version"] == "v2_ai" for body in bodies)
    session_id = UUID(bodies[0]["id"])

    status = await client.get(
        f"/api/v1/sessions/{session_id}/initialization",
        headers={"Authorization": f"Bearer {capability}"},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert status.json()["opening"] is None

    blocked_turn_id = uuid4()
    blocked_turn = await client.post(
        f"/api/v1/sessions/{session_id}/turns",
        json={
            "client_turn_id": str(blocked_turn_id),
            "expected_turn": 0,
            "text": "Undersøg situationen.",
            "input_language": "da",
        },
        headers={
            "Authorization": f"Bearer {capability}",
            "Idempotency-Key": str(blocked_turn_id),
        },
    )
    assert blocked_turn.status_code == 409

    async with session_factory() as db:
        jobs = (
            await db.execute(
                select(SessionInitializationJob).where(
                    SessionInitializationJob.session_id == session_id
                )
            )
        ).scalars().all()
        session = await db.get(GameSession, session_id)
    assert len(jobs) == 1
    assert jobs[0].status == "queued"
    assert session.state == {}

    mismatched_retry = await client.post(
        "/api/v1/sessions",
        json={"scenario": slug, "role_id": "another-role"},
        headers=create_headers(request_key=request_key, capability=capability),
    )
    assert mismatched_retry.status_code == 409


async def test_concurrent_claims_fence_restarted_worker_and_commit_one_opening(v2_client):
    client, slug = v2_client
    first = await create_v2_session(client, slug)
    second = await create_v2_session(client, slug)
    first_id, second_id = UUID(first.json()["id"]), UUID(second.json()["id"])

    claim_a, claim_b = await asyncio.gather(claim_next(), claim_next())
    assert claim_a is not None and claim_b is not None
    assert claim_a.session_id != claim_b.session_id
    assert {claim_a.session_id, claim_b.session_id} == {first_id, second_id}

    async with session_factory() as db, db.begin():
        await db.execute(
            update(SessionInitializationJob)
            .where(SessionInitializationJob.id == claim_a.job_id)
            .values(leased_until=func.clock_timestamp() - timedelta(seconds=1))
        )
    resumed_claim = await claim_next()
    assert resumed_claim is not None
    assert resumed_claim.job_id == claim_a.job_id
    assert resumed_claim.lease_token != claim_a.lease_token

    owner_token = first.json()["token"] if resumed_claim.session_id == first_id else second.json()["token"]
    running = await client.get(
        f"/api/v1/sessions/{resumed_claim.session_id}/initialization",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert running.status_code == 200
    assert running.json()["status"] == "running"
    assert running.json()["opening"] is None

    opening = {
        "language": "da",
        "world_date": "1888-05-01",
        "scene": "En undersøger skal indsamle oplysninger.",
    }
    initial_state = {"world_date": "1888-05-01"}
    assert not await complete_claim(claim_a, opening, initial_state)
    assert await complete_claim(resumed_claim, opening, initial_state)
    assert not await complete_claim(resumed_claim, {**opening, "scene": "duplicate"}, initial_state)

    status = await client.get(
        f"/api/v1/sessions/{resumed_claim.session_id}/initialization",
        headers={"Authorization": f"Bearer {first.json()['token']}"}
        if resumed_claim.session_id == first_id
        else {"Authorization": f"Bearer {second.json()['token']}"},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "ready"
    assert status.json()["opening"] == opening

    async with session_factory() as db:
        ready_job = await db.get(SessionInitializationJob, resumed_claim.job_id)
        ready_session = await db.get(GameSession, resumed_claim.session_id)
        session_jobs = (
            await db.execute(
                select(SessionInitializationJob.id).where(
                    SessionInitializationJob.session_id == resumed_claim.session_id
                )
            )
        ).all()
    assert ready_job.result["opening"] == opening
    assert ready_job.result["initial_state"] == initial_state
    assert ready_session.state == initial_state
    assert ready_session.turn_number == 0
    assert len(session_jobs) == 1

    turn_id = uuid4()
    unsupported_turn = await client.post(
        f"/api/v1/sessions/{resumed_claim.session_id}/turns",
        json={
            "client_turn_id": str(turn_id),
            "expected_turn": 0,
            "text": "Undersøg situationen.",
            "input_language": "da",
        },
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Idempotency-Key": str(turn_id),
        },
    )
    assert unsupported_turn.status_code == 409


@pytest.mark.asyncio
async def test_repair_budget_is_persisted_and_fenced_across_claims(v2_client):
    client, slug = v2_client
    created = await create_v2_session(client, slug)
    claim = await claim_next()
    assert claim is not None
    assert claim.session_id == UUID(created.json()["id"])
    assert not claim.repair_used

    assert await mark_repair_attempt(claim)
    assert not await mark_repair_attempt(claim)
    assert await renew_claim(claim)
    async with session_factory() as db, db.begin():
        await db.execute(
            update(SessionInitializationJob)
            .where(SessionInitializationJob.id == claim.job_id)
            .values(leased_until=func.clock_timestamp() - timedelta(seconds=1))
        )

    resumed = await claim_next()
    assert resumed is not None
    assert resumed.job_id == claim.job_id
    assert resumed.lease_token != claim.lease_token
    assert resumed.repair_used
    assert not await mark_repair_attempt(resumed)
    assert not await renew_claim(claim)


class _NoEvidenceRetrieval:
    async def search(self, *_args, **_kwargs):
        raise AssertionError("this scenario has no eligible source passages")


class _ScriptedOpening:
    model_alias = "ddu-narrator"

    def __init__(self, output):
        from hashlib import sha256

        from app.ai.llm import StructuredCompletion

        self.output = output
        self.calls = []
        self.completion = StructuredCompletion(
            output=output,
            requested_model=self.model_alias,
            served_model=self.model_alias,
            model_revision="test-revision",
            provider_request_id="test-opening-request",
            prompt_tokens=140,
            completion_tokens=90,
            duration_ms=17,
            raw_output_sha256=sha256(b"sql-backed test opening").hexdigest(),
            finish_reason="stop",
        )

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.completion


@pytest.mark.asyncio
async def test_worker_generates_from_pinned_sql_context_and_persists_immutable_provenance(v2_client):
    from app.scenarios.loader import load_scenario
    from app.workers.initialization import process_initialization_claim

    client, slug = v2_client
    created = await create_v2_session(client, slug)
    claim = await claim_next()
    assert claim is not None
    source = load_scenario(SOURCE_RELEASE).definition
    role = next(role for role in source.roles if role.id == source.default_role_id)
    proposal = {
        "language": "da",
        "role_id": role.id,
        "world_date": source.chronology.opening_date.isoformat(),
        "title": "Undersøgelsen begynder",
        "scene": "En arbejdsgruppe samles for at undersøge forholdene.",
        "immediate_tension": "Arbejdernes behov og virksomhedens økonomi støder sammen.",
        "player_goal": "Find frem til de vigtigste spørgsmål.",
        "player_prompt": "Hvad vil du undersøge først?",
        "present_actor_ids": [role.actor_id],
        "objective_ids": [source.learning_objectives[0].id],
        "simulation_label": "simulated",
        "unresolved_threads": ["Hvilke forhold bør undersøges?"],
        "historical_claims": [],
    }
    generator = _ScriptedOpening(proposal)

    committed = await process_initialization_claim(
        claim,
        generator=generator,
        retrieval_service=_NoEvidenceRetrieval(),
        renew_lease=False,
    )

    assert committed
    assert "forfatterudarbejdet oplæg" in generator.calls[0]["user_prompt"]
    status_url = f"/api/v1/sessions/{claim.session_id}/initialization"
    headers = {"Authorization": f"Bearer {created.json()['token']}"}
    status = await client.get(status_url, headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "ready"
    assert status.json()["opening"] == proposal
    assert status.json()["provenance"]["requested_model"] == "ddu-narrator"
    assert status.json()["provenance"]["model_revision"] == "test-revision"

    async with session_factory() as db:
        ready_job = await db.get(SessionInitializationJob, claim.job_id)
        ready_session = await db.get(GameSession, claim.session_id)
        first_result = ready_job.result
    assert first_result["provenance"]["scenario"]["slug"] == slug
    assert first_result["provenance"]["scenario"]["content_hash"]
    assert first_result["initial_state"]["turn_number"] == 0
    assert await complete_claim(
        claim,
        {**proposal, "scene": "duplicate scene"},
        ready_session.state,
        provenance=first_result["provenance"],
        attempt_metadata=first_result["attempt_metadata"],
    ) is False

    resumed = await client.get(status_url, headers=headers)
    assert resumed.json()["opening"] == proposal
    assert resumed.json()["provenance"] == first_result["provenance"]


async def test_expired_capability_cannot_replay_session_creation(v2_client):
    client, slug = v2_client
    request_key = uuid4()
    capability = secrets.token_urlsafe(32)
    created = await create_v2_session(
        client, slug, request_key=request_key, capability=capability
    )
    session_id = UUID(created.json()["id"])
    async with session_factory() as db, db.begin():
        await db.execute(
            update(GameSession)
            .where(GameSession.id == session_id)
            .values(capability_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )

    replay = await client.post(
        "/api/v1/sessions",
        json={"scenario": slug},
        headers=create_headers(request_key=request_key, capability=capability),
    )
    assert replay.status_code == 404


async def test_concurrent_create_with_reused_capability_conflicts_cleanly(v2_client):
    client, slug = v2_client
    capability = secrets.token_urlsafe(32)
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/sessions",
                json={"scenario": slug},
                headers=create_headers(request_key=uuid4(), capability=capability),
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    created = next(response for response in responses if response.status_code == 201)
    client.created_sessions.add(UUID(created.json()["id"]))


async def test_retryable_failure_reuses_job_and_exhausts_one_client_retry(v2_client):
    client, slug = v2_client
    created = await create_v2_session(client, slug)
    session = created.json()
    headers = {"Authorization": f"Bearer {session['token']}"}

    for expected_attempt in range(1, 4):
        claim = await claim_next()
        assert claim is not None
        assert await fail_claim(claim, "INFERENCE_UNAVAILABLE", retryable=True)
        async with session_factory() as db:
            job = await db.get(SessionInitializationJob, claim.job_id)
        assert job.attempts == expected_attempt
        if expected_attempt < 3:
            assert job.status == "queued"
            await set_job_due(job.id)
        else:
            assert job.status == "retryable_failure"

    retryable_status = await client.get(
        f"/api/v1/sessions/{session['id']}/initialization", headers=headers
    )
    assert retryable_status.json()["status"] == "retryable_failure"
    assert retryable_status.json()["failure"]["retryable"] is True
    assert retryable_status.json()["opening"] is None

    job_id = job.id
    retry = await client.post(
        f"/api/v1/sessions/{session['id']}/initialization/retry", headers=headers
    )
    replay = await client.post(
        f"/api/v1/sessions/{session['id']}/initialization/retry", headers=headers
    )
    assert retry.status_code == replay.status_code == 202
    assert retry.json()["job_id"] == replay.json()["job_id"] == str(job_id)
    assert retry.json()["status"] == replay.json()["status"] == "queued"

    for expected_attempt in range(4, 7):
        await set_job_due(job_id)
        claim = await claim_next()
        assert claim is not None and claim.job_id == job_id
        assert await fail_claim(claim, "INFERENCE_UNAVAILABLE", retryable=True)
        async with session_factory() as db:
            job = await db.get(SessionInitializationJob, job_id)
        assert job.attempts == expected_attempt
        if expected_attempt < 6:
            assert job.status == "queued"
        else:
            assert job.status == "failed"

    status = await client.get(
        f"/api/v1/sessions/{session['id']}/initialization", headers=headers
    )
    assert status.json()["status"] == "failed"
    assert status.json()["failure"]["code"] == "INFERENCE_UNAVAILABLE"
    assert status.json()["opening"] is None
    resumed = await client.get(f"/api/v1/sessions/{session['id']}", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["turn_number"] == 0
    assert resumed.json()["state"] == {}
    terminal_retry = await client.post(
        f"/api/v1/sessions/{session['id']}/initialization/retry", headers=headers
    )
    assert terminal_retry.status_code == 409
