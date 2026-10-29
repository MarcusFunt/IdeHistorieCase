"""Session-scoped M2 API; generation is handled by the separate worker."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.connection import session_factory
from app.db.models import CommittedTurn, GameSession, ScenarioVersion, TurnRequest
from app.scenarios.repository import get_active_scenario

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)
ACTIVE_STATUSES = ("queued", "running")
CAPABILITY_LIFETIME = timedelta(hours=24)


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str = Field(min_length=1, max_length=80)
    role_id: str | None = Field(default=None, min_length=2, max_length=64)


class SubmitTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_turn_id: UUID
    expected_turn: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2000)
    input_language: Literal["da"]

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required", headers={
            "WWW-Authenticate": "Bearer"
        })
    return credentials.credentials


async def owned_session(db: AsyncSession, session_id: UUID, token: str, lock: bool = False):
    statement = select(GameSession).where(GameSession.id == session_id)
    if lock:
        statement = statement.with_for_update()
    session = (await db.execute(statement)).scalar_one_or_none()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if (
        session is None
        or not hmac.compare_digest(session.capability_hash, digest)
        or session.capability_expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def public_request(turn: TurnRequest) -> dict:
    return {
        "request_id": str(turn.id),
        "status": turn.status,
        "poll_after_ms": 1000,
    }


@router.get("/scenarios")
async def list_scenarios():
    async with session_factory() as db:
        rows = (await db.execute(select(ScenarioVersion).order_by(
            ScenarioVersion.slug,
            ScenarioVersion.is_active.desc(),
            ScenarioVersion.created_at,
            ScenarioVersion.version,
        ))).scalars().all()
        result = []
        for row in rows:
            definition = row.definition
            metadata = definition.get("metadata", {}) if definition else {}
            result.append({
                "slug": row.slug,
                "version": row.version,
                "title": metadata.get("title"),
                "summary": metadata.get("summary"),
                "default_language": metadata.get("default_language"),
                "supported_roles": [
                    role["id"] for role in definition.get("roles", []) if role.get("playable")
                ] if definition else [],
                "is_active": row.is_active,
                "is_authored": definition is not None,
            })
        return result


@router.get("/scenarios/{slug}")
async def get_scenario(slug: str):
    async with session_factory() as db:
        scenario = await get_active_scenario(db, slug)
        if scenario is None:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if scenario.definition is None:
            raise HTTPException(
                status_code=409,
                detail="Active scenario version is an unauthored legacy stub and is not playable",
            )
        definition = scenario.definition
        return {
            "slug": scenario.slug,
            "version": scenario.version,
            "metadata": definition["metadata"],
            "historical_context": definition["historical_context"],
            "actors": definition["actors"],
            "roles": [role for role in definition["roles"] if role["playable"]],
            "default_role_id": definition["default_role_id"],
            "learning_objectives": definition["learning_objectives"],
            "phases": definition["phases"],
            "dilemmas": definition["dilemmas"],
        }


@router.post("/sessions", status_code=201)
async def create_session(payload: CreateSession):
    token = secrets.token_urlsafe(32)
    async with session_factory() as db:
        async with db.begin():
            scenario = await get_active_scenario(db, payload.scenario, lock=True)
            if scenario is None:
                raise HTTPException(status_code=404, detail="Scenario not found")
            player_role_id = None
            if scenario.definition is not None:
                playable_roles = {
                    role["id"] for role in scenario.definition["roles"] if role["playable"]
                }
                player_role_id = payload.role_id or scenario.definition["default_role_id"]
                if player_role_id not in playable_roles:
                    raise HTTPException(status_code=422, detail="role_id is not a playable role")
            elif payload.role_id is not None:
                raise HTTPException(status_code=422, detail="Legacy scenario does not support roles")
            session = GameSession(
                id=uuid4(), scenario_version_id=scenario.id,
                player_role_id=player_role_id,
                capability_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                capability_expires_at=datetime.now(timezone.utc) + CAPABILITY_LIFETIME,
                turn_number=0, state=dict(scenario.initial_state),
            )
            db.add(session)
            return {
                "id": str(session.id), "token": token, "turn_number": 0,
                "scenario": scenario.slug, "scenario_version": scenario.version,
                "role_id": player_role_id,
                "token_expires_at": session.capability_expires_at.isoformat(),
            }


@router.get("/sessions/{session_id}")
async def resume_session(session_id: UUID, token: Annotated[str, Depends(require_token)]):
    async with session_factory() as db:
        # Keep completion from changing the state before the pending request is read.
        session = await owned_session(db, session_id, token, lock=True)
        pending = (await db.execute(select(TurnRequest).where(
            TurnRequest.session_id == session.id, TurnRequest.status.in_(ACTIVE_STATUSES)
        ))).scalar_one_or_none()
        scenario = await db.get(ScenarioVersion, session.scenario_version_id)
        return {
            "id": str(session.id), "turn_number": session.turn_number, "state": session.state,
            "pending_request": public_request(pending) if pending else None,
            "scenario": scenario.slug,
            "scenario_version": scenario.version,
            "player_role_id": session.player_role_id,
        }

@router.post("/sessions/{session_id}/turns", status_code=202)
async def submit_turn(
    session_id: UUID,
    payload: SubmitTurn,
    response: Response,
    token: Annotated[str, Depends(require_token)],
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    if idempotency_key != payload.client_turn_id:
        raise HTTPException(status_code=400, detail="Idempotency key must match client_turn_id")
    async with session_factory() as db:
        async with db.begin():
            session = await owned_session(db, session_id, token, lock=True)
            existing = (await db.execute(select(TurnRequest).where(
                TurnRequest.session_id == session.id,
                TurnRequest.client_turn_id == payload.client_turn_id,
            ))).scalar_one_or_none()
            if existing:
                if (existing.expected_turn != payload.expected_turn or existing.text != payload.text
                        or existing.input_language != payload.input_language):
                    raise HTTPException(status_code=409, detail="Idempotency key already used")
                response.headers["Location"] = f"/api/v1/turn-requests/{existing.id}"
                return public_request(existing)
            pending = (await db.execute(select(TurnRequest).where(
                TurnRequest.session_id == session.id, TurnRequest.status.in_(ACTIVE_STATUSES)
            ))).scalar_one_or_none()
            if pending:
                return JSONResponse(status_code=409, content={
                    "detail": "A turn is already in progress", "request_id": str(pending.id)
                })
            if payload.expected_turn != session.turn_number:
                raise HTTPException(status_code=409, detail="Expected turn does not match session")
            turn = TurnRequest(
                id=uuid4(), session_id=session.id, client_turn_id=payload.client_turn_id,
                expected_turn=payload.expected_turn, text=payload.text,
                input_language=payload.input_language, status="queued", attempts=0,
            )
            db.add(turn)
            response.headers["Location"] = f"/api/v1/turn-requests/{turn.id}"
            return public_request(turn)


@router.get("/turn-requests/{request_id}")
async def get_turn_request(request_id: UUID, token: Annotated[str, Depends(require_token)]):
    async with session_factory() as db:
        turn = await db.get(TurnRequest, request_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="Turn request not found")
        await owned_session(db, turn.session_id, token, lock=True)
        # A completion may have committed while the session lock was acquired.
        await db.refresh(turn)
        result = (await db.execute(
            select(CommittedTurn).where(CommittedTurn.request_id == turn.id)
        )).scalar_one_or_none()
        return {
            **public_request(turn),
            "failure_code": turn.failure_code,
            "result": result.result if result else None,
        }


@router.get("/sessions/{session_id}/turns")
async def get_turns(session_id: UUID, token: Annotated[str, Depends(require_token)]):
    async with session_factory() as db:
        await owned_session(db, session_id, token)
        rows = (await db.execute(select(CommittedTurn).where(
            CommittedTurn.session_id == session_id
        ).order_by(CommittedTurn.turn_number))).scalars().all()
        return {"turns": [row.result for row in rows]}
