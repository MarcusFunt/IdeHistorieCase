"""M2 contract tests against a disposable migrated PostgreSQL database."""
import asyncio
import os
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from app.db.connection import engine, session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, TurnRequest
from app.game import routes, turn_worker
from app.game.turn_worker import claim_next, complete_claim, process_next
from app.main import app

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured"),
]


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.created_sessions = []
        yield c
        if c.created_sessions:
            async with session_factory() as db, db.begin():
                turns = select(CommittedTurn.id).where(
                    CommittedTurn.session_id.in_(c.created_sessions)
                )
                await db.execute(delete(GameEvent).where(GameEvent.committed_turn_id.in_(turns)))
                await db.execute(delete(CommittedTurn).where(
                    CommittedTurn.session_id.in_(c.created_sessions)
                ))
                await db.execute(delete(TurnRequest).where(
                    TurnRequest.session_id.in_(c.created_sessions)
                ))
                await db.execute(delete(GameSession).where(GameSession.id.in_(c.created_sessions)))
    await engine.dispose()


async def new_session(client):
    response = await client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    assert response.status_code == 201, response.text
    session = response.json()
    client.created_sessions.append(UUID(session["id"]))
    assert session["turn_number"] == 0
    assert session["scenario_version"] == "1"
    return session


def turn_input(turn_id=None):
    return {
        "client_turn_id": str(turn_id or uuid4()),
        "expected_turn": 0,
        "text": "Vi må sikre en retfærdig fred.",
        "input_language": "da",
    }


def request_headers(session, payload):
    return {
        "Authorization": f"Bearer {session['token']}",
        "Idempotency-Key": payload["client_turn_id"],
    }


async def test_authenticated_session_and_idempotent_submission(client):
    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    url = f"/api/v1/sessions/{session['id']}/turns"

    first, retry = await asyncio.gather(
        client.post(url, json=payload, headers=headers),
        client.post(url, json=payload, headers=headers),
    )
    assert first.status_code == retry.status_code == 202
    request_id = first.json()["request_id"]
    assert retry.json()["request_id"] == request_id
    assert first.headers["location"] == f"/api/v1/turn-requests/{request_id}"
    resumed = await client.get(
        f"/api/v1/sessions/{session['id']}", headers={"Authorization": headers["Authorization"]}
    )
    assert resumed.status_code == 200
    assert resumed.json()["pending_request"]["request_id"] == request_id
    assert "token" not in resumed.json()
    history = await client.get(url, headers={"Authorization": headers["Authorization"]})
    assert history.status_code == 200
    assert history.json()["turns"] == []


async def test_turn_conflicts_and_session_isolation(client):
    first_session = await new_session(client)
    other_session = await new_session(client)
    payload = turn_input()
    headers = request_headers(first_session, payload)
    url = f"/api/v1/sessions/{first_session['id']}/turns"
    accepted = await client.post(url, json=payload, headers=headers)
    assert accepted.status_code == 202

    changed = {**payload, "text": "Et andet forslag."}
    assert (await client.post(url, json=changed, headers=headers)).status_code == 409
    other = turn_input()
    conflict = await client.post(url, json=other, headers=request_headers(first_session, other))
    assert conflict.status_code == 409
    assert conflict.json()["request_id"] == accepted.json()["request_id"]

    other_auth = {"Authorization": f"Bearer {other_session['token']}"}
    assert (await client.get(f"/api/v1/sessions/{first_session['id']}", headers=other_auth)).status_code == 404
    assert (await client.get(accepted.headers["location"], headers=other_auth)).status_code == 404
    assert (await client.post(url, json=other, headers=request_headers(other_session, other))).status_code == 404
    assert (await client.get(f"/api/v1/sessions/{first_session['id']}")).status_code == 401


async def test_session_token_is_hashed_and_invalid_inputs_are_rejected(client):
    session = await new_session(client)
    async with session_factory() as db:
        stored = await db.get(GameSession, UUID(session["id"]))
        assert session["token"] not in stored.capability_hash
        assert len(stored.capability_hash) == 64
    payload = turn_input()
    headers = request_headers(session, payload)
    url = f"/api/v1/sessions/{session['id']}/turns"
    wrong_header = {**headers, "Idempotency-Key": str(uuid4())}
    assert (await client.post(url, json=payload, headers=wrong_header)).status_code == 400
    assert (await client.post(url, json={**payload, "text": "  "}, headers=headers)).status_code == 422
    assert (await client.post(url, json={**payload, "expected_turn": 9}, headers=headers)).status_code == 409


async def test_fake_turn_completes_and_replays_without_inference(client):
    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    url = f"/api/v1/sessions/{session['id']}/turns"
    accepted = await client.post(url, json=payload, headers=headers)
    assert accepted.status_code == 202
    assert await process_next()
    status = await client.get(accepted.headers["location"], headers=headers)
    assert status.json()["status"] == "completed"
    assert status.json()["result"]["turn_number"] == 1
    assert status.json()["result"]["player_text"] == payload["text"]
    resumed = await client.get(f"/api/v1/sessions/{session['id']}", headers=headers)
    assert resumed.json()["turn_number"] == 1
    assert resumed.json()["pending_request"] is None
    history = await client.get(url, headers=headers)
    assert history.json()["turns"] == [status.json()["result"]]
    retry = await client.post(url, json=payload, headers=headers)
    assert retry.status_code == 202
    assert retry.json()["request_id"] == accepted.json()["request_id"]
    stale = turn_input()
    assert (await client.post(url, json=stale, headers=request_headers(session, stale))).status_code == 409


async def test_expired_lease_cannot_commit_and_only_one_turn_is_recorded(client):
    session = await new_session(client)
    payload = turn_input()
    accepted = await client.post(
        f"/api/v1/sessions/{session['id']}/turns",
        json=payload, headers=request_headers(session, payload),
    )
    assert accepted.status_code == 202
    first = await claim_next()
    assert first is not None
    async with session_factory() as db, db.begin():
        await db.execute(update(TurnRequest).where(TurnRequest.id == first.request_id).values(
            leased_until=func.now() - timedelta(seconds=1)
        ))
    second = await claim_next()
    assert second is not None
    assert second.lease_token != first.lease_token
    proposal = {"reply": "Forslaget er registreret."}
    assert not await complete_claim(first, proposal)
    assert await complete_claim(second, proposal)
    assert not await complete_claim(second, proposal)
    async with session_factory() as db:
        committed = (await db.execute(select(CommittedTurn).where(
            CommittedTurn.session_id == UUID(session["id"])
        ))).scalars().all()
        assert len(committed) == 1


async def test_two_workers_cannot_claim_the_same_request(client):
    session = await new_session(client)
    payload = turn_input()
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns",
        json=payload, headers=request_headers(session, payload),
    )
    assert response.status_code == 202
    claims = await asyncio.gather(claim_next(), claim_next())
    assert sum(claim is not None for claim in claims) == 1


async def test_generation_does_not_hold_a_session_transaction(client):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider:
        async def generate(self, text):
            entered.set()
            await release.wait()
            return {"reply": "Forslaget er registreret."}

    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    url = f"/api/v1/sessions/{session['id']}/turns"
    response = await client.post(url, json=payload, headers=headers)
    assert response.status_code == 202
    worker = asyncio.create_task(process_next(WaitingProvider()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = turn_input()
        conflict = await asyncio.wait_for(
            client.post(url, json=second, headers=request_headers(session, second)), timeout=2
        )
        assert conflict.status_code == 409
        assert conflict.json()["request_id"] == response.json()["request_id"]
    finally:
        release.set()
        await worker


async def test_resume_is_consistent_when_a_turn_completes_during_the_read(client, monkeypatch):
    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    url = f"/api/v1/sessions/{session['id']}"
    assert (await client.post(url + "/turns", json=payload, headers=headers)).status_code == 202
    read_started, release = asyncio.Event(), asyncio.Event()
    original = routes.owned_session

    async def paused_session(db, session_id, token, lock=False):
        result = await original(db, session_id, token, lock=lock)
        read_started.set()
        await release.wait()
        return result

    monkeypatch.setattr(routes, "owned_session", paused_session)
    resume = asyncio.create_task(client.get(url, headers=headers))
    worker = None
    try:
        await asyncio.wait_for(read_started.wait(), timeout=2)
        worker = asyncio.create_task(process_next())
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=0.7)
        except TimeoutError:
            pass
        release.set()
        response = await asyncio.wait_for(resume, timeout=2)
        await asyncio.wait_for(worker, timeout=2)
        snapshot = response.json()
        assert snapshot["turn_number"] == 1 or snapshot["pending_request"] is not None
    finally:
        release.set()
        if worker is not None:
            await worker
        await resume


async def test_poll_is_consistent_when_a_turn_completes_during_the_read(client, monkeypatch):
    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns", json=payload, headers=headers
    )
    read_started, release = asyncio.Event(), asyncio.Event()
    original = routes.owned_session

    async def paused_session(db, session_id, token, lock=False):
        result = await original(db, session_id, token, lock=lock)
        read_started.set()
        await release.wait()
        return result

    monkeypatch.setattr(routes, "owned_session", paused_session)
    poll = asyncio.create_task(client.get(response.headers["location"], headers=headers))
    worker = None
    try:
        await asyncio.wait_for(read_started.wait(), timeout=2)
        worker = asyncio.create_task(process_next())
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=0.7)
        except TimeoutError:
            pass
        release.set()
        snapshot = (await asyncio.wait_for(poll, timeout=2)).json()
        await asyncio.wait_for(worker, timeout=2)
        assert (snapshot["status"] == "completed") == (snapshot["result"] is not None)
    finally:
        release.set()
        if worker is not None:
            await worker
        await poll


async def test_failed_provider_retries_are_bounded(client):
    class BrokenProvider:
        async def generate(self, text):
            raise RuntimeError("private details")

    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns", json=payload, headers=headers
    )
    request_id = UUID(response.json()["request_id"])
    for _ in range(3):
        assert await process_next(BrokenProvider())
        async with session_factory() as db, db.begin():
            await db.execute(update(TurnRequest).where(TurnRequest.id == request_id).values(
                next_attempt_at=func.now() - timedelta(seconds=1)
            ))
    status = await client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "failed"
    assert status.json()["failure_code"] == "INTERNAL_ERROR"
    assert "private details" not in status.text


async def test_worker_does_not_commit_if_lease_expires_while_waiting_for_session_lock(client):
    session = await new_session(client)
    payload = turn_input()
    accepted = await client.post(
        f"/api/v1/sessions/{session['id']}/turns",
        json=payload, headers=request_headers(session, payload),
    )
    assert accepted.status_code == 202
    claim = await claim_next()
    async with session_factory() as db, db.begin():
        await db.execute(update(TurnRequest).where(TurnRequest.id == claim.request_id).values(
            leased_until=func.clock_timestamp() + timedelta(seconds=1)
        ))
    async with session_factory() as db, db.begin():
        await db.execute(select(GameSession).where(
            GameSession.id == UUID(session["id"])
        ).with_for_update())
        completion = asyncio.create_task(complete_claim(claim, {"reply": "Hej."}))
        await asyncio.sleep(1.5)
    assert not await completion
    async with session_factory() as db:
        assert (await db.execute(select(CommittedTurn).where(
            CommittedTurn.request_id == claim.request_id
        ))).scalar_one_or_none() is None


async def test_hung_provider_is_bounded_and_requeued(client, monkeypatch):
    class HungProvider:
        async def generate(self, text):
            await asyncio.sleep(1)
            return {"reply": "For sent."}

    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns", json=payload, headers=headers
    )
    monkeypatch.setattr(turn_worker, "PROVIDER_TIMEOUT_SECONDS", 0.05)
    assert await process_next(HungProvider())
    status = await client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "queued"
    assert status.json()["failure_code"] == "INTERNAL_ERROR"


async def test_malformed_proposal_cannot_commit_game_state(client):
    class InvalidProvider:
        async def generate(self, text):
            return {"reply": "", "unexpected": "value"}

    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns", json=payload, headers=headers
    )
    assert await process_next(InvalidProvider())
    status = await client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "queued"
    resumed = await client.get(f"/api/v1/sessions/{session['id']}", headers=headers)
    assert resumed.json()["turn_number"] == 0
    assert (await client.get(
        f"/api/v1/sessions/{session['id']}/turns", headers=headers
    )).json()["turns"] == []


async def test_expired_capability_cannot_read_session(client):
    session = await new_session(client)
    async with session_factory() as db, db.begin():
        await db.execute(update(GameSession).where(GameSession.id == UUID(session["id"])).values(
            capability_expires_at=func.now() - timedelta(seconds=1)
        ))
    response = await client.get(
        f"/api/v1/sessions/{session['id']}", headers={"Authorization": f"Bearer {session['token']}"}
    )
    assert response.status_code == 404


async def test_repeated_worker_crashes_eventually_fail_the_request(client):
    session = await new_session(client)
    payload = turn_input()
    headers = request_headers(session, payload)
    response = await client.post(
        f"/api/v1/sessions/{session['id']}/turns", json=payload, headers=headers
    )
    for _ in range(3):
        claim = await claim_next()
        assert claim is not None
        async with session_factory() as db, db.begin():
            await db.execute(update(TurnRequest).where(TurnRequest.id == claim.request_id).values(
                leased_until=func.now() - timedelta(seconds=1)
            ))
    assert await claim_next() is None
    status = await client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "failed"
    assert status.json()["failure_code"] == "MAX_ATTEMPTS_EXCEEDED"
