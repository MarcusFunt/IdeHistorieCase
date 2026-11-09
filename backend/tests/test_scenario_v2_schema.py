"""Strict structural and semantic checks for schema v2 manifests."""
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.scenarios.validation import validate_v2_definition

FIXTURE = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry/1.0.yaml"


def schema_v2_class():
    spec = importlib.util.find_spec("app.scenarios.schema_v2")
    assert spec is not None, "schema v2 model is not implemented"
    from app.scenarios.schema_v2 import ScenarioDefinitionV2

    return ScenarioDefinitionV2


def valid_manifest() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_investigation_fixture_has_no_mandatory_phase_or_dilemma_graph():
    model = schema_v2_class()
    raw = valid_manifest()

    definition = model.model_validate(raw)
    validate_v2_definition(definition)

    assert definition.schema_version == 2
    assert definition.interaction_guidance.style == "investigation"
    assert not hasattr(definition, "phases")
    assert not hasattr(definition, "dilemmas")


def test_v2_rejects_unknown_fields_and_non_v2_discriminators():
    model = schema_v2_class()
    raw = valid_manifest()
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="extra"):
        model.model_validate(raw)

    raw = valid_manifest()
    raw["schema_version"] = 1
    with pytest.raises(ValidationError, match="schema_version"):
        model.model_validate(raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["actors"][0].update(id="FactoryOwner"), "string_pattern_mismatch"),
        (lambda d: d["roles"][0].update(actor_id="unknown_actor"), "unknown actor"),
        (lambda d: d["completion_policy"].update(objective_ids=["unknown_goal"]), "objective"),
        (lambda d: d["content_items"][0].update(visibility=["unknown_role"]), "role"),
        (lambda d: d["content_items"][0].update(source_refs=["R99"]), "source"),
        (lambda d: d["chronology"].update(opening_date="1879-12-31"), "opening_date"),
        (lambda d: d["chronology"].update(max_days_per_advance=0), "greater than or equal to 1"),
        (lambda d: d["content_items"][0].update(world_valid_from="1889-01-01", world_valid_to="1888-12-31"), "world_valid_from"),
    ],
)
def test_v2_rejects_invalid_references_ids_and_dates(mutate, message: str):
    raw = valid_manifest()
    mutate(raw)

    model = schema_v2_class()
    try:
        definition = model.model_validate(raw)
    except ValidationError as error:
        assert message in str(error)
        return

    with pytest.raises(ValueError, match=message):
        validate_v2_definition(definition)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["metadata"]["historical_period"].update(start="1891-01-01"), "historical_period"),
        (lambda d: d["roles"].append(deepcopy(d["roles"][0])), "duplicate role"),
        (lambda d: d["content_items"].append(deepcopy(d["content_items"][0])), "duplicate content key"),
        (lambda d: d["source_refs"].append("R01"), "source_refs"),
    ],
)
def test_v2_rejects_duplicate_ids_and_invalid_periods(mutate, message: str):
    raw = valid_manifest()
    mutate(raw)

    model = schema_v2_class()
    try:
        definition = model.model_validate(raw)
    except ValidationError as error:
        assert message in str(error)
        return

    with pytest.raises(ValueError, match=message):
        validate_v2_definition(definition)
