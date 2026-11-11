"""Lease, generate, and atomically commit durable game turns."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, or_, select, update

from app.core.settings import Settings, get_settings
from app.db.connection import session_factory
from app.db.models import (
    CommittedTurn,
    GameEvent,
    GameSession,
    HistoricalPassage,
    HistoricalPassageReview,
    ScenarioVersion,
    TurnRequest,
)
from app.game.interpreter import ScenarioTurnContext
from app.rag.policy import EvidenceContext, EvidencePassage, PassageApproval, is_eligible
from app.runtime.orchestrator import TurnGeneration, TurnGenerationFailure
from app.runtime.state import project_state
from app.scenarios.engine import ScenarioEventProposal, apply_event_proposals
from app.scenarios.evidence import available_source_ids, citation_source_ids
from app.scenarios.schema import ScenarioDefinition
from app.scenarios.schema_v2 import ScenarioDefinitionV2

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
PROVIDER_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Claim:
    request_id: UUID
    session_id: UUID
    lease_token: UUID
    text: str
    expected_turn: int
    attempt_number: int
    repair_used: bool
    runtime_version: str
    scenario_context: ScenarioTurnContext | None = None
    attempt_metadata: tuple[dict, ...] = ()


class LegacyProposal(BaseModel):
    """Compatibility shape for explicitly injected v1 adapters and test doubles."""

    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=600)
    events: list[ScenarioEventProposal] = Field(default_factory=list, max_length=32)

    @field_validator("reply")
    @classmethod
    def nonblank_reply(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply must not be blank")
        return value


async def _revalidate_turn_citations(db, game: GameSession, scenario, generation) -> None:
    cited_ids = {
        evidence_id
        for event in generation.events
        if getattr(event, "event_type", None) == "historical_claim_recorded"
        for evidence_id in event.evidence_ids
    }
    if not cited_ids:
        return

    supplied = generation.provenance.get("eligible_evidence")
    by_id = {
        item.get("passage_id"): item
        for item in supplied or []
        if isinstance(item, dict) and isinstance(item.get("passage_id"), str)
    }
    if not cited_ids <= by_id.keys():
        raise TurnGenerationFailure("MODEL_OUTPUT_INVALID", retryable=False)
    try:
        passage_ids = sorted((UUID(value) for value in cited_ids), key=str)
        definition = ScenarioDefinitionV2.model_validate(scenario.definition)
        role = next(item for item in definition.roles if item.id == game.player_role_id)
        actor = next(item for item in definition.actors if item.id == role.actor_id)
        world_date = date.fromisoformat(game.state["world_date"])
        evidence_context = EvidenceContext(
            scenario_version_id=game.scenario_version_id,
            scenario_slug=scenario.slug,
            scenario_version=scenario.version,
            actor_id=actor.id,
            world_date=world_date,
            mode="gameplay",
            evidence_policy=scenario.evidence_policy,
        )
        available_sources = available_source_ids(
            scenario.evidence_policy,
            world_date,
            actor_id=actor.id,
            mode="gameplay",
        )
        citable_sources = citation_source_ids(
            scenario.evidence_policy, available_sources
        )
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise TurnGenerationFailure("TURN_CONTEXT_INVALID", retryable=False) from exc

    passages = list(
        (
            await db.execute(
                select(HistoricalPassage)
                .where(HistoricalPassage.id.in_(passage_ids))
                .order_by(HistoricalPassage.id)
                .with_for_update()
            )
        ).scalars()
    )
    if len(passages) != len(passage_ids):
        raise TurnGenerationFailure("EVIDENCE_UNAVAILABLE", retryable=True, stage="commit")

    for passage in passages:
        recorded = by_id[str(passage.id)]
        if (
            recorded.get("passage_sha256") != passage.passage_sha256
            or passage.scenario_version_id != game.scenario_version_id
            or passage.source_identifier not in citable_sources
        ):
            raise TurnGenerationFailure("EVIDENCE_UNAVAILABLE", retryable=True, stage="commit")
        review = (
            await db.execute(
                select(HistoricalPassageReview)
                .where(HistoricalPassageReview.passage_id == passage.id)
                .order_by(HistoricalPassageReview.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            review is None
            or review.review_status != "approved"
            or review.rights_status != "approved"
            or recorded.get("review_revision") != review.revision
        ):
            raise TurnGenerationFailure("EVIDENCE_UNAVAILABLE", retryable=True, stage="commit")
        candidate = EvidencePassage(
            id=passage.id,
            scenario_version_id=passage.scenario_version_id,
            scenario_slug=passage.scenario_slug,
            scenario_version=passage.scenario_version,
            passage_key=passage.passage_key,
            source_identifier=passage.source_identifier,
            source_title=passage.source_title,
            source_url=passage.source_url,
            source_anchor=passage.source_anchor,
            text=passage.passage_text,
            passage_sha256=passage.passage_sha256,
            document_date=passage.document_date,
            in_world_available_from=passage.in_world_available_from,
            postgame_only=passage.postgame_only,
            actor_ids=tuple(passage.actor_ids),
            visibility_scope=passage.visibility_scope,
        )
        if not is_eligible(
            candidate,
            evidence_context,
            PassageApproval(
                review_status=review.review_status,
                rights_status=review.rights_status,
                revision=review.revision,
            ),
            scenario.evidence_policy,
        ):
            raise TurnGenerationFailure("EVIDENCE_UNAVAILABLE", retryable=True, stage="commit")


async def claim_next(settings: Settings | None = None) -> Claim | None:
    settings = settings or get_settings()
    async with session_factory() as db, db.begin():
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        turn = (
            await db.execute(
                select(TurnRequest)
                .where(
                    or_(
                        and_(
                            TurnRequest.status == "queued",
                            TurnRequest.next_attempt_at <= now,
                        ),
                        and_(
                            TurnRequest.status == "running",
                            TurnRequest.leased_until <= now,
                        ),
                    )
                )
                .order_by(TurnRequest.created_at, TurnRequest.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if turn is None:
            return None
        if turn.attempts >= MAX_ATTEMPTS:
            turn.status = "failed" if turn.client_retry_used else "retryable_failure"
            turn.failure_code = "MAX_ATTEMPTS_EXCEEDED"
            turn.failure_stage = "worker"
            turn.failure_message = "Turn processing attempts are exhausted."
            turn.lease_token = None
            turn.leased_until = None
            turn.updated_at = now
            return None

        token = uuid4()
        turn.status = "running"
        turn.attempts += 1
        turn.lease_token = token
        turn.leased_until = now + timedelta(seconds=settings.turn_lease_seconds)
        turn.updated_at = now
        game = await db.get(GameSession, turn.session_id)
        scenario = await db.get(ScenarioVersion, game.scenario_version_id) if game else None
        if game is None or scenario is None:
            turn.status = "failed"
            turn.failure_code = "TURN_CONTEXT_INVALID"
            turn.lease_token = None
            turn.leased_until = None
            return None
        context = None
        if game.runtime_version == "v1_legacy":
            context = ScenarioTurnContext(
                scenario=scenario.slug,
                scenario_version=scenario.version,
                player_role_id=game.player_role_id,
                state=game.state,
                definition=scenario.definition,
                behavior_rules=scenario.behavior_rules,
                evidence_policy=scenario.evidence_policy,
            )
        return Claim(
            request_id=turn.id,
            session_id=turn.session_id,
            lease_token=token,
            text=turn.text,
            expected_turn=turn.expected_turn,
            attempt_number=turn.attempts,
            repair_used=turn.repair_used,
            runtime_version=game.runtime_version,
            scenario_context=context,
            attempt_metadata=tuple(turn.attempt_metadata or ()),
        )


async def renew_claim(claim: Claim, settings: Settings | None = None) -> bool:
    """Extend only the current, still-live owner using the database clock."""
    settings = settings or get_settings()
    async with session_factory() as db, db.begin():
        result = await db.execute(
            update(TurnRequest)
            .where(
                TurnRequest.id == claim.request_id,
                TurnRequest.session_id == claim.session_id,
                TurnRequest.status == "running",
                TurnRequest.lease_token == claim.lease_token,
                TurnRequest.leased_until > func.clock_timestamp(),
            )
            .values(
                leased_until=func.clock_timestamp()
                + timedelta(seconds=settings.turn_lease_seconds),
                updated_at=func.clock_timestamp(),
            )
        )
        return result.rowcount == 1


async def mark_repair_attempt(claim: Claim) -> bool:
    """Consume the request's one repair slot while the caller still owns its lease."""
    async with session_factory() as db, db.begin():
        result = await db.execute(
            update(TurnRequest)
            .where(
                TurnRequest.id == claim.request_id,
                TurnRequest.session_id == claim.session_id,
                TurnRequest.status == "running",
                TurnRequest.lease_token == claim.lease_token,
                TurnRequest.leased_until > func.clock_timestamp(),
                TurnRequest.repair_used.is_(False),
            )
            .values(repair_used=True, updated_at=func.clock_timestamp())
        )
        return result.rowcount == 1


async def record_attempt_metadata(claim: Claim, metadata: dict) -> bool:
    """Append only allowlisted diagnostic metadata while the claim remains live."""
    allowed = {
        "attempt_number",
        "call_number",
        "started_at",
        "finished_at",
        "duration_ms",
        "outcome",
        "failure_code",
        "validation_codes",
        "output_sha256",
        "prompt_sha256",
        "provider_request_id",
        "requested_model",
        "served_model",
        "model_revision",
        "prompt_tokens",
        "completion_tokens",
        "tool_calls",
        "tool_calls_count",
        "final_correction",
        "enable_thinking",
        "max_output_tokens",
    }
    safe = {key: value for key, value in metadata.items() if key in allowed}
    async with session_factory() as db, db.begin():
        turn = (
            await db.execute(
                select(TurnRequest)
                .where(TurnRequest.id == claim.request_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            turn is None
            or turn.session_id != claim.session_id
            or turn.status != "running"
            or turn.lease_token != claim.lease_token
            or turn.leased_until is None
            or turn.leased_until <= now
        ):
            return False
        turn.attempt_metadata = [*(turn.attempt_metadata or []), safe][-7:]
        turn.updated_at = now
        return True


async def complete_claim(claim: Claim, proposal: dict | TurnGeneration) -> bool:
    generation = proposal if isinstance(proposal, TurnGeneration) else None
    validated = None if generation is not None else LegacyProposal.model_validate(proposal)
    async with session_factory() as db, db.begin():
        # Route submission and commit share this lock order: session, then request.
        game = (
            await db.execute(
                select(GameSession)
                .where(GameSession.id == claim.session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        turn = (
            await db.execute(
                select(TurnRequest)
                .where(TurnRequest.id == claim.request_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            game is None
            or turn is None
            or turn.session_id != claim.session_id
            or turn.status != "running"
            or turn.lease_token != claim.lease_token
            or turn.leased_until is None
            or turn.leased_until <= now
        ):
            return False
        if game.turn_number != turn.expected_turn or turn.expected_turn != claim.expected_turn:
            turn.status = "failed"
            turn.failure_code = "SESSION_CONFLICT"
            turn.lease_token = None
            turn.leased_until = None
            turn.updated_at = now
            return False

        if generation is None and game.runtime_version != "v1_legacy":
            turn.status = "failed"
            turn.failure_code = "INFERENCE_UNAVAILABLE"
            turn.lease_token = None
            turn.leased_until = None
            turn.updated_at = now
            return False

        scenario = await db.get(ScenarioVersion, game.scenario_version_id)
        number = game.turn_number + 1
        if generation is not None:
            if (
                game.runtime_version != "v2_ai"
                or scenario is None
                or scenario.version != generation.scenario_version
            ):
                turn.status = "failed"
                turn.failure_code = "TURN_CONTEXT_INVALID"
                turn.lease_token = None
                turn.leased_until = None
                turn.updated_at = now
                return False
            pinned_scenario = generation.provenance.get("scenario")
            if (
                not isinstance(pinned_scenario, dict)
                or pinned_scenario.get("release_id") != str(game.scenario_version_id)
                or pinned_scenario.get("slug") != scenario.slug
                or pinned_scenario.get("version") != scenario.version
            ):
                raise TurnGenerationFailure("TURN_CONTEXT_INVALID", retryable=False)
            await _revalidate_turn_citations(db, game, scenario, generation)
            accepted_events = [
                event.model_dump(mode="json") for event in generation.events
            ]
            next_state = project_state(game.state, generation.events)
            next_state["turn_number"] = number
            next_state["last_turn_number"] = number
            model_proposal = generation.proposal
            result = {
                "turn_number": number,
                "player_text": turn.text,
                "reply": model_proposal.narration,
                "dialogue": [line.model_dump(mode="json") for line in model_proposal.dialogue],
                "outcome_summary": model_proposal.outcome_summary,
                "next_player_prompt": model_proposal.next_player_prompt,
                "scene_status": model_proposal.scene_status,
                "provider": generation.provenance,
                "scenario_version_id": str(game.scenario_version_id),
                "scenario_version": generation.scenario_version,
                "events": accepted_events,
                "state": next_state,
            }
        else:
            assert validated is not None
            next_state = dict(game.state)
            accepted_events = []
            if scenario is not None and scenario.definition is not None:
                definition = ScenarioDefinition.model_validate(scenario.definition)
                next_state, accepted_events = apply_event_proposals(
                    definition,
                    game.state,
                    validated.events,
                    behavior_rules=scenario.behavior_rules,
                )
            elif validated.events:
                raise ValueError("scenario event proposals require an authored scenario definition")
            result = {
                "turn_number": number,
                "player_text": turn.text,
                "reply": validated.reply,
                "provider": "explicit-legacy-adapter",
                "scenario_version_id": str(game.scenario_version_id),
            }
            if scenario is not None and scenario.definition is not None:
                result["scenario_version"] = scenario.version
                result["events"] = accepted_events
                result["state"] = next_state

        committed = CommittedTurn(
            id=uuid4(),
            session_id=game.id,
            request_id=turn.id,
            turn_number=number,
            result=result,
        )
        db.add(committed)
        await db.flush()
        db.add(
            GameEvent(
                id=uuid4(),
                committed_turn_id=committed.id,
                event_index=0,
                event_type="player_argument",
                payload={"text": turn.text},
            )
        )
        if generation is not None:
            for index, event in enumerate(generation.events, start=1):
                payload = event.model_dump(mode="json", exclude={"event_type"})
                db.add(
                    GameEvent(
                        id=uuid4(),
                        committed_turn_id=committed.id,
                        event_index=index,
                        event_type=event.event_type,
                        payload=payload,
                    )
                )
        else:
            for index, event in enumerate(accepted_events, start=1):
                db.add(
                    GameEvent(
                        id=uuid4(),
                        committed_turn_id=committed.id,
                        event_index=index,
                        event_type=event["type"],
                        payload=event,
                    )
                )
        game.turn_number = number
        game.state = {**next_state, "last_turn_number": number}
        turn.status = "completed"
        turn.failure_code = None
        turn.failure_stage = None
        turn.failure_message = None
        turn.lease_token = None
        turn.leased_until = None
        turn.updated_at = now
        return True


async def fail_claim(
    claim: Claim,
    failure_code: str = "INTERNAL_ERROR",
    *,
    retryable: bool = True,
    failure_stage: str = "worker",
    failure_message: str = "The turn could not be processed.",
) -> bool:
    async with session_factory() as db, db.begin():
        turn = (
            await db.execute(
                select(TurnRequest)
                .where(TurnRequest.id == claim.request_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            turn is None
            or turn.session_id != claim.session_id
            or turn.status != "running"
            or turn.lease_token != claim.lease_token
            or turn.leased_until is None
            or turn.leased_until <= now
        ):
            return False
        turn.failure_code = failure_code
        turn.failure_stage = failure_stage
        turn.failure_message = failure_message
        turn.lease_token = None
        turn.leased_until = None
        turn.updated_at = now
        if retryable and turn.attempts < MAX_ATTEMPTS:
            turn.status = "queued"
            turn.next_attempt_at = now + timedelta(
                seconds=min(2 ** (turn.attempts - 1), 30)
            )
        else:
            turn.status = "failed" if turn.client_retry_used or not retryable else "retryable_failure"
        return True


async def _call_explicit_v1_provider(provider, claim: Claim, settings: Settings) -> dict:
    if claim.runtime_version != "v1_legacy":
        raise TurnGenerationFailure("INFERENCE_UNAVAILABLE", retryable=True)
    interpret = getattr(provider, "interpret", None)
    if callable(interpret):
        if claim.scenario_context is None:
            raise TurnGenerationFailure("TURN_CONTEXT_INVALID", retryable=False)
        pending = interpret(claim.text, claim.scenario_context)
    else:
        generate = getattr(provider, "generate", None)
        if not callable(generate):
            raise TurnGenerationFailure("INFERENCE_UNAVAILABLE", retryable=True)
        pending = generate(claim.text)
    timeout = min(PROVIDER_TIMEOUT_SECONDS, settings.turn_inference_timeout_seconds)
    try:
        return await asyncio.wait_for(pending, timeout=timeout)
    except TimeoutError as exc:
        raise TurnGenerationFailure("INFERENCE_TIMEOUT", retryable=True) from exc


async def process_next(
    provider=None,
    *,
    orchestrator=None,
    settings: Settings | None = None,
) -> bool:
    """Process one claim; production must inject the real structured orchestrator."""
    settings = settings or get_settings()
    claim = await claim_next(settings)
    if claim is None:
        return False

    lease_lost = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.turn_lease_renew_interval_seconds)
            try:
                renewed = await renew_claim(claim, settings)
            except Exception:
                logger.exception("Turn lease renewal failed")
                renewed = False
            if not renewed:
                lease_lost.set()
                return

    async def run_generation():
        if orchestrator is not None:
            generated = await orchestrator.generate(
                claim,
                authorize_repair=lambda: mark_repair_attempt(claim),
                record_attempt=lambda metadata: record_attempt_metadata(claim, metadata),
            )
            return await complete_claim(claim, generated)
        if provider is not None:
            raw_proposal = await _call_explicit_v1_provider(provider, claim, settings)
            return await complete_claim(claim, raw_proposal)
        raise TurnGenerationFailure("INFERENCE_UNAVAILABLE", retryable=True)

    heartbeat_task = asyncio.create_task(heartbeat())
    generation_task = asyncio.create_task(run_generation())
    lease_lost_task = asyncio.create_task(lease_lost.wait())
    failure: TurnGenerationFailure | None = None
    try:
        done, _ = await asyncio.wait(
            {generation_task, lease_lost_task},
            timeout=settings.turn_total_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_lost_task in done and lease_lost.is_set():
            generation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await generation_task
            return True
        if generation_task not in done:
            generation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await generation_task
            failure = TurnGenerationFailure("TURN_TOTAL_TIMEOUT", retryable=True)
        else:
            try:
                await generation_task
            except TurnGenerationFailure as exc:
                failure = exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Turn processing failed (%s)", type(exc).__name__)
                failure = TurnGenerationFailure("INTERNAL_ERROR", retryable=True)
    finally:
        for task in (generation_task, heartbeat_task, lease_lost_task):
            if not task.done():
                task.cancel()
        for task in (generation_task, heartbeat_task, lease_lost_task):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    if failure is not None:
        await fail_claim(
            claim,
            failure.code,
            retryable=failure.retryable,
            failure_stage=failure.stage,
            failure_message=failure.message,
        )
    return True


async def run_turn_worker(orchestrator=None, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    while True:
        try:
            await process_next(orchestrator=orchestrator, settings=settings)
        except Exception as exc:
            logger.error("Turn worker could not access the database (%s)", type(exc).__name__)
        await asyncio.sleep(1)
