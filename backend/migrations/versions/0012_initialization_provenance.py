"""Persist initialization repair budget, safe attempt metadata, and failure stage."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_initialization_provenance"
down_revision = "0011_session_initialization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_initialization_jobs",
        sa.Column("repair_used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "session_initialization_jobs",
        sa.Column(
            "attempt_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "session_initialization_jobs",
        sa.Column("failure_stage", sa.String(32), nullable=True),
    )
    op.create_check_constraint(
        "ck_initialization_job_attempt_metadata_array",
        "session_initialization_jobs",
        "jsonb_typeof(attempt_metadata) = 'array'",
    )


def downgrade() -> None:
    durable_provenance = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM session_initialization_jobs "
        "WHERE repair_used OR attempt_metadata <> '[]'::jsonb "
        "OR result ? 'provenance' OR failure_stage IS NOT NULL)"
    ).scalar_one()
    if durable_provenance:
        raise RuntimeError(
            "cannot drop initialization repair, provenance, or failure-stage data; "
            "back it up before downgrade"
        )
    op.drop_constraint(
        "ck_initialization_job_attempt_metadata_array",
        "session_initialization_jobs",
        type_="check",
    )
    op.drop_column("session_initialization_jobs", "failure_stage")
    op.drop_column("session_initialization_jobs", "attempt_metadata")
    op.drop_column("session_initialization_jobs", "repair_used")
