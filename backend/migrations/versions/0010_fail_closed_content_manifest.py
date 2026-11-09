"""Fail closed on missing v2 content manifests and protect content on downgrade."""
from alembic import op

revision = "0010_manifest_guard"
down_revision = "0009_content_item_hash_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_scenario_v2_manifest_nonempty_content_items",
        "scenario_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scenario_v2_manifest_nonempty_content_items",
        "scenario_versions",
        "CASE "
        "WHEN schema_version IS DISTINCT FROM 2 THEN true "
        "WHEN definition IS NULL THEN false "
        "WHEN NOT (definition ? 'content_items') THEN false "
        "WHEN jsonb_typeof(definition->'content_items') IS DISTINCT FROM 'array' THEN false "
        "ELSE jsonb_array_length(definition->'content_items') > 0 END",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION verify_v2_content_item_set() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            expected_count integer;
            actual_count integer;
        BEGIN
            IF NEW.schema_version = 2 THEN
                IF NEW.definition IS NULL
                   OR jsonb_typeof(NEW.definition->'content_items') IS DISTINCT FROM 'array' THEN
                    RAISE EXCEPTION 'schema-v2 scenario manifest must declare content_items';
                END IF;
                expected_count := jsonb_array_length(NEW.definition->'content_items');
                IF expected_count < 1 THEN
                    RAISE EXCEPTION 'schema-v2 scenario content item set must not be empty';
                END IF;
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


def downgrade() -> None:
    op.drop_constraint(
        "ck_scenario_v2_manifest_nonempty_content_items",
        "scenario_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scenario_v2_manifest_nonempty_content_items",
        "scenario_versions",
        "schema_version IS DISTINCT FROM 2 OR "
        "(jsonb_typeof(definition->'content_items') = 'array' "
        "AND jsonb_array_length(definition->'content_items') > 0)",
    )
