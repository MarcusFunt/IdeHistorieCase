"""Persistent service status and M2 game state."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    inference_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    message: Mapped[str] = mapped_column(String(128), nullable=False, default="starting")


class ScenarioVersion(Base):
    __tablename__ = "scenario_versions"
    __table_args__ = (
        UniqueConstraint("slug", "version"),
        Index("uq_scenario_versions_active_slug", "slug", unique=True, postgresql_where=text("is_active")),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int | None] = mapped_column(Integer)
    definition: Mapped[dict | None] = mapped_column(JSONB)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    bundle_hash: Mapped[str | None] = mapped_column(String(64))
    behavior_rules: Mapped[dict | None] = mapped_column(JSONB)
    evidence_policy: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GameSession(Base):
    __tablename__ = "game_sessions"
    __table_args__ = (CheckConstraint("turn_number >= 0"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scenario_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scenario_versions.id"), nullable=False
    )
    capability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    player_role_id: Mapped[str | None] = mapped_column(String(64))
    capability_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TurnRequest(Base):
    __tablename__ = "turn_requests"
    __table_args__ = (
        UniqueConstraint("session_id", "client_turn_id"),
        CheckConstraint("expected_turn >= 0"),
        CheckConstraint("attempts >= 0"),
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"),
        Index(
            "uq_turn_requests_active_session",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_turn_requests_claim", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=False
    )
    client_turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    expected_turn: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    input_language: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommittedTurn(Base):
    __tablename__ = "committed_turns"
    __table_args__ = (UniqueConstraint("session_id", "turn_number"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("game_sessions.id"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("turn_requests.id"), unique=True, nullable=False
    )
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (
        CheckConstraint("event_index >= 0"),
        UniqueConstraint("committed_turn_id", "event_index"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    committed_turn_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("committed_turns.id"), nullable=False
    )
    event_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
