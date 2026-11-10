"""Application-side validation for model-proposed v2 turn operations."""
import json
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from app.runtime.context import SessionPromptContext
from app.runtime.proposals import (
    MAX_PROPOSAL_JSON_CHARS,
    AdvanceWorldTimeOperation,
    AuthorContextReference,
    AuthorContextReferencedEvent,
    DecisionRecordedEvent,
    HistoricalClaimRecordedEvent,
    HistoricalEvidenceClaim,
    LearningObservationRecordedEvent,
    OpenThreadOperation,
    PlayerClaimRecordedEvent,
    RecordDecisionOperation,
    RecordSimulationEventOperation,
    RelationChangedEvent,
    ResolveThreadOperation,
    SceneCompletedEvent,
    SetRelationOperation,
    SimulationEventRecordedEvent,
    StateEvent,
    StateOperation,
    ThreadOpenedEvent,
    ThreadResolvedEvent,
    TurnProposal,
    WorldTimeAdvancedEvent,
)
from app.runtime.state import StateProjectionError, project_state
from app.scenarios.schema_v2 import ScenarioDefinitionV2


class ProposalValidationError(ValueError):
    """A proposal was discarded before commit; issues are safe stable codes."""

    def __init__(self, issues: list[str] | tuple[str, ...]):
        self.issues = tuple(dict.fromkeys(issues))
        super().__init__("turn proposal rejected: " + ", ".join(self.issues))


@dataclass(frozen=True)
class ValidatedTurnProposal:
    proposal: TurnProposal
    events: tuple[StateEvent, ...]


def _schema_issues(exc: ValidationError) -> list[str]:
    if any(error.get("type") == "union_tag_invalid" for error in exc.errors()):
        return ["unsupported_operation"]
    return ["schema_invalid"]


def _state_date(state: dict) -> date | None:
    value = state.get("world_date") if isinstance(state, dict) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _operation_event(
    operation: StateOperation, current_date: date, player_action: str
) -> StateEvent:
    if isinstance(operation, RecordDecisionOperation):
        return DecisionRecordedEvent(
            event_type="decision_recorded",
            summary=operation.summary,
            player_action=player_action.strip(),
        )
    if isinstance(operation, OpenThreadOperation):
        return ThreadOpenedEvent(
            event_type="thread_opened", thread_id=operation.thread_id, summary=operation.summary
        )
    if isinstance(operation, ResolveThreadOperation):
        return ThreadResolvedEvent(
            event_type="thread_resolved",
            thread_id=operation.thread_id,
            resolution=operation.resolution,
        )
    if isinstance(operation, RecordSimulationEventOperation):
        return SimulationEventRecordedEvent(
            event_type="simulation_event_recorded",
            summary=operation.summary,
            actor_ids=operation.actor_ids,
        )
    if isinstance(operation, SetRelationOperation):
        return RelationChangedEvent(
            event_type="relation_changed",
            actor_a_id=operation.actor_a_id,
            actor_b_id=operation.actor_b_id,
            relation=operation.relation,
            reason=operation.reason,
        )
    if isinstance(operation, AdvanceWorldTimeOperation):
        return WorldTimeAdvancedEvent(
            event_type="world_time_advanced",
            from_date=current_date.isoformat(),
            to_date=operation.target_date,
        )
    raise ProposalValidationError(["unsupported_operation"])


def validate_turn_proposal(
    raw: object,
    *,
    definition: ScenarioDefinitionV2,
    context: SessionPromptContext,
    current_state: dict,
    role_id: str,
    player_action: str,
) -> ValidatedTurnProposal:
    """Validate model output against the pinned release, state, role, and turn context."""
    try:
        proposal = TurnProposal.model_validate(raw)
    except ValidationError as exc:
        raise ProposalValidationError(_schema_issues(exc)) from exc

    try:
        serialized_size = len(
            json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise ProposalValidationError(["schema_invalid"]) from exc
    if serialized_size > MAX_PROPOSAL_JSON_CHARS:
        raise ProposalValidationError(["proposal_too_large"])

    issues: list[str] = []
    if context.stage != "turn" or context.mode != "gameplay":
        issues.append("invalid_context_mode")
    roles = {role.id: role for role in definition.roles}
    role = roles.get(role_id)
    selected_role = context.payload.get("selected_role")
    if (
        role is None
        or not role.playable
        or not isinstance(selected_role, dict)
        or selected_role.get("id") != role_id
        or selected_role.get("actor_id") != role.actor_id
    ):
        issues.append("role_mismatch")

    current_date = _state_date(current_state)
    if current_date is None or current_date.isoformat() != context.world_date.isoformat():
        issues.append("context_state_mismatch")
    if current_date is not None and not (
        definition.chronology.opening_date
        <= current_date
        <= definition.metadata.historical_period.end
    ):
        issues.append("invalid_current_state")
    actors = {actor.id for actor in definition.actors}
    if not isinstance(current_state, dict) or current_state.get("runtime_version") != "v2_ai":
        issues.append("invalid_current_state")
        current_state = {}
    present_actor_ids = current_state.get("present_actor_ids")
    if (
        not isinstance(present_actor_ids, list)
        or any(not isinstance(actor_id, str) for actor_id in present_actor_ids)
        or len(present_actor_ids) != len(set(present_actor_ids))
        or any(actor_id not in actors for actor_id in present_actor_ids)
    ):
        issues.append("invalid_current_state")
        present_actor_ids = []
    accessible_actors = set(present_actor_ids)
    if role is not None and role.actor_id not in accessible_actors:
        issues.append("invalid_current_state")

    objectives = {objective.id for objective in definition.learning_objectives}
    for observation in proposal.learning_observations:
        if observation.objective_id not in objectives:
            issues.append("unknown_objective_id")

    evidence = context.payload.get("evidence", {})
    passages = evidence.get("passages", []) if isinstance(evidence, dict) else []
    evidence_ids = {
        passage.get("passage_id")
        for passage in passages
        if isinstance(passage, dict)
        and passage.get("citation_ready") is True
        and isinstance(passage.get("passage_id"), str)
    } if isinstance(passages, list) else set()
    for claim in proposal.historical_claims:
        if not isinstance(claim, HistoricalEvidenceClaim) or any(
            evidence_id not in evidence_ids for evidence_id in claim.evidence_ids
        ):
            issues.append("evidence_not_in_context")

    author_notes = context.payload.get("author_notes", [])
    note_ids = {
        note.get("item_key")
        for note in author_notes
        if isinstance(note, dict) and isinstance(note.get("item_key"), str)
    } if isinstance(author_notes, list) else set()
    for reference in proposal.author_context_references:
        if not isinstance(reference, AuthorContextReference) or reference.item_key not in note_ids:
            issues.append("author_note_not_in_context")

    if any(claim.text != player_action.strip() for claim in proposal.player_claims):
        issues.append("player_claim_mismatch")
    if len({claim.text for claim in proposal.player_claims}) != len(proposal.player_claims):
        issues.append("contradictory_operations")
    if proposal.scene_status == "complete":
        issues.append("completion_not_supported")

    def check_actor(actor_id: str) -> None:
        if actor_id not in actors:
            issues.append("unknown_actor_id")
        elif actor_id not in accessible_actors:
            issues.append("inaccessible_actor")

    for line in proposal.dialogue:
        check_actor(line.actor_id)

    existing_threads = current_state.get("open_threads", [])
    if not isinstance(existing_threads, list):
        issues.append("invalid_current_state")
        existing_threads = []
    thread_status: dict[str, str] = {}
    for thread in existing_threads:
        if (
            not isinstance(thread, dict)
            or not isinstance(thread.get("id"), str)
            or thread.get("status") not in {"open", "resolved"}
            or thread["id"] in thread_status
        ):
            issues.append("invalid_current_state")
            continue
        thread_status[thread["id"]] = thread["status"]

    touched_threads: set[str] = set()
    touched_relations: set[tuple[str, str]] = set()
    time_advance_count = 0
    decision_count = 0
    simulation_count = 0
    for operation_index, operation in enumerate(proposal.state_operations):
        if isinstance(operation, RecordDecisionOperation):
            decision_count += 1
            if decision_count > 1:
                issues.append("contradictory_operations")
        elif isinstance(operation, OpenThreadOperation):
            if operation.thread_id in touched_threads:
                issues.append("contradictory_operations")
            touched_threads.add(operation.thread_id)
            if operation.thread_id in thread_status:
                issues.append("contradictory_operations")
            else:
                thread_status[operation.thread_id] = "open"
        elif isinstance(operation, ResolveThreadOperation):
            if operation.thread_id in touched_threads:
                issues.append("contradictory_operations")
            touched_threads.add(operation.thread_id)
            if operation.thread_id not in thread_status:
                issues.append("unknown_thread_id")
            elif thread_status[operation.thread_id] != "open":
                issues.append("thread_not_open")
            else:
                thread_status[operation.thread_id] = "resolved"
        elif isinstance(operation, RecordSimulationEventOperation):
            simulation_count += 1
            if simulation_count > 8:
                issues.append("proposal_too_large")
            for actor_id in operation.actor_ids:
                check_actor(actor_id)
        elif isinstance(operation, SetRelationOperation):
            check_actor(operation.actor_a_id)
            check_actor(operation.actor_b_id)
            pair = tuple(sorted((operation.actor_a_id, operation.actor_b_id)))
            if pair in touched_relations:
                issues.append("contradictory_operations")
            touched_relations.add(pair)
            relations = current_state.get("relations", [])
            if not isinstance(relations, list):
                issues.append("invalid_current_state")
            else:
                for relation in relations:
                    if (
                        not isinstance(relation, dict)
                        or not isinstance(relation.get("actor_a_id"), str)
                        or not isinstance(relation.get("actor_b_id"), str)
                        or relation.get("relation")
                        not in {"cooperative", "strained", "contested", "unknown"}
                        or not isinstance(relation.get("reason"), str)
                    ):
                        issues.append("invalid_current_state")
                        continue
                    existing_pair = tuple(
                        sorted((relation.get("actor_a_id"), relation.get("actor_b_id")))
                    )
                    if existing_pair == pair and relation.get("relation") == operation.relation:
                        issues.append("contradictory_operations")
        elif isinstance(operation, AdvanceWorldTimeOperation):
            time_advance_count += 1
            if time_advance_count > 1:
                issues.append("contradictory_operations")
            if operation_index != len(proposal.state_operations) - 1:
                issues.append("contradictory_operations")
            if current_date is None:
                continue
            target = date.fromisoformat(operation.target_date)
            period_end = definition.metadata.historical_period.end
            max_days = definition.chronology.max_days_per_advance
            if (
                not current_date < target <= period_end
                or (target - current_date).days > max_days
            ):
                issues.append("invalid_time_advance")

    if list(thread_status.values()).count("open") > 100:
        issues.append("proposal_too_large")
    if issues:
        raise ProposalValidationError(issues)

    assert current_date is not None
    assert role is not None
    event_date = context.world_date.isoformat()
    events: list[StateEvent] = []
    events.extend(
        HistoricalClaimRecordedEvent(
            event_type="historical_claim_recorded",
            **claim.model_dump(),
            world_date=event_date,
            actor_id=role.actor_id,
        )
        for claim in proposal.historical_claims
    )
    events.extend(
        AuthorContextReferencedEvent(
            event_type="author_context_referenced",
            **reference.model_dump(),
            world_date=event_date,
            actor_id=role.actor_id,
        )
        for reference in proposal.author_context_references
    )
    events.extend(
        PlayerClaimRecordedEvent(
            event_type="player_claim_recorded",
            **claim.model_dump(),
            world_date=event_date,
            actor_id=role.actor_id,
        )
        for claim in proposal.player_claims
    )
    events.extend(
        LearningObservationRecordedEvent(
            event_type="learning_observation_recorded",
            **observation.model_dump(),
            world_date=event_date,
            actor_id=role.actor_id,
        )
        for observation in proposal.learning_observations
    )
    events.extend(
        _operation_event(operation, current_date, player_action)
        for operation in proposal.state_operations
    )
    if proposal.scene_status == "complete":
        events.append(SceneCompletedEvent(event_type="scene_completed"))
    events = tuple(events)
    try:
        project_state(current_state, events)
    except StateProjectionError as exc:
        raise ProposalValidationError(["operation_conflict"]) from exc
    return ValidatedTurnProposal(proposal=proposal, events=events)
