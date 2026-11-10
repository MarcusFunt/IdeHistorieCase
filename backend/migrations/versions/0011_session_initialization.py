"""Add durable, fenced initialization jobs for schema-v2 sessions."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_session_initialization"
down_revision = "0010_manifest_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_versions",
        sa.Column(
            "runtime_version",
            sa.String(16),
            nullable=False,
            server_default="v1_legacy",
        ),
    )
    op.execute(
        "UPDATE scenario_versions SET runtime_version = "
        "CASE WHEN schema_version = 2 THEN 'v2_ai' ELSE 'v1_legacy' END"
    )
    op.create_check_constraint(
        "ck_scenario_runtime_version",
        "scenario_versions",
        "(schema_version IS NULL AND runtime_version = 'v1_legacy') OR "
        "(schema_version = 1 AND runtime_version = 'v1_legacy') OR "
        "(schema_version = 2 AND runtime_version = 'v2_ai')",
    )
    op.add_column(
        "game_sessions",
        sa.Column(
            "runtime_version",
            sa.String(16),
            nullable=False,
            server_default="v1_legacy",
        ),
    )
    op.execute(
        "UPDATE game_sessions AS sessions SET runtime_version = versions.runtime_version "
        "FROM scenario_versions AS versions "
        "WHERE versions.id = sessions.scenario_version_id"
    )
    op.create_check_constraint(
        "ck_game_session_runtime_version",
        "game_sessions",
        "runtime_version IN ('v1_legacy', 'v2_ai')",
    )
    op.create_unique_constraint(
        "uq_game_sessions_capability_hash", "game_sessions", ["capability_hash"]
    )
    op.add_column("game_sessions", sa.Column("creation_key_hash", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_game_sessions_creation_key_hash", "game_sessions", ["creation_key_hash"]
    )

    op.create_table(
        "session_initialization_jobs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "session_id",
            sa.UUID(),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cycle_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("client_retry_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", name="uq_initialization_job_session"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'retryable_failure', 'failed')",
            name="ck_initialization_job_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_initialization_job_attempts_nonnegative"),
        sa.CheckConstraint(
            "cycle_attempts BETWEEN 0 AND 3", name="ck_initialization_job_cycle_attempts"
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) OR "
            "(status <> 'running' AND lease_token IS NULL AND leased_until IS NULL)",
            name="ck_initialization_job_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'ready' AND result IS NOT NULL AND failure_code IS NULL) OR "
            "(status <> 'ready' AND result IS NULL)",
            name="ck_initialization_job_result_state",
        ),
        sa.CheckConstraint(
            "(status IN ('retryable_failure', 'failed') AND failure_code IS NOT NULL) OR "
            "(status NOT IN ('retryable_failure', 'failed') AND failure_code IS NULL)",
            name="ck_initialization_job_failure_state",
        ),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name="ck_initialization_job_result_object",
        ),
    )
    op.create_index(
        "ix_initialization_jobs_claim",
        "session_initialization_jobs",
        ["status", "next_attempt_at", "created_at"],
    )

    # Existing v2 sessions have no validated opening. Queue turn-zero sessions;
    # preserve progressed sessions as terminally failed instead of inventing one.
    op.execute(
        """
        INSERT INTO session_initialization_jobs
            (id, session_id, status, attempts, cycle_attempts, client_retry_used, failure_code)
        SELECT
            gen_random_uuid(),
            game_sessions.id,
            CASE WHEN game_sessions.turn_number = 0 THEN 'queued' ELSE 'failed' END,
            0,
            0,
            false,
            CASE WHEN game_sessions.turn_number = 0
                 THEN NULL ELSE 'LEGACY_SESSION_REQUIRES_INITIALIZATION' END
        FROM game_sessions
        JOIN scenario_versions
          ON scenario_versions.id = game_sessions.scenario_version_id
        WHERE scenario_versions.schema_version = 2
        """
    )

    op.execute(
        """
        CREATE FUNCTION guard_ready_session_initialization() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'ready' THEN
                RAISE EXCEPTION 'committed session initialization is immutable';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_ready_session_initialization_immutable "
        "BEFORE UPDATE ON session_initialization_jobs "
        "FOR EACH ROW EXECUTE FUNCTION guard_ready_session_initialization()"
    )


def downgrade() -> None:
    durable_initialization = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM session_initialization_jobs) OR EXISTS "
        "(SELECT 1 FROM game_sessions WHERE creation_key_hash IS NOT NULL)"
    ).scalar_one()
    if durable_initialization:
        raise RuntimeError(
            "cannot drop durable session initialization data; back up and remove it before downgrade"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_ready_session_initialization_immutable ON session_initialization_jobs")
    op.execute("DROP FUNCTION IF EXISTS guard_ready_session_initialization()")
    op.drop_index("ix_initialization_jobs_claim", table_name="session_initialization_jobs")
    op.drop_table("session_initialization_jobs")
    op.drop_constraint(
        "uq_game_sessions_creation_key_hash", "game_sessions", type_="unique"
    )
    op.drop_column("game_sessions", "creation_key_hash")
    op.drop_constraint("uq_game_sessions_capability_hash", "game_sessions", type_="unique")
    op.drop_constraint("ck_game_session_runtime_version", "game_sessions", type_="check")
    op.drop_column("game_sessions", "runtime_version")
    op.drop_constraint("ck_scenario_runtime_version", "scenario_versions", type_="check")
    op.drop_column("scenario_versions", "runtime_version")
