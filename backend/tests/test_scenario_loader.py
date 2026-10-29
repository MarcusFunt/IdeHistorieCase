"""Safe YAML parsing failures for authored scenarios."""
import importlib.util
from pathlib import Path

import pytest
from test_scenario_schema import MINIMAL_SCENARIO


def available_loader():
    try:
        spec = importlib.util.find_spec("app.scenarios.loader")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "authored scenario loader is not implemented"
    from app.scenarios.loader import load_scenario

    return load_scenario


@pytest.mark.parametrize(
    "text",
    [
        MINIMAL_SCENARIO.replace("slug: test-scenario", "slug: test-scenario\nslug: duplicate"),
        MINIMAL_SCENARIO.replace("tags: [test]", "tags: &labels [test]\n  copied_tags: *labels"),
        "- not-a-mapping\n",
        "---\n" + MINIMAL_SCENARIO + "---\n" + MINIMAL_SCENARIO,
    ],
)
def test_unsafe_or_ambiguous_yaml_is_rejected(tmp_path: Path, text: str):
    path = tmp_path / "invalid.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        available_loader()(path)


def test_oversized_manifest_is_rejected_before_parsing(tmp_path: Path):
    path = tmp_path / "oversized.yaml"
    path.write_text(MINIMAL_SCENARIO + "#" * (1024 * 1024), encoding="utf-8")

    with pytest.raises(ValueError, match="size"):
        available_loader()(path)


def test_validate_all_finds_manifests_and_skips_source_sidecars(tmp_path: Path):
    try:
        spec = importlib.util.find_spec("app.scenarios.loader")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "authored scenario loader is not implemented"
    from app.scenarios.loader import validate_all

    (tmp_path / "versailles").mkdir()
    (tmp_path / "versailles" / "2.5.yaml").write_text(MINIMAL_SCENARIO, encoding="utf-8")
    behavior_sidecar = """\
policy_version: 1
scenario_slug: test-scenario
scenario_version: "1"
reaction_policy: index_pairing_is_illustrative_not_historical
rules: []
"""
    (tmp_path / "versailles" / "2.5_behaviors.yaml").write_text(
        behavior_sidecar, encoding="utf-8"
    )
    (tmp_path / "versailles" / "sources").mkdir()
    (tmp_path / "versailles" / "sources" / "catalog.yaml").write_text("sources: []\n", encoding="utf-8")

    results = validate_all(tmp_path)

    assert [result.path.name for result in results] == ["2.5.yaml"]
    assert results[0].content_hash




def test_bundle_hash_pins_manifest_sidecars_and_source_catalog(tmp_path: Path):
    scenario = tmp_path / "test-scenario"
    sources = scenario / "sources"
    sources.mkdir(parents=True)
    manifest = scenario / "1.yaml"
    manifest.write_text(MINIMAL_SCENARIO, encoding="utf-8")
    (scenario / "1_notes.yaml").write_text("policy_version: 1\n", encoding="utf-8")
    (sources / "catalog.yaml").write_text("sources: []\n", encoding="utf-8")

    first = available_loader()(manifest)
    same = available_loader()(manifest)
    (sources / "catalog.yaml").write_text("sources: [R01]\n", encoding="utf-8")
    changed = available_loader()(manifest)

    assert first.bundle_hash
    assert first.bundle_hash == same.bundle_hash
    assert first.bundle_hash != changed.bundle_hash


def test_standalone_manifest_has_no_bundle_hash(tmp_path: Path):
    manifest = tmp_path / "standalone.yaml"
    manifest.write_text(MINIMAL_SCENARIO, encoding="utf-8")

    assert available_loader()(manifest).bundle_hash is None




@pytest.mark.parametrize(
    ("sidecar_name", "manifest_text", "sidecar_text", "message"),
    [
        (
            "1_behaviors.yaml",
            MINIMAL_SCENARIO,
            """policy_version: 1
scenario_slug: test-scenario
scenario_version: "1"
reaction_policy: index_pairing_is_illustrative_not_historical
rules:
  - issue_id: first_choice
    phase_id: opening
    decision_codes: [[not-a-string]]
    objection_codes: [concern]
    evidence_refs: []
""",
            "decision_codes must",
        ),
        (
            "1_evidence.yaml",
            MINIMAL_SCENARIO.replace("source_refs: []", "source_refs: [R01]"),
            """policy_version: 1
scenario_slug: test-scenario
scenario_version: "1"
citation_ready: false
source_access:
  - source_id: R01
    in_world_available_from: "1919-01-01"
    scope: period_statement_paraphrase_only
    actor_ids: [[not-a-string]]
    citation_ready: false
""",
            "actor_ids must",
        ),
        (
            "1_evidence.yaml",
            MINIMAL_SCENARIO.replace("source_refs: []", "source_refs: [R01]"),
            """policy_version: 1
scenario_slug: test-scenario
scenario_version: "1"
citation_ready: false
source_access:
  - source_id: R01
    in_world_available_from: yesterday
    scope: period_statement_paraphrase_only
    actor_ids: []
    citation_ready: false
""",
            "ISO date",
        ),
        (
            "1_evidence.yaml",
            MINIMAL_SCENARIO.replace("source_refs: []", "source_refs: [R01]"),
            """policy_version: 1
scenario_slug: test-scenario
scenario_version: "1"
citation_ready: false
source_access:
  - source_id: R01
    in_world_available_from: "1919-01-01"
    scope: confidential_councl_record
    actor_ids: []
    citation_ready: false
""",
            "scope is unknown",
        ),
    ],
)
def test_sidecar_policy_malformed_values_fail_closed(
    tmp_path: Path, sidecar_name: str, manifest_text: str, sidecar_text: str, message: str
):
    scenario_dir = tmp_path / "scenario"
    scenario_dir.mkdir()
    manifest_path = scenario_dir / "1.yaml"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    (scenario_dir / sidecar_name).write_text(sidecar_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        available_loader()(manifest_path)


@pytest.mark.parametrize(
    ("add_deadlock_permission", "agreement_initial", "include_advance", "message"),
    [
        (False, False, True, "set_flag permissions"),
        (True, True, True, "initially false"),
        (True, False, False, "advance_phase permission"),
    ],
)
def test_terminal_policy_requires_usable_outcome_permissions(
    tmp_path: Path,
    add_deadlock_permission: bool,
    agreement_initial: bool,
    include_advance: bool,
    message: str,
):
    manifest_text = MINIMAL_SCENARIO.replace(
        "  - {id: agreement_reached, kind: boolean_flag, initial: false, description: \"Whether a deal was reached.\"}\n"
        "phases:",
        "  - {id: agreement_reached, kind: boolean_flag, initial: false, description: \"Whether a deal was reached.\"}\n"
        "  - {id: deadlock, kind: boolean_flag, initial: false, description: \"Whether talks end without agreement.\"}\n"
        "phases:",
    )
    if agreement_initial:
        manifest_text = manifest_text.replace(
            "id: agreement_reached, kind: boolean_flag, initial: false",
            "id: agreement_reached, kind: boolean_flag, initial: true",
        )
    if add_deadlock_permission:
        manifest_text = manifest_text.replace(
            "  - {type: set_flag, target_id: agreement_reached}\n",
            "  - {type: set_flag, target_id: agreement_reached}\n"
            "  - {type: set_flag, target_id: deadlock}\n",
        )
    if not include_advance:
        manifest_text = manifest_text.replace("  - {type: advance_phase}\n", "")
    scenario_dir = tmp_path / "scenario"
    scenario_dir.mkdir()
    manifest_path = scenario_dir / "1.yaml"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    (scenario_dir / "1_behaviors.yaml").write_text(
        """policy_version: 1
scenario_slug: test-scenario
scenario_version: "1"
reaction_policy: index_pairing_is_illustrative_not_historical
rules:
  - issue_id: first_choice
    phase_id: opening
    decision_codes: [accept, deadlock]
    objection_codes: [concern]
    evidence_refs: []
terminal_policy:
  phase_id: opening
  target_phase_id: reflection
  issue_id: first_choice
  deadlock_decision_code: deadlock
  agreement_flag_id: agreement_reached
  deadlock_flag_id: deadlock
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        available_loader()(manifest_path)
