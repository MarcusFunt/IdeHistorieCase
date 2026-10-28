"""Lease and commit fake-provider turns without holding a transaction during generation."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, or_, select

from app.db.connection import session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, TurnRequest

logger = logging.getLogger(__name__)
LEASE_SECONDS = 60
MAX_ATTEMPTS = 3
PROVIDER_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Claim:
    request_id: UUID
    session_id: UUID
    lease_token: UUID
    text: str


class FakeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=600)

    @field_validator("reply")
    @classmethod
    def nonblank_reply(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply must not be blank")
        return value


class FakeProvider:
    async def generate(self, text: str) -> dict:
        return {"reply": "Dit forslag er registreret til den historiske drøftelse."}


async def claim_next() -> Claim | None:
    async with session_factory() as db, db.begin():
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        turn = (await db.execute(
            select(TurnRequest).where(or_(
                and_(TurnRequest.status == "queued", TurnRequest.next_attempt_at <= now),
                and_(TurnRequest.status == "running", TurnRequest.leased_until <= now),
            )).order_by(TurnRequest.created_at, TurnRequest.id)
            .with_for_update(skip_locked=True).limit(1)
        )).scalar_one_or_none()
        if turn is None:
            return None
        if turn.attempts >= MAX_ATTEMPTS:
            turn.status = "failed"
            turn.failure_code = "MAX_ATTEMPTS_EXCEEDED"
            turn.lease_token = None
            turn.leased_until = None
            turn.updated_at = now
            return None
        token = uuid4()
        turn.status = "running"
        turn.attempts += 1
        turn.lease_token = token
        turn.leased_until = now + timedelta(seconds=LEASE_SECONDS)
        turn.failure_code = None
        turn.updated_at = now
        return Claim(turn.id, turn.session_id, token, turn.text)


async def complete_claim(claim: Claim, proposal: dict) -> bool:
    validated = FakeProposal.model_validate(proposal)
    async with session_factory() as db, db.begin():
        # Submission also locks the session before a request, avoiding lock-order inversion.
        game = (await db.execute(select(GameSession).where(
            GameSession.id == claim.session_id
        ).with_for_update())).scalar_one_or_none()
        turn = (await db.execute(select(TurnRequest).where(
            TurnRequest.id == claim.request_id
        ).with_for_update())).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            game is None or turn is None or turn.session_id != claim.session_id
            or turn.status != "running" or turn.lease_token != claim.lease_token
            or turn.leased_until is None or turn.leased_until <= now
        ):
            return False
        if game.turn_number != turn.expected_turn:
            turn.status = "failed"
            turn.failure_code = "SESSION_CONFLICT"
            turn.lease_token = None
            turn.leased_until = None
            turn.updated_at = now
            return False
        number = game.turn_number + 1
        result = {
            "turn_number": number, "player_text": turn.text, "reply": validated.reply,
            "provider": "fake", "scenario_version_id": str(game.scenario_version_id),
        }
        committed = CommittedTurn(
            id=uuid4(), session_id=game.id, request_id=turn.id, turn_number=number, result=result,
        )
        db.add(committed)
        await db.flush()
        db.add(GameEvent(
            id=uuid4(), committed_turn_id=committed.id, event_type="player_argument",
            payload={"text": turn.text},
        ))
        game.turn_number = number
        game.state = {**game.state, "last_turn_number": number}
        turn.status = "completed"
        turn.lease_token = None
        turn.leased_until = None
        turn.updated_at = now
        return True


async def fail_claim(claim: Claim) -> bool:
    async with session_factory() as db, db.begin():
        turn = (await db.execute(select(TurnRequest).where(
            TurnRequest.id == claim.request_id
        ).with_for_update())).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            turn is None or turn.status != "running" or turn.lease_token != claim.lease_token
            or turn.leased_until is None or turn.leased_until <= now
        ):
            return False
        turn.failure_code = "INTERNAL_ERROR"
        turn.lease_token = None
        turn.leased_until = None
        turn.updated_at = now
        if turn.attempts >= MAX_ATTEMPTS:
            turn.status = "failed"
        else:
            turn.status = "queued"
            turn.next_attempt_at = now + timedelta(seconds=min(2 ** (turn.attempts - 1), 30))
        return True


async def process_next(provider=None) -> bool:
    claim = await claim_next()
    if claim is None:
        return False
    try:
        proposal = await asyncio.wait_for(
            (provider or FakeProvider()).generate(claim.text), timeout=PROVIDER_TIMEOUT_SECONDS
        )
        await complete_claim(claim, proposal)
    except Exception as exc:
        logger.warning("Turn processing failed (%s)", type(exc).__name__)
        await fail_claim(claim)
    return True


async def run_turn_worker() -> None:
    while True:
        try:
            await process_next()
        except Exception as exc:
            logger.error("Turn worker could not access the database (%s)", type(exc).__name__)
        await asyncio.sleep(1)
