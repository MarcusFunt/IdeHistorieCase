"""Typed and bounded runtime effects for authored scenario turns."""
from copy import deepcopy
from typing import Annotated, Literal, Union

from pydantic import Field, StrictInt, TypeAdapter

from app.scenarios.schema import ScenarioDefinition, StrictModel


class AdjustMetricProposal(StrictModel):
    type: Literal["adjust_metric"]
    target_id: str
    delta: StrictInt


class SetFlagProposal(StrictModel):
    type: Literal["set_flag"]
    target_id: str


class AdvancePhaseProposal(StrictModel):
    type: Literal["advance_phase"]
    target_phase_id: str


class IssueDecisionProposal(StrictModel):
    type: Literal["issue_decision"]
    issue_id: str
    decision_code: str


ScenarioEventProposal = Annotated[
    Union[
        AdjustMetricProposal,
        SetFlagProposal,
        AdvancePhaseProposal,
        IssueDecisionProposal,
    ],
    Field(discriminator="type"),
]
_PROPOSAL_ADAPTER = TypeAdapter(ScenarioEventProposal)


def _validate_terminal_outcome(
    state: dict,
    terminal_policy: dict,
    *,
    require_outcome: bool,
) -> None:
    issue_id = terminal_policy["issue_id"]
    decision_code = state.get("decisions", {}).get(issue_id)
    agreement_flag = terminal_policy["agreement_flag_id"]
    deadlock_flag = terminal_policy["deadlock_flag_id"]
    agreement = state.get("flags", {}).get(agreement_flag) is True
    deadlock = state.get("flags", {}).get(deadlock_flag) is True
    if decision_code is None and require_outcome:
        raise ValueError(f"terminal outcome requires decision for {issue_id}")
    if decision_code is None:
        return
    is_deadlock_decision = decision_code == terminal_policy["deadlock_decision_code"]
    if is_deadlock_decision and (not deadlock or agreement):
        raise ValueError("deadlock decision requires only the deadlock outcome flag")
    if not is_deadlock_decision and (not agreement or deadlock):
        raise ValueError("agreement decision requires only the agreement outcome flag")


def apply_event_proposals(
    definition: ScenarioDefinition,
    state: dict,
    proposals: list[ScenarioEventProposal | dict],
    *,
    behavior_rules: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Validate and apply the complete proposal atomically to a copied state."""
    if len(proposals) > 32:
        raise ValueError("a turn may propose at most 32 events")
    variables = {variable.id: variable for variable in definition.state_variables}
    permissions = [permission.model_dump(mode="json") for permission in definition.allowed_events]
    phases = {phase.id: phase for phase in definition.phases}
    rules = (behavior_rules or {}).get("rules", [])
    terminal_policy = (behavior_rules or {}).get("terminal_policy")
    result = deepcopy(state)
    result.setdefault("decisions", {})
    result.setdefault("skipped_issues", [])
    result.setdefault("visited_phases", [result.get("phase")])
    if (
        not isinstance(result["decisions"], dict)
        or not isinstance(result["skipped_issues"], list)
        or not isinstance(result["visited_phases"], list)
    ):
        raise ValueError("runtime issue state is malformed")
    accepted: list[dict] = []
    phase_advances = 0
    decisions_this_turn: set[str] = set()

    for raw_proposal in proposals:
        proposal = _PROPOSAL_ADAPTER.validate_python(raw_proposal)
        if isinstance(proposal, AdjustMetricProposal):
            variable = variables.get(proposal.target_id)
            if variable is None or variable.kind != "integer_metric":
                raise ValueError(f"metric target is not authorized: {proposal.target_id}")
            permission = next(
                (
                    item for item in permissions
                    if item["type"] == "adjust_metric" and item["target_id"] == proposal.target_id
                ),
                None,
            )
            if permission is None or not permission["min_delta"] <= proposal.delta <= permission["max_delta"]:
                raise ValueError(f"metric delta is not within the permitted delta for {proposal.target_id}")
            current = result.get("metrics", {}).get(proposal.target_id)
            if not isinstance(current, int) or isinstance(current, bool):
                raise ValueError(f"runtime metric is missing: {proposal.target_id}")
            value = current + proposal.delta
            if not variable.min_value <= value <= variable.max_value:
                raise ValueError(f"metric value would exceed its state range: {proposal.target_id}")
            result.setdefault("metrics", {})[proposal.target_id] = value
            accepted.append(
                {
                    "type": "adjust_metric",
                    "target_id": proposal.target_id,
                    "delta": proposal.delta,
                    "value": value,
                }
            )
        elif isinstance(proposal, SetFlagProposal):
            variable = variables.get(proposal.target_id)
            permitted = any(
                item["type"] == "set_flag" and item["target_id"] == proposal.target_id
                for item in permissions
            )
            if variable is None or variable.kind != "boolean_flag" or not permitted:
                raise ValueError(f"flag target is not authorized: {proposal.target_id}")
            if not isinstance(result.get("flags", {}).get(proposal.target_id), bool):
                raise ValueError(f"runtime flag is missing: {proposal.target_id}")
            if terminal_policy is not None:
                agreement_flag = terminal_policy["agreement_flag_id"]
                deadlock_flag = terminal_policy["deadlock_flag_id"]
                if proposal.target_id in {agreement_flag, deadlock_flag}:
                    if result.get("phase") != terminal_policy["phase_id"]:
                        raise ValueError("settlement outcome flags are only available in the final phase")
                    decision_code = result.get("decisions", {}).get(terminal_policy["issue_id"])
                    if decision_code is None:
                        raise ValueError("settlement outcome requires a final-package decision")
                    is_deadlock_decision = (
                        decision_code == terminal_policy["deadlock_decision_code"]
                    )
                    if (
                        proposal.target_id == agreement_flag and is_deadlock_decision
                    ) or (
                        proposal.target_id == deadlock_flag and not is_deadlock_decision
                    ):
                        raise ValueError("settlement decision conflicts with the selected outcome flag")
                    other_flag = deadlock_flag if proposal.target_id == agreement_flag else agreement_flag
                    if result["flags"].get(other_flag) is True:
                        raise ValueError("agreement and deadlock outcome flags are mutually exclusive")
            result.setdefault("flags", {})[proposal.target_id] = True
            accepted.append({"type": "set_flag", "target_id": proposal.target_id, "value": True})
        elif isinstance(proposal, IssueDecisionProposal):
            rule = next(
                (item for item in rules if item.get("issue_id") == proposal.issue_id),
                None,
            )
            if rule is None or proposal.decision_code not in rule.get("decision_codes", []):
                raise ValueError(
                    f"issue decision is not authorized: {proposal.issue_id}/{proposal.decision_code}"
                )
            if result.get("phase") != rule.get("phase_id"):
                raise ValueError(
                    f"issue {proposal.issue_id} is not available in phase {result.get('phase')}"
                )
            if terminal_policy is not None and proposal.issue_id == terminal_policy["issue_id"]:
                outcome_flags = (
                    terminal_policy["agreement_flag_id"],
                    terminal_policy["deadlock_flag_id"],
                )
                outcome_already_set = any(
                    result.get("flags", {}).get(flag_id) is True for flag_id in outcome_flags
                )
                previous_decision = result["decisions"].get(proposal.issue_id)
                if outcome_already_set and previous_decision != proposal.decision_code:
                    raise ValueError("final-package decision cannot change after an outcome flag is set")
            if proposal.issue_id in decisions_this_turn:
                raise ValueError(f"issue decision is duplicated in one turn: {proposal.issue_id}")
            objections = rule.get("objection_codes", [])
            if not objections:
                raise ValueError(f"issue {proposal.issue_id} has no authored reaction rule")
            decision_index = rule["decision_codes"].index(proposal.decision_code)
            objection_code = objections[decision_index % len(objections)]
            result["decisions"][proposal.issue_id] = proposal.decision_code
            decisions_this_turn.add(proposal.issue_id)
            result["skipped_issues"] = [
                issue_id for issue_id in result["skipped_issues"] if issue_id != proposal.issue_id
            ]
            accepted.append(
                {
                    "type": "issue_decision",
                    "issue_id": proposal.issue_id,
                    "decision_code": proposal.decision_code,
                    "simulated_objection_code": objection_code,
                    "reaction": (
                        "Et tænkt modhensyn i denne øvelse er: "
                        + objection_code.replace("_", " ")
                        + "."
                    ),
                    "qualitative_only": True,
                }
            )
        else:
            if not any(item["type"] == "advance_phase" for item in permissions):
                raise ValueError("phase advance is not authorized")
            phase_advances += 1
            if phase_advances > 1:
                raise ValueError("only one phase advance is allowed per turn")
            current_phase = phases.get(result.get("phase"))
            if current_phase is None or proposal.target_phase_id not in current_phase.allowed_next_phase_ids:
                raise ValueError(
                    f"phase transition is not allowed from phase {result.get('phase')}"
                )
            final_transition = (
                terminal_policy is not None
                and current_phase.id == terminal_policy["phase_id"]
                and proposal.target_phase_id == terminal_policy["target_phase_id"]
            )
            if final_transition:
                _validate_terminal_outcome(result, terminal_policy, require_outcome=True)
            skipped_now = [
                rule["issue_id"]
                for rule in rules
                if rule.get("phase_id") == current_phase.id
                and rule.get("issue_id") not in result["decisions"]
            ]
            for issue_id in skipped_now:
                if issue_id not in result["skipped_issues"]:
                    result["skipped_issues"].append(issue_id)
            if final_transition:
                for rule in rules:
                    issue_id = rule.get("issue_id")
                    if issue_id not in result["decisions"] and issue_id not in result["skipped_issues"]:
                        result["skipped_issues"].append(issue_id)
            result["phase"] = proposal.target_phase_id
            result["visited_phases"].append(proposal.target_phase_id)
            accepted.append(
                {
                    "type": "advance_phase",
                    "from_phase_id": current_phase.id,
                    "to_phase_id": proposal.target_phase_id,
                }
            )
    if terminal_policy is not None:
        outcome_flags = (
            terminal_policy["agreement_flag_id"],
            terminal_policy["deadlock_flag_id"],
        )
        if any(result.get("flags", {}).get(flag_id) is True for flag_id in outcome_flags):
            _validate_terminal_outcome(result, terminal_policy, require_outcome=True)
    return result, accepted
