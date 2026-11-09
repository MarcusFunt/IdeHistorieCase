"""Guard schema-v2 content against manifest drift and truncate operations."""
from alembic import op

revision = "0008_scenario_content_guards"
down_revision = "0007_scenario_content_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_scenario_content_item_manifest() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            version_definition jsonb;
            manifest_item jsonb;
            expected_count integer;
            actual_count integer;
        BEGIN
            SELECT definition INTO version_definition
            FROM scenario_versions
            WHERE id = NEW.scenario_version_id AND schema_version = 2;
            IF NOT FOUND OR jsonb_typeof(version_definition->'content_items') <> 'array' THEN
                RAISE EXCEPTION 'scenario content items require a schema-v2 scenario version';
            END IF;

            SELECT item.value INTO manifest_item
            FROM jsonb_array_elements(version_definition->'content_items') AS item(value)
            WHERE item.value->>'key' = NEW.item_key;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'scenario content item key is absent from its immutable manifest';
            END IF;

            IF (manifest_item->>'kind') IS DISTINCT FROM NEW.kind
                OR (version_definition->'metadata'->>'language') IS DISTINCT FROM NEW.language
                OR (manifest_item->>'availability_date') IS DISTINCT FROM NEW.availability_date::text
                OR (manifest_item->>'world_valid_from') IS DISTINCT FROM NEW.world_valid_from::text
                OR (manifest_item->>'world_valid_to') IS DISTINCT FROM NEW.world_valid_to::text
                OR (manifest_item->'visibility') IS DISTINCT FROM NEW.visibility
                OR (manifest_item->>'mode') IS DISTINCT FROM NEW.mode
                OR (manifest_item->>'review_status') IS DISTINCT FROM NEW.review_status
                OR (manifest_item->'source_refs') IS DISTINCT FROM NEW.source_refs
                OR (manifest_item->>'file') IS DISTINCT FROM (NEW.metadata->>'source_file')
                OR NEW.authoritative_scope IS DISTINCT FROM 'author_context'
            THEN
                RAISE EXCEPTION 'scenario content item does not match its immutable manifest';
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
        CREATE TRIGGER trg_scenario_content_item_manifest
        BEFORE INSERT ON scenario_content_items
        FOR EACH ROW EXECUTE FUNCTION guard_scenario_content_item_manifest()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_scenario_content_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'published scenario content items are immutable against TRUNCATE';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scenario_content_item_truncate
        BEFORE TRUNCATE ON scenario_content_items
        FOR EACH STATEMENT EXECUTE FUNCTION reject_scenario_content_truncate()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scenario_content_item_truncate ON scenario_content_items")
    op.execute("DROP FUNCTION IF EXISTS reject_scenario_content_truncate()")
    op.execute("DROP TRIGGER IF EXISTS trg_scenario_content_item_manifest ON scenario_content_items")
    op.execute("DROP FUNCTION IF EXISTS guard_scenario_content_item_manifest()")
