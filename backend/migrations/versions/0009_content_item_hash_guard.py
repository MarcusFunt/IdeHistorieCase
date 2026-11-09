"""Require nonempty v2 manifests and matching SQL content body hashes."""
from alembic import op

revision = "0009_content_item_hash_guard"
down_revision = "0008_scenario_content_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_check_constraint(
        "ck_scenario_v2_manifest_nonempty_content_items",
        "scenario_versions",
        "schema_version IS DISTINCT FROM 2 OR "
        "(jsonb_typeof(definition->'content_items') = 'array' "
        "AND jsonb_array_length(definition->'content_items') > 0)",
    )
    op.execute(
        """
        CREATE FUNCTION verify_scenario_content_item_hash() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.sha256 IS DISTINCT FROM encode(
                digest(convert_to(NEW.body, 'UTF8'), 'sha256'), 'hex'
            ) THEN
                RAISE EXCEPTION 'scenario content item SHA-256 does not match its body';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scenario_content_item_hash
        BEFORE INSERT ON scenario_content_items
        FOR EACH ROW EXECUTE FUNCTION verify_scenario_content_item_hash()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scenario_content_item_hash ON scenario_content_items")
    op.execute("DROP FUNCTION IF EXISTS verify_scenario_content_item_hash()")
    op.drop_constraint(
        "ck_scenario_v2_manifest_nonempty_content_items",
        "scenario_versions",
        type_="check",
    )
