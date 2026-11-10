"""The v2 state projection is a deterministic fold over accepted typed events."""
from copy import deepcopy

import pytest

from app.runtime.state import StateProjectionError, project_state


def initial_state():
    return {
        "runtime_version": "v2_ai",
        "world_date": "1888-05-01",
        "turn_number": 0,
        "scene": {"title": "Undersøgelse", "description": "En sag undersøges."},
        "present_actor_ids": ["factory_owner", "witness"],
        "open_threads": [{"id": "opening-1", "text": "Første spørgsmål", "status": "open"}],
        "decisions": [],
        "simulation_events": [],
        "relations": [],
        "historical_claims": [],
    }


def events():
    return [
        {
            "event_type": "historical_claim_recorded",
            "truth_type": "historical_evidence",
            "claim": "En kilde belyser sagen.",
            "evidence_ids": ["a6a00000-0000-4000-8000-000000000001"],
            "world_date": "1888-05-01",
            "actor_id": "factory_owner",
        },
        {
            "event_type": "author_context_referenced",
            "truth_type": "author_context",
            "item_key": "site_briefing",
            "world_date": "1888-05-01",
            "actor_id": "factory_owner",
        },
        {
            "event_type": "player_claim_recorded",
            "truth_type": "player_claim",
            "text": "Undersøg arbejdsvilkårene nærmere.",
            "world_date": "1888-05-01",
            "actor_id": "factory_owner",
        },
        {
            "event_type": "learning_observation_recorded",
            "truth_type": "learning_observation",
            "objective_id": "weigh_evidence",
            "observation": "Spilleren efterspørger belæg.",
            "world_date": "1888-05-01",
            "actor_id": "factory_owner",
        },
        {
            "event_type": "decision_recorded",
            "summary": "Undersøg lønregnskaber.",
            "player_action": "Undersøg arbejdsvilkårene nærmere.",
        },
        {
            "event_type": "thread_opened",
            "thread_id": "wage_records",
            "summary": "Find lønregnskaber.",
        },
        {
            "event_type": "thread_resolved",
            "thread_id": "opening-1",
            "resolution": "Spørgsmålet er undersøgt.",
        },
        {
            "event_type": "simulation_event_recorded",
            "summary": "En simuleret samtale finder sted.",
            "actor_ids": ["factory_owner", "witness"],
        },
        {
            "event_type": "relation_changed",
            "actor_a_id": "factory_owner",
            "actor_b_id": "witness",
            "relation": "strained",
            "reason": "De er uenige om fortolkningen.",
        },
        {
            "event_type": "world_time_advanced",
            "from_date": "1888-05-01",
            "to_date": "1888-05-05",
        },
        {"event_type": "scene_completed"},
    ]


def test_projection_replays_all_event_types_in_order_without_mutating_inputs():
    state = initial_state()
    log = events()
    original_state = deepcopy(state)
    original_log = deepcopy(log)

    projection = project_state(state, log)

    assert projection["world_date"] == "1888-05-05"
    assert projection["decisions"] == [
        {
            "summary": "Undersøg lønregnskaber.",
            "player_action": "Undersøg arbejdsvilkårene nærmere.",
            "truth_type": "player_decision",
        }
    ]
    assert projection["open_threads"] == [
        {
            "id": "opening-1",
            "text": "Første spørgsmål",
            "status": "resolved",
            "resolution": "Spørgsmålet er undersøgt.",
        },
        {"id": "wage_records", "text": "Find lønregnskaber.", "status": "open"},
    ]
    assert projection["simulation_events"][0]["truth_type"] == "simulation"
    assert projection["historical_claims"][0]["truth_type"] == "historical_evidence"
    assert projection["historical_claims"][0]["world_date"] == "1888-05-01"
    assert projection["historical_claims"][0]["actor_id"] == "factory_owner"
    assert projection["author_context_references"][0]["truth_type"] == "author_context"
    assert projection["player_claims"][0]["truth_type"] == "player_claim"
    assert projection["learning_observations"][0]["truth_type"] == "learning_observation"
    assert projection["completion_status"] == "complete"
    assert projection["relations"] == [
        {
            "actor_a_id": "factory_owner",
            "actor_b_id": "witness",
            "relation": "strained",
            "reason": "De er uenige om fortolkningen.",
        }
    ]
    assert state == original_state
    assert log == original_log
    assert project_state(state, log) == projection
    database_events = [
        {"event_type": event["event_type"], "payload": {key: value for key, value in event.items() if key != "event_type"}}
        for event in log
    ]
    assert project_state(state, database_events) == projection


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "replace_state", "state": {"world_date": "9999-01-01"}},
        {"event_type": "decision_recorded", "summary": "Et valg.", "state_patch": {"turn_number": 900}},
        {"event_type": "thread_resolved", "thread_id": "missing", "resolution": "Løst."},
        {
            "event_type": "world_time_advanced",
            "from_date": "1888-05-02",
            "to_date": "1888-05-04",
        },
    ],
)
def test_projection_rejects_unknown_patch_fields_and_invalid_event_references(event):
    with pytest.raises(StateProjectionError):
        project_state(initial_state(), [event])


def test_projection_rejects_thread_reuse_and_reversed_time():
    with pytest.raises(StateProjectionError, match="already exists"):
        project_state(
            initial_state(),
            [{"event_type": "thread_opened", "thread_id": "opening-1", "summary": "Kopi."}],
        )
    with pytest.raises(StateProjectionError, match="must advance"):
        project_state(
            initial_state(),
            [
                {
                    "event_type": "world_time_advanced",
                    "from_date": "1888-05-01",
                    "to_date": "1888-04-30",
                }
            ],
        )
    with pytest.raises(StateProjectionError, match="already complete"):
        project_state(
            initial_state(),
            [{"event_type": "scene_completed"}, {"event_type": "scene_completed"}],
        )
