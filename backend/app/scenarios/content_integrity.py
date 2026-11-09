"""Shared verification for SQL-backed schema-v2 content rows."""

from hashlib import sha256

from app.db.models import ScenarioContentItem, ScenarioVersion
from app.scenarios.schema_v2 import ScenarioDefinitionV2
from app.scenarios.validation import validate_v2_definition


class ScenarioContentIntegrityError(ValueError):
    """Stored author content does not match its immutable scenario manifest."""


def verify_v2_content_rows(
    scenario: ScenarioVersion,
    rows: list[ScenarioContentItem],
) -> dict[str, ScenarioContentItem]:
    """Validate the complete SQL child set against the pinned v2 manifest."""
    if scenario.schema_version != 2 or not isinstance(scenario.definition, dict):
        raise ScenarioContentIntegrityError("SQL content integrity requires a schema-v2 release")
    try:
        definition = ScenarioDefinitionV2.model_validate(scenario.definition)
        validate_v2_definition(definition)
    except ValueError as exc:
        raise ScenarioContentIntegrityError("stored schema-v2 manifest is invalid") from exc
    if definition.slug != scenario.slug or definition.version != scenario.version:
        raise ScenarioContentIntegrityError("stored manifest identity does not match its release")

    expected = {item.key: item for item in definition.content_items}
    actual: dict[str, ScenarioContentItem] = {}
    for row in rows:
        if row.item_key in actual:
            raise ScenarioContentIntegrityError("SQL content item keys are duplicated")
        actual[row.item_key] = row
    if set(actual) != set(expected):
        raise ScenarioContentIntegrityError("SQL content item rows do not match the immutable manifest")

    for key, item in expected.items():
        row = actual[key]
        if not isinstance(row.body, str):
            raise ScenarioContentIntegrityError(f"SQL content item {key!r} has an invalid body")
        body_hash = sha256(row.body.encode("utf-8")).hexdigest()
        if not (
            row.scenario_version_id == scenario.id
            and row.item_key == item.key
            and row.kind == item.kind
            and row.language == definition.metadata.language
            and row.authoritative_scope == "author_context"
            and row.availability_date == item.availability_date
            and row.world_valid_from == item.world_valid_from
            and row.world_valid_to == item.world_valid_to
            and row.visibility == list(item.visibility)
            and row.mode == item.mode
            and row.review_status == item.review_status
            and row.source_refs == list(item.source_refs)
            and row.metadata_json == {"source_file": item.file}
            and row.sha256 == body_hash
        ):
            raise ScenarioContentIntegrityError(
                f"SQL content item {key!r} does not match the immutable manifest"
            )
    return actual
