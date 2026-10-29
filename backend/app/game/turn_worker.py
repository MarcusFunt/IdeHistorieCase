"""Lease and commit fake-provider turns without holding a transaction during generation."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, or_, select

from app.db.connection import session_factory
from app.db.models import CommittedTurn, GameEvent, GameSession, ScenarioVersion, TurnRequest
from app.game.interpreter import ScenarioTurnContext
from app.scenarios.engine import ScenarioEventProposal, apply_event_proposals
from app.scenarios.schema import ScenarioDefinition

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
    scenario_context: ScenarioTurnContext | None = None


class FakeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=600)
    events: list[ScenarioEventProposal] = Field(default_factory=list, max_length=32)

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
        game = await db.get(GameSession, turn.session_id)
        scenario = await db.get(ScenarioVersion, game.scenario_version_id) if game else None
        context = None
        if game is not None and scenario is not None:
            context = ScenarioTurnContext(
                scenario=scenario.slug,
                scenario_version=scenario.version,
                player_role_id=game.player_role_id,
                state=game.state,
                definition=scenario.definition,
                behavior_rules=scenario.behavior_rules,
                evidence_policy=scenario.evidence_policy,
            )
        return Claim(turn.id, turn.session_id, token, turn.text, context)


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

        scenario = await db.get(ScenarioVersion, game.scenario_version_id)
        next_state = dict(game.state)
        accepted_events = []
        if scenario is not None and scenario.definition is not None:
            definition = ScenarioDefinition.model_validate(scenario.definition)
            next_state, accepted_events = apply_event_proposals(
                definition, game.state, validated.events, behavior_rules=scenario.behavior_rules
            )
        elif validated.events:
            raise ValueError("scenario event proposals require an authored scenario definition")

        number = game.turn_number + 1
        result = {
            "turn_number": number, "player_text": turn.text, "reply": validated.reply,
            "provider": "fake", "scenario_version_id": str(game.scenario_version_id),
        }
        if scenario is not None and scenario.definition is not None:
            result["scenario_version"] = scenario.version
            result["events"] = accepted_events
            result["state"] = next_state
        committed = CommittedTurn(
            id=uuid4(), session_id=game.id, request_id=turn.id, turn_number=number, result=result,
        )
        db.add(committed)
        await db.flush()
        db.add(GameEvent(
            id=uuid4(), committed_turn_id=committed.id, event_index=0,
            event_type="player_argument", payload={"text": turn.text},
        ))
        for index, event in enumerate(accepted_events, start=1):
            db.add(GameEvent(
                id=uuid4(),
                committed_turn_id=committed.id,
                event_index=index,
                event_type=event["type"],
                payload=event,
            ))
        game.turn_number = number
        game.state = {**next_state, "last_turn_number": number}
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
        active_provider = provider or FakeProvider()
        interpret = getattr(active_provider, "interpret", None)
        if callable(interpret):
            if claim.scenario_context is None:
                raise ValueError("scenario interpreter requires a persisted scenario context")
            pending_proposal = interpret(claim.text, claim.scenario_context)
        else:
            pending_proposal = active_provider.generate(claim.text)
        proposal = await asyncio.wait_for(pending_proposal, timeout=PROVIDER_TIMEOUT_SECONDS)
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
