"""Durable session initialization jobs with fenced leases and bounded retries."""
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.connection import session_factory
from app.db.models import GameSession, SessionInitializationJob

LEASE_SECONDS = 60
LEASE_RENEW_INTERVAL_SECONDS = 20
MAX_ATTEMPTS_PER_CYCLE = 3
MAX_CLIENT_RETRIES = 1
MAX_RETRY_DELAY_SECONDS = 30
FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class InitializationClaim:
    job_id: UUID
    session_id: UUID
    lease_token: UUID
    attempts: int
    cycle_attempts: int
    scenario_version_id: UUID
    player_role_id: str | None
    repair_used: bool


class InitializationNotRetryable(ValueError):
    """The session has no remaining client retry cycle."""


async def enqueue_initialization(db: AsyncSession, session_id: UUID) -> SessionInitializationJob:
    """Create one job per session; concurrent or repeated enqueues return the same row."""
    table = SessionInitializationJob.__table__
    statement = insert(table).values(id=uuid4(), session_id=session_id)
    await db.execute(statement.on_conflict_do_nothing(index_elements=[table.c.session_id]))
    return (
        await db.execute(
            select(SessionInitializationJob).where(
                SessionInitializationJob.session_id == session_id
            )
        )
    ).scalar_one()


async def get_session_job(
    db: AsyncSession, session_id: UUID, *, lock: bool = False
) -> SessionInitializationJob | None:
    statement = select(SessionInitializationJob).where(
        SessionInitializationJob.session_id == session_id
    )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def claim_next() -> InitializationClaim | None:
    """Claim one due job; expired leases are fenced by a fresh token."""
    async with session_factory() as db, db.begin():
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        job = (
            await db.execute(
                select(SessionInitializationJob)
                .where(
                    or_(
                        and_(
                            SessionInitializationJob.status == "queued",
                            SessionInitializationJob.next_attempt_at <= now,
                        ),
                        and_(
                            SessionInitializationJob.status == "running",
                            SessionInitializationJob.leased_until <= now,
                        ),
                    )
                )
                .order_by(SessionInitializationJob.created_at, SessionInitializationJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return None

        if job.cycle_attempts >= MAX_ATTEMPTS_PER_CYCLE:
            job.status = "failed" if job.client_retry_used else "retryable_failure"
            job.failure_code = job.failure_code or "ATTEMPTS_EXHAUSTED"
            job.failure_stage = job.failure_stage or "worker"
            job.lease_token = None
            job.leased_until = None
            job.updated_at = now
            return None

        session = await db.get(GameSession, job.session_id)
        if session is None:
            job.status = "failed"
            job.failure_code = "SESSION_NOT_FOUND"
            job.failure_stage = "worker"
            job.lease_token = None
            job.leased_until = None
            job.updated_at = now
            return None

        token = uuid4()
        job.status = "running"
        job.attempts += 1
        job.cycle_attempts += 1
        job.lease_token = token
        job.leased_until = now + timedelta(seconds=LEASE_SECONDS)
        job.failure_code = None
        job.failure_stage = None
        job.updated_at = now
        return InitializationClaim(
            job_id=job.id,
            session_id=job.session_id,
            lease_token=token,
            attempts=job.attempts,
            cycle_attempts=job.cycle_attempts,
            scenario_version_id=session.scenario_version_id,
            player_role_id=session.player_role_id,
            repair_used=job.repair_used,
        )


async def mark_repair_attempt(claim: InitializationClaim) -> bool:
    """Spend the job's single repair token before making the second model call."""
    async with session_factory() as db, db.begin():
        job = (
            await db.execute(
                select(SessionInitializationJob)
                .where(SessionInitializationJob.id == claim.job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            job is None
            or job.session_id != claim.session_id
            or job.status != "running"
            or job.lease_token != claim.lease_token
            or job.leased_until is None
            or job.leased_until <= now
            or job.repair_used
        ):
            return False
        job.repair_used = True
        job.updated_at = now
        return True


async def renew_claim(claim: InitializationClaim) -> bool:
    """Extend a live fenced lease in a short transaction between inference waits."""
    async with session_factory() as db, db.begin():
        job = (
            await db.execute(
                select(SessionInitializationJob)
                .where(SessionInitializationJob.id == claim.job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            job is None
            or job.session_id != claim.session_id
            or job.status != "running"
            or job.lease_token != claim.lease_token
            or job.leased_until is None
            or job.leased_until <= now
        ):
            return False
        job.leased_until = now + timedelta(seconds=LEASE_SECONDS)
        job.updated_at = now
        return True


async def complete_claim(
    claim: InitializationClaim,
    opening: dict,
    initial_state: dict,
    *,
    provenance: dict | None = None,
    attempt_metadata: list[dict] | None = None,
) -> bool:
    """Commit a previously validated opening and state projection exactly once.

    Validation and inference belong to the caller and must finish before this
    function is called. This function never invokes a provider.
    """
    if not isinstance(opening, dict) or not isinstance(initial_state, dict):
        raise ValueError("opening and initial_state must be JSON objects")
    if provenance is not None and not isinstance(provenance, dict):
        raise ValueError("provenance must be a JSON object")
    attempt_metadata = attempt_metadata or []
    if not isinstance(attempt_metadata, list) or len(attempt_metadata) > 16:
        raise ValueError("attempt_metadata must be a bounded JSON list")
    try:
        json.dumps(opening, ensure_ascii=False, allow_nan=False)
        json.dumps(initial_state, ensure_ascii=False, allow_nan=False)
        json.dumps(provenance, ensure_ascii=False, allow_nan=False)
        json.dumps(attempt_metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("opening and initial_state must contain valid JSON values") from exc

    async with session_factory() as db, db.begin():
        # Match the turn worker's lock order: session first, then its job.
        session = (
            await db.execute(
                select(GameSession)
                .where(GameSession.id == claim.session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        job = (
            await db.execute(
                select(SessionInitializationJob)
                .where(SessionInitializationJob.id == claim.job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            session is None
            or job is None
            or job.session_id != claim.session_id
            or job.status != "running"
            or job.lease_token != claim.lease_token
            or job.leased_until is None
            or job.leased_until <= now
        ):
            return False

        session.state = initial_state
        merged_attempt_metadata = [*(job.attempt_metadata or []), *attempt_metadata]
        job.attempt_metadata = merged_attempt_metadata
        job.result = {
            "opening": opening,
            "initial_state": initial_state,
            "attempt_metadata": merged_attempt_metadata,
        }
        if provenance is not None:
            job.result["provenance"] = provenance
        job.status = "ready"
        job.failure_code = None
        job.lease_token = None
        job.leased_until = None
        job.updated_at = now
        return True


async def fail_claim(
    claim: InitializationClaim,
    failure_code: str,
    *,
    retryable: bool,
    failure_stage: str = "worker",
    attempt_metadata: list[dict] | None = None,
) -> bool:
    """Record a safe failure code, requeue transient faults, or end the retry cycle."""
    if not isinstance(failure_code, str) or not FAILURE_CODE_PATTERN.fullmatch(failure_code):
        raise ValueError("failure_code must be a stable uppercase code")
    if failure_stage not in {"context", "model", "validation", "worker"}:
        raise ValueError("failure_stage must be a stable initialization stage")
    attempt_metadata = attempt_metadata or []
    if not isinstance(attempt_metadata, list) or len(attempt_metadata) > 16:
        raise ValueError("attempt_metadata must be a bounded JSON list")
    try:
        json.dumps(attempt_metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("attempt_metadata must contain valid JSON values") from exc

    async with session_factory() as db, db.begin():
        job = (
            await db.execute(
                select(SessionInitializationJob)
                .where(SessionInitializationJob.id == claim.job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            job is None
            or job.session_id != claim.session_id
            or job.status != "running"
            or job.lease_token != claim.lease_token
            or job.leased_until is None
            or job.leased_until <= now
        ):
            return False

        job.lease_token = None
        job.leased_until = None
        job.updated_at = now
        job.attempt_metadata = [*(job.attempt_metadata or []), *attempt_metadata]
        if not retryable:
            job.status = "failed"
            job.failure_code = failure_code
            job.failure_stage = failure_stage
        elif job.cycle_attempts >= MAX_ATTEMPTS_PER_CYCLE:
            job.status = "failed" if job.client_retry_used else "retryable_failure"
            job.failure_code = failure_code
            job.failure_stage = failure_stage
        else:
            job.status = "queued"
            job.failure_code = None
            job.failure_stage = None
            delay = min(2 ** (job.cycle_attempts - 1), MAX_RETRY_DELAY_SECONDS)
            job.next_attempt_at = now + timedelta(seconds=delay)
        return True


async def request_retry(
    db: AsyncSession, session_id: UUID
) -> SessionInitializationJob:
    """Start the single client retry cycle, idempotently for repeated requests."""
    job = await get_session_job(db, session_id, lock=True)
    if job is None:
        return await enqueue_initialization(db, session_id)
    if job.status in {"queued", "running", "ready"}:
        return job
    if job.status != "retryable_failure" or job.client_retry_used:
        raise InitializationNotRetryable("session initialization has no retry remaining")

    now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
    job.status = "queued"
    job.cycle_attempts = 0
    job.client_retry_used = True
    job.next_attempt_at = now
    job.failure_code = None
    job.failure_stage = None
    job.updated_at = now
    return job
