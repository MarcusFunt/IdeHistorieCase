"""Typed, bounded event proposals for authored scenario state."""
import pytest
from test_scenario_schema import MINIMAL_SCENARIO

from app.scenarios.engine import (
    AdjustMetricProposal,
    AdvancePhaseProposal,
    SetFlagProposal,
    apply_event_proposals,
)
from app.scenarios.loader import load_scenario_text


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
