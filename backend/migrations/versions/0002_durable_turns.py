"""Create M2 sessions, durable requests and committed turns."""
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_durable_turns"
down_revision = "0001_service_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    scenarios = op.create_table(
        "scenario_versions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("initial_state", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("slug", "version"),
    )
    op.bulk_insert(scenarios, [{
        "id": uuid.UUID("d19c3d68-d9e7-4ed8-9c29-316a4d830001"),
        "slug": "versailles-1919",
        "version": "1",
        "initial_state": {"phase": "opening"},
    }])
    op.create_table(
        "game_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("scenario_version_id", sa.UUID(), sa.ForeignKey("scenario_versions.id"), nullable=False),
        sa.Column("capability_hash", sa.String(64), nullable=False),
        sa.Column("capability_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("turn_number >= 0"),
    )
    op.create_table(
        "turn_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("game_sessions.id"), nullable=False),
        sa.Column("client_turn_id", sa.UUID(), nullable=False),
        sa.Column("expected_turn", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("input_language", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.UUID()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "client_turn_id"),
        sa.CheckConstraint("expected_turn >= 0"),
        sa.CheckConstraint("attempts >= 0"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"),
    )
    op.create_index(
        "uq_turn_requests_active_session", "turn_requests", ["session_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_turn_requests_claim", "turn_requests", ["status", "created_at"])
    op.create_table(
        "committed_turns",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("game_sessions.id"), nullable=False),
        sa.Column("request_id", sa.UUID(), sa.ForeignKey("turn_requests.id"), nullable=False, unique=True),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "turn_number"),
    )
    op.create_table(
        "game_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("committed_turn_id", sa.UUID(), sa.ForeignKey("committed_turns.id"), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("game_events")
    op.drop_table("committed_turns")
    op.drop_index("ix_turn_requests_claim", table_name="turn_requests")
    op.drop_index("uq_turn_requests_active_session", table_name="turn_requests")
    op.drop_table("turn_requests")
    op.drop_table("game_sessions")
    op.drop_table("scenario_versions")
