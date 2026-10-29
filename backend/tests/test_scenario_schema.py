"""Behavioral checks for authored scenario validation."""
import importlib.util
from pathlib import Path

import pytest


def loader_module():
    try:
        spec = importlib.util.find_spec("app.scenarios.loader")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "authored scenario loader is not implemented"
    from app.scenarios.loader import load_scenario

    return load_scenario


MINIMAL_SCENARIO = """\
schema_version: 1
slug: test-scenario
version: "1"
metadata:
  title: "Test scenario"
  summary: "A minimal independently authored scenario."
  default_language: da
  supported_languages: [da]
  historical_period: {start_year: 1919, end_year: 1919}
  subject: "History"
  tags: [test]
  intended_audience: "HTX"
historical_context:
  introduction: "A test setting."
  counterfactual_disclaimer: "The outcome may differ from history."
actors:
  - {id: delegation, name: Delegation, description: "A participant."}
roles:
  - {id: representative, actor_id: delegation, title: Representative, playable: true, introduction: "You negotiate."}
default_role_id: representative
learning_objectives:
  - id: reasoning
    description: "Explain a choice."
    concepts: [evidence]
    observable_behaviors: ["Give one reason."]
state_variables:
  - {id: support, kind: integer_metric, initial: 50, min_value: 0, max_value: 100, description: "Simulated support."}
  - {id: agreement_reached, kind: boolean_flag, initial: false, description: "Whether a deal was reached."}
phases:
  - {id: opening, title: Opening, is_start: true, is_terminal: false, allowed_next_phase_ids: [reflection]}
  - {id: reflection, title: Reflection, is_start: false, is_terminal: true, allowed_next_phase_ids: []}
dilemmas:
  - {id: first_choice, phase_id: opening, prompt: "What should be negotiated?", relevant_actor_ids: [delegation], learning_objective_ids: [reasoning]}
allowed_events:
  - {type: adjust_metric, target_id: support, min_delta: -10, max_delta: 10}
  - {type: set_flag, target_id: agreement_reached}
  - {type: advance_phase}
source_refs: []
"""


def test_valid_scenario_derives_compatibility_state_and_digest(tmp_path: Path):
    path = tmp_path / "valid.yaml"
    path.write_text(MINIMAL_SCENARIO, encoding="utf-8")

    loaded = loader_module()(path)

    assert loaded.initial_state == {
        "phase": "opening",
        "metrics": {"support": 50},
        "flags": {"agreement_reached": False},
        "decisions": {},
        "skipped_issues": [],
        "visited_phases": ["opening"],
    }
    assert loaded.definition.default_role_id == "representative"
    assert len(loaded.content_hash) == 64


def test_canonical_digest_ignores_yaml_whitespace_and_mapping_order(tmp_path: Path):
    load_scenario = loader_module()
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(MINIMAL_SCENARIO, encoding="utf-8")
    reordered = MINIMAL_SCENARIO.replace(
        'schema_version: 1\nslug: test-scenario\nversion: "1"',
        'version: "1"\nslug: test-scenario\nschema_version: 1',
    ).replace(
        '  title: "Test scenario"\n  summary:',
        '  summary:',
    ).replace(
        '  summary: "A minimal independently authored scenario."\n',
        '  summary: "A minimal independently authored scenario."\n  title: "Test scenario"\n',
    ).replace(": ", ":   ")
    second.write_text(reordered, encoding="utf-8")

    assert load_scenario(first).content_hash == load_scenario(second).content_hash


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("id: reflection, title: Reflection", "id: opening, title: Reflection", "duplicate"),
        ("phase_id: opening", "phase_id: missing_phase", "phase"),
        ("default_role_id: representative", "default_role_id: missing_role", "role"),
        ("min_value: 0, max_value: 100", "min_value: 60, max_value: 40", "range"),
        ("target_id: support, min_delta", "target_id: agreement_reached, min_delta", "metric"),
    ],
)
def test_cross_reference_and_state_errors_are_rejected(
    tmp_path: Path, old: str, new: str, message: str
):
    path = tmp_path / "invalid.yaml"
    path.write_text(MINIMAL_SCENARIO.replace(old, new), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        loader_module()(path)


def test_unreachable_phase_is_rejected(tmp_path: Path):
    path = tmp_path / "unreachable.yaml"
    path.write_text(
        MINIMAL_SCENARIO.replace(
            "  - {id: reflection, title: Reflection, is_start: false, is_terminal: true, allowed_next_phase_ids: []}",
            "  - {id: reflection, title: Reflection, is_start: false, is_terminal: true, allowed_next_phase_ids: []}\n"
            "  - {id: hidden, title: Hidden, is_start: false, is_terminal: true, allowed_next_phase_ids: []}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unreachable"):
        loader_module()(path)


def test_phase_without_terminal_route_is_rejected(tmp_path: Path):
    path = tmp_path / "dead-end.yaml"
    path.write_text(
        MINIMAL_SCENARIO.replace(
            "allowed_next_phase_ids: [reflection]",
            "allowed_next_phase_ids: [detour]",
        ).replace(
            "  - {id: reflection, title: Reflection, is_start: false, is_terminal: true, allowed_next_phase_ids: []}",
            "  - {id: detour, title: Detour, is_start: false, is_terminal: false, allowed_next_phase_ids: [detour]}\n"
            "  - {id: reflection, title: Reflection, is_start: false, is_terminal: true, allowed_next_phase_ids: []}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="terminal"):
        loader_module()(path)


def test_unsupported_schema_version_is_rejected(tmp_path: Path):
    path = tmp_path / "unsupported.yaml"
    path.write_text(MINIMAL_SCENARIO.replace("schema_version: 1", "schema_version: 2"), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        loader_module()(path)
