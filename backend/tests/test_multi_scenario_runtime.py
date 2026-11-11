"""The published v2 cases remain scenario-data driven, not engine-specific."""
import json
import runpy
from datetime import date
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.rag.corpus import load_corpus
from app.rag.repository import _validate_scenario_policy
from app.runtime.context import SessionPromptContext
from app.runtime.orchestrator import (
    REPAIR_GUIDANCE,
    SYSTEM_PROMPT,
    TURN_PROMPT_VERSION,
    TURN_SCHEMA_VERSION,
    _build_turn_prompt,
    _explicit_evidence_search_queries,
    _narration_quality_issue_codes,
    _remaining_evidence_search_budget,
)
from app.runtime.proposals import EvidenceSearchToolCall, TurnAgentResponse
from app.scenarios.loader import load_scenario
from app.scenarios.schema_v2 import ScenarioDefinitionV2

ROOT = Path(__file__).parents[2]
VERSAILLES = ROOT / "scenarios" / "versailles-1919" / "3.1-ai.yaml"
INDUSTRIAL = ROOT / "scenarios" / "industrial-workplace" / "1.0.yaml"


def test_two_playable_v2_cases_have_distinct_data_and_reviewed_evidence():
    versailles = load_scenario(VERSAILLES)
    industrial = load_scenario(INDUSTRIAL)
    assert isinstance(versailles.definition, ScenarioDefinitionV2)
    assert isinstance(industrial.definition, ScenarioDefinitionV2)

    first = versailles.definition
    second = industrial.definition
    assert first.slug == "versailles-1919"
    assert second.slug == "industrial-workplace"
    assert first.version == "3.1-ai"
    assert second.version == "1.0"
    assert first.interaction_guidance.freedom == second.interaction_guidance.freedom == "open_ended"
    assert first.interaction_guidance.style != second.interaction_guidance.style
    assert {item.id for item in first.learning_objectives} != {
        item.id for item in second.learning_objectives
    }
    assert versailles.evidence_policy is None
    assert industrial.evidence_policy is not None
    assert {entry["source_id"] for entry in industrial.evidence_policy["source_access"]} == {
        "R01"
    }

    loaded_corpus = load_corpus(INDUSTRIAL.parent / "rag" / "corpus.yaml")
    corpus = loaded_corpus.corpus
    assert corpus.scenario_slug == second.slug
    assert corpus.scenario_version == second.version
    assert {passage.source_identifier for passage in corpus.passages} == {"R01"}
    assert all(passage.review_status == "pending" for passage in corpus.passages)
    assert all(passage.rights_status == "pending" for passage in corpus.passages)
    assert any(
        "less than 150 feet in height" in passage.passage_text
        for passage in corpus.passages
    )
    _validate_scenario_policy(
        loaded_corpus,
        SimpleNamespace(
            schema_version=2,
            definition=second.model_dump(mode="json"),
            evidence_policy=industrial.evidence_policy,
        ),
    )


def test_shared_turn_prompt_honors_explicit_requests_without_state_changes():
    assert TURN_PROMPT_VERSION == "freeform-turn-da-v13"
    assert TURN_SCHEMA_VERSION == "turn-agent-response-schema-v2"
    assert "state_operations være []" in SYSTEM_PROMPT
    assert "state_operations være []" in REPAIR_GUIDANCE["contradictory_operations"]
    assert "search_approved_evidence" in SYSTEM_PROMPT
    assert "current_state.present_actor_ids" in SYSTEM_PROMPT
    assert "Besvar direkte den nyeste" in SYSTEM_PROMPT
    assert "ordret fra spillerens præcise kildeudtryk" in SYSTEM_PROMPT
    assert "du bruger et returneret godkendt uddrag" in SYSTEM_PROMPT
    assert "udled ikke måned eller dag fra metadata" in SYSTEM_PROMPT
    assert 'omskriv ikke "kan" til "ofte"' in SYSTEM_PROMPT
    assert "gentag ikke en afsluttet søgning" in SYSTEM_PROMPT
    assert "turn_agent_response_invalid" in REPAIR_GUIDANCE
    assert "response-envelope" in REPAIR_GUIDANCE["turn_agent_response_invalid"]
    assert "truncated_output" in REPAIR_GUIDANCE
    assert "narration_incomplete" in REPAIR_GUIDANCE
    assert "narration_too_long" in REPAIR_GUIDANCE
    assert "repeated_previous_response" in REPAIR_GUIDANCE
    assert "searched_evidence_not_cited" in REPAIR_GUIDANCE
    assert "800 tegn" in SYSTEM_PROMPT


def test_prior_visible_answers_do_not_authorize_evidence_citations():
    assert "Tidligere synlige svar autoriserer ikke evidence-id'er i denne tur" in SYSTEM_PROMPT
    actions = runpy.run_path(str(ROOT / "scripts" / "ai_gameplay_smoke.py"))["ACTION_SCRIPTS"]
    scope_review_action = actions["industrial-workplace"][1]
    assert "query='ordinary wood'" in scope_review_action
    assert "query='irrespective of the exit facilities'" in scope_review_action


def test_only_explicitly_quoted_search_queries_are_recovered():
    assert _explicit_evidence_search_queries(
        "Brug query='ordinary wood' og query=\"exit facilities\"."
    ) == ["ordinary wood", "exit facilities"]
    assert _explicit_evidence_search_queries("Søg efter ordinary wood.") == []


def test_requested_search_budget_closes_after_exact_queries_complete():
    query = "ordinary wood"
    query_hash = sha256(query.encode("utf-8")).hexdigest()
    assert _remaining_evidence_search_budget(
        [{"query_sha256": query_hash}], 1, [query]
    ) == 0
    assert _remaining_evidence_search_budget([], 1, [query]) == 2


def test_narration_quality_checks_reject_incomplete_and_repeated_answers():
    assert _narration_quality_issue_codes("En afsluttet sætning.", []) == []
    assert _narration_quality_issue_codes("Et svar midt i", []) == ["narration_incomplete"]
    assert _narration_quality_issue_codes("a" * 1000 + ".", []) == [
        "narration_too_long"
    ]
    assert _narration_quality_issue_codes(
        "Det forrige svar.", [{"reply": "Det forrige svar."}]
    ) == ["repeated_previous_response"]
    earlier = (
        "Den eksisterende rapport peger på risikoen ved træværk i bygninger under 150 fod, "
        "men den fortæller ikke, hvor ofte problemet forekommer. Jeg foreslår derfor, at "
        "kommissionen undersøger flere fabrikker, før der fastsættes nye krav."
    )
    near_repeat = (
        "Rapporten peger på risiko ved træværk i bygninger under 150 fod, men den viser ikke, "
        "hvor ofte problemet forekommer. Derfor bør kommissionen undersøge flere fabrikker, "
        "før den fastsætter nye krav."
    )
    assert _narration_quality_issue_codes(near_repeat, [{"reply": earlier}]) == [
        "repeated_previous_response"
    ]


def test_narration_quality_rejects_a_repeated_sentence_inside_one_answer():
    repeated_sentence = (
        "Sagsmapperne stod høje og støvede i rummet, mens katten gemte sig mellem dem."
    )
    narration = (
        f"{repeated_sentence} Den ventede stille på, at nogen skulle åbne en mappe. "
        f"{repeated_sentence}"
    )
    assert _narration_quality_issue_codes(narration, []) == [
        "repeated_narration_sentence"
    ]


def test_turn_prompt_repeats_latest_action_after_context_and_history():
    context = SessionPromptContext(
        payload={"evidence": {"passages": []}},
        world_date=date(1919, 4, 1),
        evidence_status="no_eligible_sources",
        serialized_chars=0,
        stage="turn",
        mode="gameplay",
        metadata={"evidence": {"eligible_source_count": 0}},
    )
    action = "Uventet sidespor: skriv limerick."
    prompt, payload = _build_turn_prompt(
        context=context,
        current_state={"present_actor_ids": ["britain"]},
        recent_events=[],
        visible_turns=[{"player_action": "Ældre handling", "reply": "Ældre svar"}],
        player_action=action,
        allowed_actor_ids=["britain"],
        allowed_objective_ids=[],
        tool_calls_remaining=0,
        tool_history=[],
    )
    assert payload["player_action"] == action
    assert prompt.endswith(json.dumps(action, ensure_ascii=False))


def test_turn_model_can_request_multiple_bounded_approved_evidence_searches():
    response = TurnAgentResponse.model_validate(
        {
            "schema_version": 1,
            "tool_calls": [
                {"name": "search_approved_evidence", "query": "loftbygninger og udgange"},
                {"name": "search_approved_evidence", "query": "fabriksinspektion"},
            ],
            "proposal": None,
        }
    )
    assert len(response.tool_calls) == 2
    assert all(call.name == "search_approved_evidence" for call in response.tool_calls)

    with pytest.raises(ValidationError):
        EvidenceSearchToolCall(name="delete_database", query="alt")

    duplicate = {"name": "search_approved_evidence", "query": "same query"}
    with pytest.raises(ValidationError):
        TurnAgentResponse(schema_version=1, tool_calls=[duplicate, duplicate], proposal=None)
    with pytest.raises(ValidationError):
        TurnAgentResponse(
            schema_version=1,
            tool_calls=[
                {"name": "search_approved_evidence", "query": f"query {index}"}
                for index in range(4)
            ],
            proposal=None,
        )
