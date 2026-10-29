"""Cross-reference and finite phase-graph checks for scenario manifests."""
from collections import deque

from app.scenarios.schema import (
    AdjustMetricPermission,
    BooleanFlagDefinition,
    ScenarioDefinition,
    SetFlagPermission,
)


def validate_definition(definition: ScenarioDefinition) -> None:
    namespaces = {
        "actor": [item.id for item in definition.actors],
        "role": [item.id for item in definition.roles],
        "learning objective": [item.id for item in definition.learning_objectives],
        "state variable": [item.id for item in definition.state_variables],
        "phase": [item.id for item in definition.phases],
        "dilemma": [item.id for item in definition.dilemmas],
    }
    errors: list[str] = []
    all_ids: dict[str, str] = {}
    for namespace, values in namespaces.items():
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            errors.append(f"duplicate {namespace} id(s): {', '.join(duplicates)}")
        for value in values:
            if value in all_ids:
                errors.append(f"id {value!r} is shared by {all_ids[value]} and {namespace}")
            else:
                all_ids[value] = namespace

    actors = set(namespaces["actor"])
    roles = {item.id: item for item in definition.roles}
    objectives = set(namespaces["learning objective"])
    variables = {item.id: item for item in definition.state_variables}
    phases = {item.id: item for item in definition.phases}
    phase_ids = set(phases)
    if definition.metadata.historical_period.start_year > definition.metadata.historical_period.end_year:
        errors.append("historical_period requires start_year <= end_year")
    if definition.default_role_id not in roles:
        errors.append(f"default role {definition.default_role_id!r} does not exist")
    elif not roles[definition.default_role_id].playable:
        errors.append(f"default role {definition.default_role_id!r} must be playable")
    for role in definition.roles:
        if role.actor_id not in actors:
            errors.append(f"role {role.id!r} references unknown actor {role.actor_id!r}")

    starts = [phase.id for phase in definition.phases if phase.is_start]
    terminals = {phase.id for phase in definition.phases if phase.is_terminal}
    if len(starts) != 1:
        errors.append("phase graph requires exactly one start phase")
    if not terminals:
        errors.append("phase graph requires at least one terminal phase")
    for phase in definition.phases:
        if phase.is_terminal and phase.allowed_next_phase_ids:
            errors.append(f"terminal phase {phase.id!r} cannot have outgoing phases")
        for target in phase.allowed_next_phase_ids:
            if target not in phase_ids:
                errors.append(f"phase {phase.id!r} references unknown phase {target!r}")

    edges_valid = all(
        target in phase_ids
        for phase in definition.phases
        for target in phase.allowed_next_phase_ids
    )
    if len(starts) == 1 and edges_valid:
        graph = {phase.id: phase.allowed_next_phase_ids for phase in definition.phases}
        reached: set[str] = set()
        queue = deque([starts[0]])
        while queue:
            phase_id = queue.popleft()
            if phase_id in reached:
                continue
            reached.add(phase_id)
            queue.extend(graph[phase_id])
        unreachable = sorted(phase_ids - reached)
        if unreachable:
            errors.append(f"unreachable phase(s): {', '.join(unreachable)}")
        can_terminate = set(terminals)
        changed = True
        while changed:
            changed = False
            for phase_id, targets in graph.items():
                if phase_id not in can_terminate and any(target in can_terminate for target in targets):
                    can_terminate.add(phase_id)
                    changed = True
        dead_ends = sorted(phase_id for phase_id in phase_ids if phase_id not in can_terminate)
        if dead_ends:
            errors.append(f"phase(s) cannot reach a terminal phase: {', '.join(dead_ends)}")

    for dilemma in definition.dilemmas:
        if dilemma.phase_id not in phase_ids:
            errors.append(f"dilemma {dilemma.id!r} references unknown phase {dilemma.phase_id!r}")
        unknown_actors = sorted(set(dilemma.relevant_actor_ids) - actors)
        if unknown_actors:
            errors.append(f"dilemma {dilemma.id!r} references unknown actor(s): {', '.join(unknown_actors)}")
        unknown_objectives = sorted(set(dilemma.learning_objective_ids) - objectives)
        if unknown_objectives:
            errors.append(
                f"dilemma {dilemma.id!r} references unknown learning objective(s): {', '.join(unknown_objectives)}"
            )

    metrics = {
        variable_id for variable_id, variable in variables.items()
        if not isinstance(variable, BooleanFlagDefinition)
    }
    flags = {
        variable_id for variable_id, variable in variables.items()
        if isinstance(variable, BooleanFlagDefinition)
    }
    for event in definition.allowed_events:
        if isinstance(event, AdjustMetricPermission) and event.target_id not in metrics:
            errors.append(f"adjust_metric target {event.target_id!r} must reference an integer metric")
        if isinstance(event, SetFlagPermission) and event.target_id not in flags:
            errors.append(f"set_flag target {event.target_id!r} must reference a boolean flag")
    if errors:
        raise ValueError("; ".join(errors))
