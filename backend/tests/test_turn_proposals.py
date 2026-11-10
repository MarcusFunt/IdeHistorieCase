"""Turn proposals are untrusted bounded input, validated against pinned turn context."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.runtime.context import SessionPromptContext
from app.runtime.validator import ProposalValidationError, validate_turn_proposal
from app.scenarios.loader import load_scenario
from app.scenarios.schema_v2 import ActorDefinitionV2

FIXTURE = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry/1.0.yaml"
EVIDENCE_ID = "a6a00000-0000-4000-8000-000000000001"


def fixtures():
    definition = load_scenario(FIXTURE).definition
    role = next(role for role in definition.roles if role.id == definition.default_role_id)
    definition = definition.model_copy(
        update={
            "actors": [
                *definition.actors,
                ActorDefinitionV2(
                    id="witness",
                    name="Vidne",
                    description="En person med egne erfaringer.",
                ),
                ActorDefinitionV2(
                    id="union_rep",
                    name="Arbejderrepræsentant",
                    description="En person, der taler på arbejdernes vegne.",
                ),
            ]
        }
    )
    action = "Undersøg arbejdsvilkårene nærmere."
    context = SessionPromptContext(
        payload={
            "selected_role": {"id": role.id, "actor_id": role.actor_id},
            "evidence": {
                "passages": [
                    {"passage_id": EVIDENCE_ID, "citation_ready": True},
                    {"passage_id": "a6a00000-0000-4000-8000-000000000002", "citation_ready": False},
                ]
            },
            "author_notes": [{"item_key": "site_briefing"}],
        },
        world_date=date(1888, 5, 1),
        evidence_status="evidence_included",
        serialized_chars=500,
        stage="turn",
        mode="gameplay",
        metadata={},
    )
    state = {
        "runtime_version": "v2_ai",
        "world_date": "1888-05-01",
        "turn_number": 1,
        "present_actor_ids": [role.actor_id, "witness"],
        "open_threads": [{"id": "opening-1", "text": "Første spørgsmål", "status": "open"}],
        "decisions": [],
        "simulation_events": [],
        "relations": [],
    }
    proposal = {
        "schema_version": 1,
        "language": "da",
        "narration": "Undersøgelsen begynder.",
        "dialogue": [{"actor_id": role.actor_id, "text": "Hvad ønsker du at undersøge?"}],
        "outcome_summary": "Undersøgelsen er sat i gang.",
        "state_operations": [
            {"type": "record_decision", "summary": "Undersøg arbejdsvilkårene."},
            {"type": "open_thread", "thread_id": "wage_records", "summary": "Find lønregnskaber."},
            {"type": "resolve_thread", "thread_id": "opening-1", "resolution": "Spørgsmålet er undersøgt."},
            {
                "type": "record_simulation_event",
                "summary": "En simuleret samtale finder sted.",
                "actor_ids": [role.actor_id, "witness"],
            },
            {
                "type": "set_relation",
                "actor_a_id": role.actor_id,
                "actor_b_id": "witness",
                "relation": "cooperative",
                "reason": "Parterne samarbejder.",
            },
            {"type": "advance_world_time", "target_date": "1888-05-05"},
        ],
        "historical_claims": [
            {
                "truth_type": "historical_evidence",
                "claim": "En kilde belyser sagen.",
                "evidence_ids": [EVIDENCE_ID],
            }
        ],
        "author_context_references": [
            {"truth_type": "author_context", "item_key": "site_briefing"}
        ],
        "player_claims": [{"truth_type": "player_claim", "text": action}],
        "learning_observations": [
            {"objective_id": "weigh_evidence", "observation": "Spilleren efterspørger belæg."}
        ],
        "scene_status": "continue",
        "next_player_prompt": "Hvad vil du gøre nu?",
    }
    return definition, role, context, state, action, proposal


def validate(raw, *, definition, role, context, state, action):
    return validate_turn_proposal(
        raw,
        definition=definition,
        context=context,
        current_state=state,
        role_id=role.id,
        player_action=action,
    )


def test_validates_every_operation_and_keeps_truth_categories_separate():
    definition, role, context, state, action, raw = fixtures()
    accepted = validate(
        raw, definition=definition, role=role, context=context, state=state, action=action
    )

    assert [event.event_type for event in accepted.events] == [
        "historical_claim_recorded",
        "author_context_referenced",
        "player_claim_recorded",
        "learning_observation_recorded",
        "decision_recorded",
        "thread_opened",
        "thread_resolved",
        "simulation_event_recorded",
        "relation_changed",
        "world_time_advanced",
    ]
    assert accepted.proposal.historical_claims[0].truth_type == "historical_evidence"
    assert accepted.proposal.author_context_references[0].truth_type == "author_context"
    assert accepted.proposal.player_claims[0].truth_type == "player_claim"
    assert accepted.events[4].player_action == action
    assert accepted.events[0].world_date == "1888-05-01"
    assert accepted.events[0].actor_id == role.actor_id
    assert state["world_date"] == "1888-05-01"


@pytest.mark.parametrize(
    ("mutate", "issue"),
    [
        (lambda p: p["state_operations"][0].update(type="replace_state"), "unsupported_operation"),
        (lambda p: p["state_operations"][3].update(actor_ids=["unknown_actor"]), "unknown_actor_id"),
        (lambda p: p["state_operations"][3].update(actor_ids=["union_rep"]), "inaccessible_actor"),
        (lambda p: p["state_operations"][1].update(thread_id="bad id"), "schema_invalid"),
        (lambda p: p["state_operations"][2].update(thread_id="missing_thread"), "unknown_thread_id"),
        (lambda p: p["state_operations"][5].update(target_date="1888-05-20"), "invalid_time_advance"),
        (lambda p: p["state_operations"][5].update(target_date="1888-02-30"), "schema_invalid"),
        (lambda p: p["historical_claims"][0].update(evidence_ids=["a6a00000-0000-4000-8000-000000000002"]), "evidence_not_in_context"),
        (lambda p: p["author_context_references"][0].update(item_key="missing_note"), "author_note_not_in_context"),
        (lambda p: p["player_claims"][0].update(text="En anden påstand"), "player_claim_mismatch"),
        (lambda p: p["learning_observations"][0].update(objective_id="unknown_goal"), "unknown_objective_id"),
        (lambda p: p["state_operations"][4].update(actor_b_id="union_rep"), "inaccessible_actor"),
        (lambda p: p.update(extra_state_patch={"world_date": "9999-01-01"}), "schema_invalid"),
    ],
)
def test_rejects_malformed_unknown_unauthorized_and_unsupported_proposals(mutate, issue):
    definition, role, context, state, action, raw = fixtures()
    bad = deepcopy(raw)
    mutate(bad)

    with pytest.raises(ProposalValidationError) as exc:
        validate(bad, definition=definition, role=role, context=context, state=state, action=action)

    assert issue in exc.value.issues


def test_rejects_oversized_and_contradictory_operations():
    definition, role, context, state, action, raw = fixtures()
    oversized = deepcopy(raw)
    oversized["narration"] = "x" * 1201
    with pytest.raises(ProposalValidationError) as exc:
        validate(oversized, definition=definition, role=role, context=context, state=state, action=action)
    assert "schema_invalid" in exc.value.issues

    too_large = deepcopy(raw)
    too_large["narration"] = "n" * 1200
    too_large["dialogue"] = [
        {"actor_id": role.actor_id, "text": "d" * 600} for _ in range(16)
    ]
    too_large["state_operations"] = [
        {"type": "record_decision", "summary": "o" * 240}
        for _ in range(20)
    ]
    too_large["historical_claims"] = [
        {
            "truth_type": "historical_evidence",
            "claim": "c" * 500,
            "evidence_ids": ["e" * 79 + str(index) for index in range(4)],
        }
        for _ in range(10)
    ]
    too_large["author_context_references"] = [
        {"truth_type": "author_context", "item_key": "a" * 64} for _ in range(10)
    ]
    too_large["player_claims"] = [
        {"truth_type": "player_claim", "text": "p" * 500} for _ in range(3)
    ]
    too_large["learning_observations"] = [
        {"objective_id": "weigh_evidence", "observation": "l" * 240} for _ in range(20)
    ]
    with pytest.raises(ProposalValidationError) as exc:
        validate(too_large, definition=definition, role=role, context=context, state=state, action=action)
    assert "proposal_too_large" in exc.value.issues

    contradictory = deepcopy(raw)
    contradictory["state_operations"].append(
        {"type": "open_thread", "thread_id": "wage_records", "summary": "Dobbelt tråd."}
    )
    with pytest.raises(ProposalValidationError) as exc:
        validate(contradictory, definition=definition, role=role, context=context, state=state, action=action)
    assert "contradictory_operations" in exc.value.issues


def test_rejects_time_moves_outside_chronology_and_stale_context():
    definition, role, context, state, action, raw = fixtures()
    too_soon = deepcopy(raw)
    too_soon["state_operations"][5]["target_date"] = "1888-05-01"
    with pytest.raises(ProposalValidationError) as exc:
        validate(too_soon, definition=definition, role=role, context=context, state=state, action=action)
    assert "invalid_time_advance" in exc.value.issues

    state["world_date"] = "1888-05-02"
    with pytest.raises(ProposalValidationError) as exc:
        validate(raw, definition=definition, role=role, context=context, state=state, action=action)
    assert "context_state_mismatch" in exc.value.issues


def test_rejects_postgame_context_for_live_turn_proposals():
    definition, role, context, state, action, raw = fixtures()
    postgame_context = replace(context, mode="postgame")
    with pytest.raises(ProposalValidationError) as exc:
        validate(
            raw,
            definition=definition,
            role=role,
            context=postgame_context,
            state=state,
            action=action,
        )
    assert "invalid_context_mode" in exc.value.issues


def test_rejects_completion_until_pinned_completion_policy_is_enforced():
    definition, role, context, state, action, raw = fixtures()
    complete = deepcopy(raw)
    complete["scene_status"] = "complete"
    complete["next_player_prompt"] = None
    with pytest.raises(ProposalValidationError) as exc:
        validate(complete, definition=definition, role=role, context=context, state=state, action=action)
    assert "completion_not_supported" in exc.value.issues


def test_rejects_duplicate_threads_relations_and_multiple_time_advances():
    definition, role, context, state, action, raw = fixtures()
    bad = deepcopy(raw)
    bad["state_operations"].extend(
        [
            {
                "type": "set_relation",
                "actor_a_id": role.actor_id,
                "actor_b_id": "witness",
                "relation": "strained",
                "reason": "Modstridende ændring.",
            },
            {"type": "advance_world_time", "target_date": "1888-05-06"},
        ]
    )
    with pytest.raises(ProposalValidationError) as exc:
        validate(bad, definition=definition, role=role, context=context, state=state, action=action)
    assert "contradictory_operations" in exc.value.issues

    bad = deepcopy(raw)
    advance = bad["state_operations"].pop()
    bad["state_operations"].insert(0, advance)
    with pytest.raises(ProposalValidationError) as exc:
        validate(bad, definition=definition, role=role, context=context, state=state, action=action)
    assert "contradictory_operations" in exc.value.issues
