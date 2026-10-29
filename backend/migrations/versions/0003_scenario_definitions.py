"""Add immutable authored scenario versions and role selection."""
import warnings

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_scenario_definitions"
down_revision = "0002_durable_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scenario_versions", sa.Column("schema_version", sa.Integer(), nullable=True))
    op.add_column(
        "scenario_versions",
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("scenario_versions", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("scenario_versions", sa.Column("bundle_hash", sa.String(64), nullable=True))
    op.add_column("scenario_versions", sa.Column("behavior_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("scenario_versions", sa.Column("evidence_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column(
        "scenario_versions",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "scenario_versions",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("game_sessions", sa.Column("player_role_id", sa.String(64), nullable=True))

    op.execute(
        sa.text(
            "UPDATE scenario_versions SET is_active = true "
            "WHERE slug = 'versailles-1919' AND version = '1'"
        )
    )
    op.create_check_constraint(
        "ck_scenario_definition_complete",
        "scenario_versions",
        "((schema_version IS NULL AND definition IS NULL AND content_hash IS NULL) "
        "OR (schema_version IS NOT NULL AND definition IS NOT NULL AND content_hash IS NOT NULL))",
    )
    op.create_index(
        "uq_scenario_versions_active_slug",
        "scenario_versions",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_scenario_content_mutation() RETURNS trigger AS $$
        BEGIN
            IF (OLD.definition IS NOT NULL OR NEW.definition IS NOT NULL)
               AND (
                    NEW.slug IS DISTINCT FROM OLD.slug
                    OR NEW.version IS DISTINCT FROM OLD.version
                    OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
                    OR NEW.definition IS DISTINCT FROM OLD.definition
                    OR NEW.initial_state IS DISTINCT FROM OLD.initial_state
                    OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                    OR NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
                    OR NEW.behavior_rules IS DISTINCT FROM OLD.behavior_rules
                    OR NEW.evidence_policy IS DISTINCT FROM OLD.evidence_policy
               )
            THEN
                RAISE EXCEPTION
                    'authored scenario content is immutable; increment the scenario version';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scenario_content_immutable
        BEFORE UPDATE ON scenario_versions
        FOR EACH ROW EXECUTE FUNCTION reject_scenario_content_mutation()
        """
    )


def downgrade() -> None:
    warnings.warn(
        "Downgrading 0003 discards authored scenario definitions and role assignments; "
        "M2 sessions, turns, events, and the seeded scenario are retained.",
        RuntimeWarning,
        stacklevel=2,
    )
    op.execute("DROP TRIGGER IF EXISTS trg_scenario_content_immutable ON scenario_versions")
    op.execute("DROP FUNCTION IF EXISTS reject_scenario_content_mutation()")
    op.drop_index("uq_scenario_versions_active_slug", table_name="scenario_versions")
    op.drop_constraint("ck_scenario_definition_complete", "scenario_versions", type_="check")
    op.drop_column("game_sessions", "player_role_id")
    op.drop_column("scenario_versions", "created_at")
    op.drop_column("scenario_versions", "is_active")
    op.drop_column("scenario_versions", "evidence_policy")
    op.drop_column("scenario_versions", "behavior_rules")
    op.drop_column("scenario_versions", "bundle_hash")
    op.drop_column("scenario_versions", "content_hash")
    op.drop_column("scenario_versions", "definition")
    op.drop_column("scenario_versions", "schema_version")
