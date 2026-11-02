"""Historical passage eligibility stays fail-closed across dates, actors and review."""
import hashlib
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from app.rag.policy import EvidenceContext, EvidencePassage, PassageApproval, is_eligible

ROOT = Path(__file__).parents[2]
POLICY_PATH = ROOT / "scenarios" / "versailles-1919" / "2.5_evidence.yaml"
VERSION_ID = UUID("4b640c11-ec24-4c98-9d50-3f69f15102f8")


def source_policy():
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def passage(**updates):
    values = {
        "id": UUID("9a41698d-7c18-48dd-b3c9-1a59d1c27583"),
        "scenario_version_id": VERSION_ID,
        "scenario_slug": "versailles-1919",
        "scenario_version": "2.5",
        "passage_key": "fourteen-points",
        "source_identifier": "R04",
        "source_title": "Wilson address",
        "source_url": "https://history.state.gov/historicaldocuments/sample",
        "source_anchor": "Document 4, page 1",
        "text": "Open covenants of peace, openly arrived at.",
        "passage_sha256": hashlib.sha256(
            b"Open covenants of peace, openly arrived at."
        ).hexdigest(),
        "document_date": date(1918, 1, 8),
        "in_world_available_from": date(1918, 1, 8),
        "postgame_only": False,
        "actor_ids": ("britain", "china"),
        "visibility_scope": "actor_limited",
    }
    values.update(updates)
    if "text" in updates and "passage_sha256" not in updates:
        values["passage_sha256"] = hashlib.sha256(values["text"].encode("utf-8")).hexdigest()
    return EvidencePassage(**values)


def context(**updates):
    values = {
        "scenario_version_id": VERSION_ID,
        "scenario_slug": "versailles-1919",
        "scenario_version": "2.5",
        "actor_id": "britain",
        "world_date": date(1919, 4, 22),
        "mode": "gameplay",
    }
    values.update(updates)
    return EvidenceContext(**values)


APPROVED = PassageApproval("approved", "approved", 1)


def test_approved_in_date_and_actor_passage_is_eligible():
    assert is_eligible(passage(), context(), APPROVED, source_policy()) is True


@pytest.mark.parametrize(
    "passage_updates,context_updates,decision",
    [
        ({"document_date": date(1919, 4, 23)}, {}, APPROVED),
        ({"in_world_available_from": date(1919, 4, 23)}, {}, APPROVED),
        ({"actor_ids": ("china",)}, {}, APPROVED),
        ({"scenario_version": "2.4"}, {}, APPROVED),
        ({}, {"scenario_version_id": UUID("d1e66ac3-751b-4fab-a61b-e9f95ab6c765")}, APPROVED),
        ({}, {}, PassageApproval("pending", "approved", 1)),
        ({}, {}, PassageApproval("approved", "pending", 1)),
        ({"source_identifier": "R13"}, {}, APPROVED),
    ],
)
def test_temporal_actor_provenance_and_review_failures_are_ineligible(
    passage_updates, context_updates, decision
):
    assert is_eligible(
        passage(**passage_updates), context(**context_updates), decision, source_policy()
    ) is False


def test_passage_hash_must_match_the_immutable_text():
    assert is_eligible(
        passage(passage_sha256="0" * 64), context(), APPROVED, source_policy()
    ) is False


def test_confidential_council_passage_remains_actor_limited_even_in_evaluator_mode():
    confidential = passage(
        source_identifier="R11",
        source_title="Council of Four minutes",
        source_anchor="Document 22, meeting minutes",
        actor_ids=("britain", "france", "united_states"),
        visibility_scope="confidential",
    )
    chinese = context(actor_id="china", world_date=date(1919, 4, 25), mode="evaluator")

    assert is_eligible(confidential, chinese, APPROVED, source_policy()) is False


def test_postgame_source_can_be_seen_without_an_in_world_actor_or_date():
    later = passage(
        source_identifier="R03",
        source_title="Reparation Commission schedule",
        source_anchor="5 May 1921 schedule",
        text="The later schedule fixed the total at 132 billion gold marks.",
        document_date=date(1921, 5, 5),
        in_world_available_from=None,
        postgame_only=True,
        actor_ids=(),
        visibility_scope="postgame",
    )

    assert is_eligible(
        later,
        context(actor_id=None, world_date=date(1919, 4, 22), mode="postgame"),
        APPROVED,
        source_policy(),
    ) is True
