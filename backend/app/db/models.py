"""Persistent service status and M2 game state."""
from datetime import date, datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
        CheckConstraint(
            "schema_version IS DISTINCT FROM 2 OR "
            "(jsonb_typeof(definition->'content_items') = 'array' "
            "AND jsonb_array_length(definition->'content_items') > 0)",
            name="ck_scenario_v2_manifest_nonempty_content_items",
        ),
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


class ScenarioContentItem(Base):
    __tablename__ = "scenario_content_items"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "item_key"),
        CheckConstraint("length(body) BETWEEN 1 AND 1048576"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("authoritative_scope = 'author_context'"),
        CheckConstraint("mode IN ('gameplay', 'postgame')"),
        CheckConstraint("review_status IN ('draft', 'author_reviewed')"),
        CheckConstraint("jsonb_typeof(visibility) = 'array'"),
        CheckConstraint("jsonb_typeof(source_refs) = 'array'"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'"),
        CheckConstraint(
            "world_valid_from IS NULL OR world_valid_to IS NULL "
            "OR world_valid_from <= world_valid_to"
        ),
        Index(
            "ix_scenario_content_items_runtime_filter",
            "scenario_version_id",
            "mode",
            "review_status",
        ),
        Index("ix_scenario_content_items_visibility", "visibility", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scenario_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scenario_versions.id", ondelete="RESTRICT"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    authoritative_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    availability_date: Mapped[date | None] = mapped_column(Date)
    world_valid_from: Mapped[date | None] = mapped_column(Date)
    world_valid_to: Mapped[date | None] = mapped_column(Date)
    visibility: Mapped[list] = mapped_column(JSONB, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
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


class RagScenarioPolicy(Base):
    __tablename__ = "rag_scenario_policies"
    __table_args__ = (
        UniqueConstraint("scenario_version_id"),
        CheckConstraint("corpus_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("jsonb_typeof(phase_world_dates) = 'object'"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scenario_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scenario_versions.id", ondelete="RESTRICT"), nullable=False
    )
    scenario_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_version: Mapped[str] = mapped_column(String(32), nullable=False)
    phase_world_dates: Mapped[dict] = mapped_column(JSONB, nullable=False)
    corpus_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HistoricalPassage(Base):
    __tablename__ = "historical_passages"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "passage_key"),
        CheckConstraint("length(passage_text) BETWEEN 1 AND 4000"),
        CheckConstraint("passage_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("corpus_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("jsonb_typeof(actor_ids) = 'array'"),
        CheckConstraint(
            "(postgame_only AND in_world_available_from IS NULL) OR "
            "(NOT postgame_only AND in_world_available_from IS NOT NULL)"
        ),
        CheckConstraint(
            "visibility_scope IN ('public', 'actor_limited', 'confidential', 'postgame')"
        ),
        Index("ix_historical_passages_scenario_version", "scenario_version_id", "source_identifier"),
        Index("ix_historical_passages_actor_ids", "actor_ids", postgresql_using="gin"),
        Index(
            "historical_passages_search_idx",
            text("to_tsvector('simple'::regconfig, passage_text)"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scenario_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scenario_versions.id", ondelete="RESTRICT"), nullable=False
    )
    scenario_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_version: Mapped[str] = mapped_column(String(32), nullable=False)
    passage_key: Mapped[str] = mapped_column(String(96), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(32), nullable=False)
    source_title: Mapped[str] = mapped_column(String(240), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_anchor: Mapped[str] = mapped_column(String(240), nullable=False)
    rights_basis: Mapped[str] = mapped_column(Text, nullable=False)
    passage_text: Mapped[str] = mapped_column(Text, nullable=False)
    passage_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    in_world_available_from: Mapped[date | None] = mapped_column(Date)
    postgame_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    actor_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    visibility_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    corpus_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HistoricalPassageReview(Base):
    __tablename__ = "historical_passage_reviews"
    __table_args__ = (
        CheckConstraint("review_status IN ('approved', 'rejected', 'revoked')"),
        CheckConstraint("rights_status IN ('approved', 'pending', 'denied', 'restricted')"),
        CheckConstraint("revision >= 1"),
        UniqueConstraint("passage_id", "revision"),
        Index("ix_historical_passage_reviews_latest", "passage_id", "revision"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    passage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("historical_passages.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    rights_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HistoricalPassageEmbedding(Base):
    __tablename__ = "historical_passage_embeddings"
    __table_args__ = (
        UniqueConstraint("passage_id", "passage_sha256", "model_id"),
        CheckConstraint("passage_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("model_id <> ''"),
        Index(
            "historical_embeddings_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    passage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("historical_passages.id", ondelete="RESTRICT"), nullable=False
    )
    passage_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
