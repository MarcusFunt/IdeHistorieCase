"""Add one explicit retry cycle and bounded turn diagnostics."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_turn_retry_diagnostics"
down_revision = "0013_turn_repair_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("turn_requests_status_check", "turn_requests", type_="check")
    op.alter_column("turn_requests", "status", type_=sa.String(24), existing_type=sa.String(16))
    op.create_check_constraint(
        "ck_turn_requests_status",
        "turn_requests",
        "status IN ('queued', 'running', 'completed', 'retryable_failure', 'failed')",
    )
    op.add_column(
        "turn_requests",
        sa.Column("client_retry_used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "turn_requests",
        sa.Column(
            "attempt_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("turn_requests", sa.Column("failure_stage", sa.String(32), nullable=True))
    op.add_column("turn_requests", sa.Column("failure_message", sa.Text(), nullable=True))
    op.drop_index("uq_turn_requests_active_session", table_name="turn_requests")
    op.create_index(
        "uq_turn_requests_active_session",
        "turn_requests",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'retryable_failure')"
        ),
    )


def downgrade() -> None:
    has_retry_data = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM turn_requests WHERE client_retry_used "
        "OR status = 'retryable_failure' OR attempt_metadata <> '[]'::jsonb "
        "OR failure_stage IS NOT NULL OR failure_message IS NOT NULL)"
    ).scalar_one()
    if has_retry_data:
        raise RuntimeError("cannot discard durable turn retry or diagnostic data")
    op.drop_index("uq_turn_requests_active_session", table_name="turn_requests")
    op.create_index(
        "uq_turn_requests_active_session",
        "turn_requests",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_constraint("ck_turn_requests_status", "turn_requests", type_="check")
    op.create_check_constraint(
        "turn_requests_status_check",
        "turn_requests",
        "status IN ('queued', 'running', 'completed', 'failed')",
    )
    op.alter_column("turn_requests", "status", type_=sa.String(16), existing_type=sa.String(24))
    op.drop_column("turn_requests", "failure_message")
    op.drop_column("turn_requests", "failure_stage")
    op.drop_column("turn_requests", "attempt_metadata")
    op.drop_column("turn_requests", "client_retry_used")
