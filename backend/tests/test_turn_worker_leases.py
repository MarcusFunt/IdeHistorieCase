"""Fenced turn leases survive slow inference and reject restarted workers."""
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.db.connection import session_factory
from app.db.models import CommittedTurn, TurnRequest
from app.game.turn_worker import claim_next, complete_claim, renew_claim


@pytest.mark.asyncio
async def test_renewal_extends_only_the_current_unexpired_claim(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    assert response.status_code == 202, response.text
    claim = await claim_next()
    assert claim is not None

    async with session_factory() as db:
        first = await db.get(TurnRequest, claim.request_id)
        first_expiry = first.leased_until
    assert await renew_claim(claim)
    async with session_factory() as db:
        renewed = await db.get(TurnRequest, claim.request_id)
        assert renewed.leased_until > first_expiry

    async with session_factory() as db, db.begin():
        await db.execute(
            update(TurnRequest)
            .where(TurnRequest.id == claim.request_id)
            .values(leased_until=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert not await renew_claim(claim)


@pytest.mark.asyncio
async def test_worker_restart_fences_old_token_from_renewal(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Tal med vidnet.")
    assert response.status_code == 202, response.text
    old_claim = await claim_next()
    assert old_claim is not None
    async with session_factory() as db, db.begin():
        await db.execute(
            update(TurnRequest)
            .where(TurnRequest.id == old_claim.request_id)
            .values(leased_until=func.clock_timestamp() - timedelta(seconds=1))
        )

    restarted_claim = await claim_next()
    assert restarted_claim is not None
    assert restarted_claim.lease_token != old_claim.lease_token
    assert not await complete_claim(old_claim, {"reply": "Gammelt svar."})
    assert not await renew_claim(old_claim)
    assert await renew_claim(restarted_claim)
    async with session_factory() as db:
        committed = (
            await db.execute(
                select(CommittedTurn).where(
                    CommittedTurn.session_id == UUID(session["id"])
                )
            )
        ).scalars().all()
    assert committed == []
