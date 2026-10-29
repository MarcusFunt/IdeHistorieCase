"""Structural and temporal safeguards for the reviewed Versailles research package."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[4] / "scenarios" / "versailles-1919" / "sources"


def read_yaml(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_catalog_has_complete_document_provenance_and_rights_fields():
    sources = read_yaml("catalog.yaml")["sources"]

    expected_ids = {f"R{number:02}" for number in range(1, 25)} | {"R11A"}
    assert len(sources) == 25
    assert {item["id"] for item in sources} == expected_ids
    assert all(item["url"].startswith("https://") for item in sources)
    assert all(item.get("rights_status") and item.get("reviewer") for item in sources)


def test_claim_ledger_has_eighteen_scoped_claims_with_valid_source_ids():
    catalog_ids = {item["id"] for item in read_yaml("catalog.yaml")["sources"]}
    claims = read_yaml("claims.yaml")["claims"]

    assert len(claims) == 18
    assert {item["claim_id"] for item in claims} == {
        f"H-{number:03}" for number in range(1, 19)
    }
    assert all(set(item["source_refs"]) <= catalog_ids for item in claims)
    assert all(item["statement"] and item["claim_type"] for item in claims)


def test_later_facts_and_german_objections_are_temporally_gated():
    claims = {item["claim_id"]: item for item in read_yaml("claims.yaml")["claims"]}

    assert claims["H-004"]["earliest_use"] == "postgame"
    assert "1921" in claims["H-004"]["statement"]
    assert claims["H-001"]["earliest_use"] == "1919-05-07"
    assert claims["H-002"]["earliest_use"] == "1919-05-07"
    assert claims["H-012"]["earliest_use"] == "1919-04-25"
    assert claims["H-014"]["earliest_use"] == "1919-05-29"
    assert claims["H-017"]["earliest_use"] == "1919-06-28"
    assert claims["H-018"]["earliest_use"] == "1919-06-28"


def test_history_goldens_cover_required_hard_cases_with_known_sources():
    catalog_ids = {item["id"] for item in read_yaml("catalog.yaml")["sources"]}
    cases = read_yaml("history_goldens.yaml")["cases"]

    assert len(cases) >= 12
    assert {item["test_id"] for item in cases} >= {f"V25-H{number:02}" for number in range(1, 13)}
    assert all(item["required_response"] for item in cases)
    assert all(set(item["source_refs"]) <= catalog_ids for item in cases)


def test_versailles_manifest_has_ten_phases_and_twelve_dilemmas():
    manifest = yaml.safe_load(
        (ROOT.parent / "2.5.yaml").read_text(encoding="utf-8")
    )

    assert manifest["version"] == "2.5"
    assert len(manifest["phases"]) == 10
    assert len(manifest["dilemmas"]) >= 12
    assert manifest["default_role_id"] == "british_adviser"


def test_evidence_policy_blocks_future_and_role_ineligible_records():
    policy = yaml.safe_load(
        (ROOT.parent / "2.5_evidence.yaml").read_text(encoding="utf-8")
    )
    sources = {item["source_id"]: item for item in policy["source_access"]}

    assert policy["citation_ready"] is False
    assert sources["R07"]["in_world_available_from"] == "1919-05-24"
    assert sources["R08"]["in_world_available_from"] == "1919-06-28"
    assert sources["R09"]["in_world_available_from"] == "1919-05-29"
    assert "china" not in sources["R07"]["actor_ids"]
    assert "china" not in sources["R11"]["actor_ids"]
    assert sources["R11A"]["actor_ids"] == ["china"]
    assert sources["R03"]["in_world_available_from"] == "postgame"
    assert sources["R01"]["in_world_available_from"] == "1919-06-28"
    assert sources["R04"]["in_world_available_from"] == "1918-01-08"
    assert all(item["citation_ready"] is False for item in sources.values())


def test_behavior_sidecar_uses_known_refs_and_does_not_claim_calibrated_effects():
    catalog_ids = {item["id"] for item in read_yaml("catalog.yaml")["sources"]}
    rules = yaml.safe_load(
        (ROOT.parent / "2.5_behaviors.yaml").read_text(encoding="utf-8")
    )["rules"]

    assert len(rules) >= 10
    behavior = yaml.safe_load((ROOT.parent / "2.5_behaviors.yaml").read_text(encoding="utf-8"))
    assert behavior["reaction_policy"] == "index_pairing_is_illustrative_not_historical"
    assert all(set(item["evidence_refs"]) <= catalog_ids for item in rules)
    assert any("qualitative" in str(item.get("response", "")).lower() for item in rules)



def test_bundle_hash_file_pins_all_authored_versailles_sidecars():
    from app.scenarios.loader import load_scenario

    manifest = ROOT.parent / "2.5.yaml"
    expected = (ROOT.parent / "2.5_bundle.sha256").read_text(encoding="ascii").strip()

    assert load_scenario(manifest).bundle_hash == expected

def test_racial_equality_claim_has_a_page_level_frus_anchor():
    catalog = read_yaml("catalog.yaml")["sources"]
    r10 = next(item for item in catalog if item["id"] == "R10")
    claims = read_yaml("claims.yaml")["claims"]
    h013 = next(item for item in claims if item["claim_id"] == "H-013")

    assert "289-291" in r10["text_anchor"]
    assert "Makino" in h013["statement"]
    assert "not approved" in h013["statement"]
