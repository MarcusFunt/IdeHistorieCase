"""Typed, bounded event proposals for authored scenario state."""
from pathlib import Path

import pytest
from test_scenario_schema import MINIMAL_SCENARIO

from app.scenarios.engine import (
    AdjustMetricProposal,
    AdvancePhaseProposal,
    IssueDecisionProposal,
    SetFlagProposal,
    apply_event_proposals,
)
from app.scenarios.loader import load_scenario, load_scenario_text


def loaded_fixture():
    return load_scenario_text(MINIMAL_SCENARIO)


def test_applies_multiple_bounded_events_and_phase_transition_atomically():
    loaded = loaded_fixture()
    original = loaded.initial_state
    proposal = [
        AdjustMetricProposal(type="adjust_metric", target_id="support", delta=5),
        SetFlagProposal(type="set_flag", target_id="agreement_reached"),
        AdvancePhaseProposal(type="advance_phase", target_phase_id="reflection"),
    ]

    next_state, accepted = apply_event_proposals(loaded.definition, original, proposal)

    assert original["phase"] == "opening"
    assert next_state["phase"] == "reflection"
    assert next_state["metrics"]["support"] == 55
    assert next_state["flags"]["agreement_reached"] is True
    assert [event["type"] for event in accepted] == [
        "adjust_metric",
        "set_flag",
        "advance_phase",
    ]


def test_rejects_metric_delta_outside_permission_or_state_range():
    loaded = loaded_fixture()
    proposal = [AdjustMetricProposal(type="adjust_metric", target_id="support", delta=11)]

    with pytest.raises(ValueError, match="permitted delta"):
        apply_event_proposals(loaded.definition, loaded.initial_state, proposal)


def test_rejects_unauthorized_flag_and_phase_jump_without_mutating_state():
    loaded = loaded_fixture()
    original = loaded.initial_state

    with pytest.raises(ValueError, match="not authorized"):
        apply_event_proposals(
            loaded.definition,
            original,
            [SetFlagProposal(type="set_flag", target_id="unknown_flag")],
        )
    with pytest.raises(ValueError, match="not allowed from phase"):
        apply_event_proposals(
            loaded.definition,
            original,
            [AdvancePhaseProposal(type="advance_phase", target_phase_id="opening")],
        )

    assert original == loaded.initial_state


def test_rejects_more_than_one_phase_advance_per_turn():
    loaded = loaded_fixture()

    with pytest.raises(ValueError, match="one phase advance"):
        apply_event_proposals(
            loaded.definition,
            loaded.initial_state,
            [
                AdvancePhaseProposal(type="advance_phase", target_phase_id="reflection"),
                AdvancePhaseProposal(type="advance_phase", target_phase_id="opening"),
            ],
        )



def test_issue_decisions_emit_repeatable_qualitative_reactions_without_history_claims():
    manifest = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
    loaded = load_scenario(manifest)
    state = {**loaded.initial_state, "phase": "security_rhineland"}
    first = [
        IssueDecisionProposal(
            type="issue_decision",
            issue_id="rhineland_security",
            decision_code="demilitarized_with_guarantees",
        )
    ]
    alternative = [
        IssueDecisionProposal(
            type="issue_decision",
            issue_id="rhineland_security",
            decision_code="permanent_occupation",
        )
    ]

    _, result_one = apply_event_proposals(
        loaded.definition, state, first, behavior_rules=loaded.behavior_rules
    )
    _, result_repeat = apply_event_proposals(
        loaded.definition, state, first, behavior_rules=loaded.behavior_rules
    )
    _, result_alternative = apply_event_proposals(
        loaded.definition, state, alternative, behavior_rules=loaded.behavior_rules
    )

    assert result_one == result_repeat
    assert result_one[0]["qualitative_only"] is True
    assert result_one[0]["reaction"] != result_alternative[0]["reaction"]
    assert "historical" not in result_one[0]["reaction"].lower()



def test_authored_terminal_policy_requires_a_consistent_settlement_outcome():
    manifest = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
    loaded = load_scenario(manifest)
    state = {
        **loaded.initial_state,
        "phase": "final_settlement",
        "visited_phases": [*loaded.initial_state["visited_phases"], "final_settlement"],
    }

    with pytest.raises(ValueError, match="requires decision"):
        apply_event_proposals(
            loaded.definition,
            state,
            [{"type": "advance_phase", "target_phase_id": "reflection"}],
            behavior_rules=loaded.behavior_rules,
        )

    with pytest.raises(ValueError, match="conflicts with the selected outcome"):
        apply_event_proposals(
            loaded.definition,
            state,
            [
                {
                    "type": "issue_decision",
                    "issue_id": "final_package",
                    "decision_code": "reconstruction_compromise",
                },
                {"type": "set_flag", "target_id": "deadlock"},
            ],
            behavior_rules=loaded.behavior_rules,
        )
    assert state["decisions"] == {}
    assert state["flags"]["agreement_reached"] is False
    assert state["flags"]["deadlock"] is False


def test_deadlock_terminal_outcome_is_recorded_and_consistent():
    manifest = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
    loaded = load_scenario(manifest)
    state = {**loaded.initial_state, "phase": "final_settlement"}
    result, events = apply_event_proposals(
        loaded.definition,
        state,
        [
            {
                "type": "issue_decision",
                "issue_id": "final_package",
                "decision_code": "deadlock",
            },
            {"type": "set_flag", "target_id": "deadlock"},
            {"type": "advance_phase", "target_phase_id": "reflection"},
        ],
        behavior_rules=loaded.behavior_rules,
    )

    assert result["decisions"]["final_package"] == "deadlock"
    assert result["flags"]["deadlock"] is True
    assert result["flags"]["agreement_reached"] is False
    assert result["phase"] == "reflection"
    assert [event["type"] for event in events] == [
        "issue_decision",
        "set_flag",
        "advance_phase",
    ]


def test_unresolved_issues_are_recorded_as_skipped_when_leaving_a_phase():
    manifest = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
    loaded = load_scenario(manifest)
    state = {**loaded.initial_state, "phase": "security_rhineland"}
    result, _ = apply_event_proposals(
        loaded.definition,
        state,
        [{"type": "advance_phase", "target_phase_id": "reparations_framework"}],
        behavior_rules=loaded.behavior_rules,
    )

    assert set(result["skipped_issues"]) >= {"rhineland_security", "army_limit"}


def test_final_package_cannot_change_after_outcome_is_committed():
    manifest = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
    loaded = load_scenario(manifest)
    state = {
        **loaded.initial_state,
        "phase": "final_settlement",
        "decisions": {"final_package": "reconstruction_compromise"},
        "flags": {**loaded.initial_state["flags"], "agreement_reached": True},
    }

    with pytest.raises(ValueError, match="cannot change after an outcome flag"):
        apply_event_proposals(
            loaded.definition,
            state,
            [{
                "type": "issue_decision",
                "issue_id": "final_package",
                "decision_code": "deadlock",
            }],
            behavior_rules=loaded.behavior_rules,
        )
    assert state["decisions"]["final_package"] == "reconstruction_compromise"
    assert state["flags"]["agreement_reached"] is True
    assert state["flags"]["deadlock"] is False
