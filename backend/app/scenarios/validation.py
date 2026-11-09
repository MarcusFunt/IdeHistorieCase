"""Cross-reference and finite phase-graph checks for scenario manifests."""
from collections import deque

from app.scenarios.schema import (
    AdjustMetricPermission,
    BooleanFlagDefinition,
    ScenarioDefinition,
    SetFlagPermission,
)
from app.scenarios.schema_v2 import ScenarioDefinitionV2


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


def validate_v2_definition(definition: ScenarioDefinitionV2) -> None:
    """Validate v2 references and date bounds without imposing a phase graph."""
    namespaces = {
        "actor": [item.id for item in definition.actors],
        "role": [item.id for item in definition.roles],
        "learning objective": [item.id for item in definition.learning_objectives],
        "content key": [item.key for item in definition.content_items],
        "optional beat": [item.id for item in definition.optional_beats],
    }
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    for namespace, values in namespaces.items():
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            errors.append(f"duplicate {namespace} id(s): {', '.join(duplicates)}")
        for value in values:
            if value in seen_ids:
                errors.append(f"id {value!r} is shared by {seen_ids[value]} and {namespace}")
            else:
                seen_ids[value] = namespace

    historical_period = definition.metadata.historical_period
    if historical_period.start > historical_period.end:
        errors.append("historical_period requires start <= end")
    if not historical_period.start <= definition.chronology.opening_date <= historical_period.end:
        errors.append("chronology.opening_date must fall within historical_period")

    actors = {actor.id for actor in definition.actors}
    roles = {role.id: role for role in definition.roles}
    objectives = {objective.id for objective in definition.learning_objectives}
    sources = set(definition.source_refs)
    if definition.default_role_id not in roles:
        errors.append(f"default role {definition.default_role_id!r} does not exist")
    elif not roles[definition.default_role_id].playable:
        errors.append(f"default role {definition.default_role_id!r} must be playable")

    for role in definition.roles:
        if role.actor_id not in actors:
            errors.append(f"role {role.id!r} references unknown actor {role.actor_id!r}")

    unknown_completion_objectives = sorted(
        set(definition.completion_policy.objective_ids) - objectives
    )
    if unknown_completion_objectives:
        errors.append(
            "completion_policy references unknown objective(s): "
            + ", ".join(unknown_completion_objectives)
        )

    for item in definition.content_items:
        unknown_roles = sorted(set(item.visibility) - roles.keys())
        if unknown_roles:
            errors.append(
                f"content item {item.key!r} references unknown role(s): {', '.join(unknown_roles)}"
            )
        unknown_sources = sorted(set(item.source_refs) - sources)
        if unknown_sources:
            errors.append(
                f"content item {item.key!r} references unknown source(s): {', '.join(unknown_sources)}"
            )
        if item.world_valid_from and item.world_valid_to and item.world_valid_from > item.world_valid_to:
            errors.append(f"content item {item.key!r} world_valid_from must be <= world_valid_to")
        for field_name in ("availability_date", "world_valid_from", "world_valid_to"):
            value = getattr(item, field_name)
            if value and not historical_period.start <= value <= historical_period.end:
                errors.append(
                    f"content item {item.key!r} {field_name} must fall within historical_period"
                )
        if (
            item.availability_date
            and item.world_valid_from
            and item.world_valid_from < item.availability_date
        ):
            errors.append(
                f"content item {item.key!r} world_valid_from cannot precede availability_date"
            )

    for beat in definition.optional_beats:
        unknown_objectives = sorted(set(beat.objective_ids) - objectives)
        if unknown_objectives:
            errors.append(
                f"optional beat {beat.id!r} references unknown objective(s): "
                + ", ".join(unknown_objectives)
            )

    if errors:
        raise ValueError("; ".join(errors))
