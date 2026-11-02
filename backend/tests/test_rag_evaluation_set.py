"""The checked-in retrieval evaluation labels exercise source policy gates."""
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from app.rag.corpus import load_corpus
from app.rag.evaluation import load_evaluation_set
from app.rag.policy import EvidenceContext, EvidencePassage, PassageApproval, is_eligible

ROOT = Path(__file__).parents[2]
CORPUS = load_corpus(ROOT / "scenarios" / "versailles-1919" / "rag" / "corpus.yaml")
EVALUATION = load_evaluation_set(
    ROOT / "backend" / "evaluation_data" / "versailles-1919" / "rag_cases.yaml"
)
SOURCE_POLICY = yaml.safe_load(
    (ROOT / "scenarios" / "versailles-1919" / "2.5_evidence.yaml").read_text(encoding="utf-8")
)


def convert(passage_data):
    return EvidencePassage(
        id=uuid4(),
        scenario_version_id=UUID("5bc6c117-a22e-44b6-bf87-9a282e9fe679"),
        scenario_slug=CORPUS.corpus.scenario_slug,
        scenario_version=CORPUS.corpus.scenario_version,
        passage_key=passage_data.key,
        source_identifier=passage_data.source_identifier,
        source_title=passage_data.source_title,
        source_url=passage_data.source_url,
        source_anchor=passage_data.source_anchor,
        text=passage_data.passage_text,
        passage_sha256=passage_data.passage_sha256,
        document_date=passage_data.document_date,
        in_world_available_from=passage_data.in_world_available_from,
        postgame_only=passage_data.postgame_only,
        actor_ids=tuple(passage_data.actor_ids),
        visibility_scope=passage_data.visibility_scope,
    )


def test_evaluation_manifest_is_bounded_labeled_and_references_corpus_passages():
    keys = {passage.key for passage in CORPUS.corpus.passages}
    assert EVALUATION.scenario_slug == CORPUS.corpus.scenario_slug
    assert EVALUATION.scenario_version == CORPUS.corpus.scenario_version
    assert len(EVALUATION.cases) <= 32
    assert {label for case in EVALUATION.cases for label in case.labels} >= {
        "answerable",
        "date_leak",
        "confidentiality",
        "rights",
        "unsupported_claim",
    }
    for case in EVALUATION.cases:
        assert set(case.expected_passage_keys + case.blocked_passage_keys) <= keys


@pytest.mark.parametrize("case_id", ["fourteen_points_answerable", "chinese_submission_answerable"])
def test_labeled_answerable_cases_have_eligible_approved_fixture_passages(case_id):
    case = next(item for item in EVALUATION.cases if item.id == case_id)
    passage_by_key = {passage.key: passage for passage in CORPUS.corpus.passages}
    context = EvidenceContext(
        scenario_version_id=UUID("5bc6c117-a22e-44b6-bf87-9a282e9fe679"),
        scenario_slug=EVALUATION.scenario_slug,
        scenario_version=EVALUATION.scenario_version,
        actor_id=case.actor_id,
        world_date=case.world_date,
        mode=case.mode,
        evidence_policy=SOURCE_POLICY,
    )
    approval = PassageApproval(
        case.simulated_review.review_status,
        case.simulated_review.rights_status,
        1,
    )
    for key in case.expected_passage_keys:
        assert is_eligible(convert(passage_by_key[key]), context, approval, SOURCE_POLICY)


@pytest.mark.parametrize(
    "case_id",
    [
        "schedule_1921_date_leak",
        "confidential_minutes_actor_leak",
        "rights_pending_matching_passage",
        "unsupported_versailles_amount_claim",
    ],
)
def test_labeled_negative_cases_are_ineligible_and_abstain(case_id):
    case = next(item for item in EVALUATION.cases if item.id == case_id)
    passage_by_key = {passage.key: passage for passage in CORPUS.corpus.passages}
    context = EvidenceContext(
        scenario_version_id=UUID("5bc6c117-a22e-44b6-bf87-9a282e9fe679"),
        scenario_slug=EVALUATION.scenario_slug,
        scenario_version=EVALUATION.scenario_version,
        actor_id=case.actor_id,
        world_date=case.world_date,
        mode=case.mode,
        evidence_policy=SOURCE_POLICY,
    )
    approval = PassageApproval(
        case.simulated_review.review_status,
        case.simulated_review.rights_status,
        1,
    )
    assert case.expected_status == "RAG_NO_EVIDENCE"
    assert not case.expected_passage_keys
    for key in case.blocked_passage_keys:
        assert not is_eligible(convert(passage_by_key[key]), context, approval, SOURCE_POLICY)
