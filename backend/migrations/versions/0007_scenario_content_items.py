"""Store versioned scenario author content for SQL-backed runtime use."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_scenario_content_items"
down_revision = "0006_history_authentic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenario_content_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "scenario_version_id",
            sa.UUID(),
            sa.ForeignKey("scenario_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("item_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("authoritative_scope", sa.String(32), nullable=False),
        sa.Column("availability_date", sa.Date(), nullable=True),
        sa.Column("world_valid_from", sa.Date(), nullable=True),
        sa.Column("world_valid_to", sa.Date(), nullable=True),
        sa.Column("visibility", postgresql.JSONB(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("review_status", sa.String(24), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "scenario_version_id", "item_key", name="uq_scenario_content_version_item_key"
        ),
        sa.CheckConstraint("length(body) BETWEEN 1 AND 1048576", name="ck_scenario_content_body_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_scenario_content_sha256"),
        sa.CheckConstraint(
            "authoritative_scope = 'author_context'", name="ck_scenario_content_scope"
        ),
        sa.CheckConstraint("mode IN ('gameplay', 'postgame')", name="ck_scenario_content_mode"),
        sa.CheckConstraint(
            "review_status IN ('draft', 'author_reviewed')", name="ck_scenario_content_review_status"
        ),
        sa.CheckConstraint("jsonb_typeof(visibility) = 'array'", name="ck_scenario_content_visibility_array"),
        sa.CheckConstraint("jsonb_typeof(source_refs) = 'array'", name="ck_scenario_content_sources_array"),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_scenario_content_metadata_object"),
        sa.CheckConstraint(
            "world_valid_from IS NULL OR world_valid_to IS NULL "
            "OR world_valid_from <= world_valid_to",
            name="ck_scenario_content_world_date_range",
        ),
    )
    op.create_index(
        "ix_scenario_content_items_runtime_filter",
        "scenario_content_items",
        ["scenario_version_id", "mode", "review_status"],
    )
    op.create_index(
        "ix_scenario_content_items_visibility",
        "scenario_content_items",
        ["visibility"],
        postgresql_using="gin",
    )
    op.execute(
        """
        CREATE FUNCTION guard_scenario_content_item_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            version_definition jsonb;
            expected_count integer;
            actual_count integer;
        BEGIN
            IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'published scenario content items are immutable';
            END IF;

            SELECT definition INTO version_definition
            FROM scenario_versions
            WHERE id = NEW.scenario_version_id AND schema_version = 2;
            IF NOT FOUND OR jsonb_typeof(version_definition->'content_items') <> 'array' THEN
                RAISE EXCEPTION 'scenario content items require a schema-v2 scenario version';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(version_definition->'content_items') AS item
                WHERE item->>'key' = NEW.item_key
            ) THEN
                RAISE EXCEPTION 'scenario content item key is absent from its immutable manifest';
            END IF;
            expected_count := jsonb_array_length(version_definition->'content_items');
            SELECT count(*) INTO actual_count
            FROM scenario_content_items
            WHERE scenario_version_id = NEW.scenario_version_id;
            IF actual_count >= expected_count THEN
                RAISE EXCEPTION 'scenario content item set is already complete';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scenario_content_item_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON scenario_content_items
        FOR EACH ROW EXECUTE FUNCTION guard_scenario_content_item_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION verify_v2_content_item_set() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            expected_count integer;
            actual_count integer;
        BEGIN
            IF NEW.schema_version = 2 THEN
                IF NEW.definition IS NULL OR jsonb_typeof(NEW.definition->'content_items') <> 'array' THEN
                    RAISE EXCEPTION 'schema-v2 scenario manifest must declare content_items';
                END IF;
                expected_count := jsonb_array_length(NEW.definition->'content_items');
                SELECT count(*) INTO actual_count
                FROM scenario_content_items
                WHERE scenario_version_id = NEW.id;
                IF actual_count <> expected_count THEN
                    RAISE EXCEPTION 'schema-v2 scenario content item set is incomplete';
                END IF;
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_scenario_version_content_complete
        AFTER INSERT ON scenario_versions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION verify_v2_content_item_set()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scenario_version_content_complete ON scenario_versions")
    op.execute("DROP FUNCTION IF EXISTS verify_v2_content_item_set()")
    op.execute("DROP TRIGGER IF EXISTS trg_scenario_content_item_immutable ON scenario_content_items")
    op.execute("DROP FUNCTION IF EXISTS guard_scenario_content_item_mutation()")
    op.drop_index("ix_scenario_content_items_visibility", table_name="scenario_content_items")
    op.drop_index("ix_scenario_content_items_runtime_filter", table_name="scenario_content_items")
    op.drop_table("scenario_content_items")
