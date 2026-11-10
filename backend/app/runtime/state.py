"""Deterministic projection of accepted schema-v2 events; events are the authority."""
from copy import deepcopy
from datetime import date
from typing import Iterable

from pydantic import TypeAdapter, ValidationError

from app.runtime.proposals import StateEvent

_EVENT_ADAPTER = TypeAdapter(StateEvent)


class StateProjectionError(ValueError):
    """An accepted-event stream cannot be applied to the supplied projection."""


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise StateProjectionError("projection world date is invalid")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise StateProjectionError("projection world date is invalid") from exc
    if result.isoformat() != value:
        raise StateProjectionError("projection world date is invalid")
    return result


def _validated_initial_state(initial_state: dict) -> dict:
    if not isinstance(initial_state, dict) or initial_state.get("runtime_version") != "v2_ai":
        raise StateProjectionError("projection requires a v2_ai initial state")
    _parse_date(initial_state.get("world_date"))
    state = deepcopy(initial_state)
    for field in (
        "open_threads",
        "decisions",
        "simulation_events",
        "relations",
        "historical_claims",
        "author_context_references",
        "player_claims",
        "learning_observations",
    ):
        if field not in state:
            state[field] = []
        if not isinstance(state[field], list):
            raise StateProjectionError("projection collection has an invalid shape")
    seen_threads: set[str] = set()
    for thread in state["open_threads"]:
        if (
            not isinstance(thread, dict)
            or not isinstance(thread.get("id"), str)
            or not isinstance(thread.get("text"), str)
            or thread.get("status") not in {"open", "resolved"}
            or thread["id"] in seen_threads
        ):
            raise StateProjectionError("initial thread projection is invalid")
        seen_threads.add(thread["id"])
    for relation in state["relations"]:
        if (
            not isinstance(relation, dict)
            or not isinstance(relation.get("actor_a_id"), str)
            or not isinstance(relation.get("actor_b_id"), str)
            or relation["actor_a_id"] >= relation["actor_b_id"]
            or relation.get("relation")
            not in {"cooperative", "strained", "contested", "unknown"}
            or not isinstance(relation.get("reason"), str)
        ):
            raise StateProjectionError("initial relation projection is invalid")
    return state


def project_state(initial_state: dict, ordered_events: Iterable[StateEvent | dict]) -> dict:
    """Fold an ordered event stream into a fresh projection without changing its inputs."""
    state = _validated_initial_state(initial_state)
    for raw_event in ordered_events:
        if isinstance(raw_event, dict) and "payload" in raw_event:
            event_type = raw_event.get("event_type")
            payload = raw_event.get("payload")
            if not isinstance(event_type, str) or not isinstance(payload, dict):
                raise StateProjectionError("event envelope is invalid")
            raw_event = {**payload, "event_type": event_type}
        try:
            event = _EVENT_ADAPTER.validate_python(raw_event)
        except (ValidationError, TypeError, ValueError) as exc:
            raise StateProjectionError("event stream contains an invalid event") from exc
        _apply_event(state, event)
    return state


def _apply_event(state: dict, event: object) -> None:
    event_type = event.event_type
    if event_type == "decision_recorded":
        state["decisions"].append(
            {
                "summary": event.summary,
                "player_action": event.player_action,
                "truth_type": "player_decision",
            }
        )
        return
    if event_type == "thread_opened":
        if any(thread["id"] == event.thread_id for thread in state["open_threads"]):
            raise StateProjectionError("thread id already exists")
        state["open_threads"].append(
            {"id": event.thread_id, "text": event.summary, "status": "open"}
        )
        return
    if event_type == "thread_resolved":
        thread = next(
            (item for item in state["open_threads"] if item["id"] == event.thread_id), None
        )
        if thread is None:
            raise StateProjectionError("thread id is unknown")
        if thread["status"] != "open":
            raise StateProjectionError("thread is not open")
        thread["status"] = "resolved"
        thread["resolution"] = event.resolution
        return
    if event_type == "simulation_event_recorded":
        state["simulation_events"].append(
            {
                "summary": event.summary,
                "actor_ids": list(event.actor_ids),
                "truth_type": "simulation",
            }
        )
        return
    if event_type == "relation_changed":
        actor_a_id, actor_b_id = sorted((event.actor_a_id, event.actor_b_id))
        relation = next(
            (
                item
                for item in state["relations"]
                if item.get("actor_a_id") == actor_a_id
                and item.get("actor_b_id") == actor_b_id
            ),
            None,
        )
        replacement = {
            "actor_a_id": actor_a_id,
            "actor_b_id": actor_b_id,
            "relation": event.relation,
            "reason": event.reason,
        }
        if relation is None:
            state["relations"].append(replacement)
        else:
            relation.update(replacement)
        state["relations"].sort(key=lambda item: (item["actor_a_id"], item["actor_b_id"]))
        return
    if event_type == "world_time_advanced":
        current = _parse_date(state["world_date"])
        from_date = _parse_date(event.from_date)
        to_date = _parse_date(event.to_date)
        if current != from_date:
            raise StateProjectionError("event date does not match current projection date")
        if to_date <= from_date:
            raise StateProjectionError("world time must advance")
        state["world_date"] = event.to_date
        return
    if event_type == "historical_claim_recorded":
        state["historical_claims"].append(
            {
                "claim": event.claim,
                "evidence_ids": list(event.evidence_ids),
                "truth_type": "historical_evidence",
                "world_date": event.world_date,
                "actor_id": event.actor_id,
            }
        )
        return
    if event_type == "author_context_referenced":
        state["author_context_references"].append(
            {
                "item_key": event.item_key,
                "truth_type": "author_context",
                "world_date": event.world_date,
                "actor_id": event.actor_id,
            }
        )
        return
    if event_type == "player_claim_recorded":
        state["player_claims"].append(
            {
                "text": event.text,
                "truth_type": "player_claim",
                "world_date": event.world_date,
                "actor_id": event.actor_id,
            }
        )
        return
    if event_type == "learning_observation_recorded":
        state["learning_observations"].append(
            {
                "objective_id": event.objective_id,
                "observation": event.observation,
                "truth_type": "learning_observation",
                "world_date": event.world_date,
                "actor_id": event.actor_id,
            }
        )
        return
    if event_type == "scene_completed":
        if state.get("completion_status") == "complete":
            raise StateProjectionError("scene is already complete")
        state["completion_status"] = "complete"
        return
    raise StateProjectionError("event type is unsupported")
