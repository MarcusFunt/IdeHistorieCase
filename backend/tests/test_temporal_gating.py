"""Evidence visibility gates run independently of any retrieval/model service."""
from datetime import date
from pathlib import Path

import yaml

from app.scenarios.evidence import available_source_ids, citation_source_ids

POLICY = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5_evidence.yaml"


def load_policy():
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def test_1919_march_and_june_views_are_temporally_monotone():
    policy = load_policy()
    march = available_source_ids(policy, date(1919, 3, 20), actor_id="britain", mode="gameplay")
    june = available_source_ids(policy, date(1919, 6, 28), actor_id="britain", mode="gameplay")

    assert march <= june
    assert "R09" not in march and "R09" in june
    assert "R07" not in march and "R07" in june
    assert "R08" not in march and "R08" in june
    assert "R01" not in date_gated_ids(policy, date(1919, 6, 27))
    assert "R01" in date_gated_ids(policy, date(1919, 6, 28))
    assert "R04" in march
    assert "R03" not in june


def date_gated_ids(policy, world_date):
    return available_source_ids(policy, world_date, actor_id="britain", mode="gameplay")


def test_confidential_council_minutes_are_not_visible_to_chinese_delegation():
    policy = load_policy()
    british = available_source_ids(policy, date(1919, 6, 28), actor_id="britain", mode="gameplay")
    chinese = available_source_ids(policy, date(1919, 6, 28), actor_id="china", mode="gameplay")

    assert "R07" in british
    assert "R07" not in chinese


def test_postgame_records_remain_separate_and_unverified_sources_cannot_be_cited():
    policy = load_policy()
    march_gameplay = available_source_ids(policy, date(1919, 3, 20), actor_id="britain", mode="gameplay")
    postgame = available_source_ids(policy, date(1919, 3, 20), actor_id=None, mode="postgame")

    assert "R03" not in march_gameplay
    assert "R03" in postgame
    assert "R09" not in march_gameplay
    assert "R09" in postgame
    assert citation_source_ids(policy, postgame) == set()



def test_malformed_policy_data_fails_closed():
    assert available_source_ids(
        {"source_access": None},
        date(1919, 6, 28),
        actor_id="britain",
        mode="gameplay",
    ) == set()
    assert available_source_ids(
        {"source_access": [{
            "source_id": "R01",
            "in_world_available_from": "1919-01-01",
            "scope": "public",
            "actor_ids": "britain",
        }]},
        date(1919, 6, 28),
        actor_id="britain",
        mode="gameplay",
    ) == set()
    assert citation_source_ids(
        {"citation_ready": True, "source_access": [{
            "source_id": [],
            "citation_ready": True,
        }]},
        {"R01"},
    ) == set()


def test_chinese_delegation_can_access_its_own_submission_but_not_council_minutes():
    policy = load_policy()
    chinese = available_source_ids(
        policy, date(1919, 4, 25), actor_id="china", mode="gameplay"
    )
    british = available_source_ids(
        policy, date(1919, 4, 25), actor_id="britain", mode="gameplay"
    )

    assert "R11A" in chinese
    assert "R11" not in chinese
    assert "R11" in british
    assert "R11A" not in british
