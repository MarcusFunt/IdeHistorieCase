"""Safe YAML loading, canonical content hashing and manifest discovery."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken

from app.scenarios.bundle import scenario_bundle_hash
from app.scenarios.evidence import VALID_SCOPES
from app.scenarios.schema import ScenarioDefinition
from app.scenarios.validation import validate_definition

MAX_MANIFEST_BYTES = 1024 * 1024


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueSafeLoader, node: MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "unhashable mapping key", key_node.start_mark
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True)
class LoadedScenario:
    definition: ScenarioDefinition
    initial_state: dict[str, Any]
    content_hash: str
    path: Path | None = None
    bundle_hash: str | None = None
    behavior_rules: dict[str, Any] | None = None
    evidence_policy: dict[str, Any] | None = None


def initial_state_for(definition: ScenarioDefinition) -> dict[str, Any]:
    metrics = {}
    flags = {}
    for variable in definition.state_variables:
        if variable.kind == "integer_metric":
            metrics[variable.id] = variable.initial
        else:
            flags[variable.id] = variable.initial
    phase = next(item.id for item in definition.phases if item.is_start)
    return {
        "phase": phase,
        "metrics": metrics,
        "flags": flags,
        "decisions": {},
        "skipped_issues": [],
        "visited_phases": [phase],
    }


def canonical_hash(definition: ScenarioDefinition) -> str:
    content = json.dumps(
        definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validation_message(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '$'}: {item['msg']}"
        for item in error.errors()
    )


def load_scenario_text(text: str, path: Path | None = None) -> LoadedScenario:
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError(f"scenario manifest exceeds maximum size of {MAX_MANIFEST_BYTES} bytes")
    try:
        for token in yaml.scan(text, Loader=_UniqueSafeLoader):
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ValueError("YAML anchors and aliases are not supported")
        documents = list(yaml.load_all(text, Loader=_UniqueSafeLoader))
    except ValueError:
        raise
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc)
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"invalid YAML{location}: {problem}") from exc
    if len(documents) != 1:
        raise ValueError("scenario manifest must contain exactly one YAML document")
    if not isinstance(documents[0], dict):
        raise ValueError("scenario manifest top level must be a mapping")
    try:
        definition = ScenarioDefinition.model_validate(documents[0])
    except ValidationError as exc:
        raise ValueError(f"invalid scenario: {_validation_message(exc)}") from exc
    validate_definition(definition)
    return LoadedScenario(definition, initial_state_for(definition), canonical_hash(definition), path)


def _load_sidecar(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read scenario sidecar {path}: {exc}") from exc
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError(f"scenario sidecar exceeds maximum size of {MAX_MANIFEST_BYTES} bytes")
    try:
        text = content.decode("utf-8-sig")
        for token in yaml.scan(text, Loader=_UniqueSafeLoader):
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ValueError("YAML anchors and aliases are not supported in scenario sidecars")
        documents = list(yaml.load_all(text, Loader=_UniqueSafeLoader))
    except ValueError:
        raise
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc)
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"invalid YAML sidecar{location}: {problem}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("scenario sidecar must contain one YAML mapping")
    return documents[0]


def _validate_sidecar_identity(sidecar: dict[str, Any], definition: ScenarioDefinition) -> None:
    policy_version = sidecar.get("policy_version")
    if (
        sidecar.get("scenario_slug") != definition.slug
        or str(sidecar.get("scenario_version")) != definition.version
        or type(policy_version) is not int
        or policy_version < 1
    ):
        raise ValueError("scenario sidecar identity must match its manifest slug and version")


def _string_list(value: Any, location: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        qualifier = "nonempty " if nonempty else ""
        raise ValueError(f"{location} must be a {qualifier}list of nonempty strings")
    return value


def _load_behavior_rules(path: Path, definition: ScenarioDefinition) -> dict[str, Any]:
    rules = _load_sidecar(path)
    _validate_sidecar_identity(rules, definition)
    if rules.get("reaction_policy") != "index_pairing_is_illustrative_not_historical":
        raise ValueError("behavior sidecar must declare its deterministic reaction policy")
    if not isinstance(rules.get("rules"), list):
        raise ValueError("behavior rules must be a list")
    phases = {phase.id for phase in definition.phases}
    sources = set(definition.source_refs)
    issue_ids: set[str] = set()
    for index, rule in enumerate(rules["rules"]):
        if not isinstance(rule, dict):
            raise ValueError(f"behavior rules[{index}] must be a mapping")
        issue_id = rule.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id or issue_id in issue_ids:
            raise ValueError(f"behavior rules[{index}].issue_id must be unique and nonempty")
        issue_ids.add(issue_id)
        phase_id = rule.get("phase_id")
        if not isinstance(phase_id, str) or phase_id not in phases:
            raise ValueError(f"behavior rules[{index}].phase_id references an unknown phase")
        decisions = _string_list(
            rule.get("decision_codes"), f"behavior rules[{index}].decision_codes", nonempty=True
        )
        objections = _string_list(
            rule.get("objection_codes"), f"behavior rules[{index}].objection_codes", nonempty=True
        )
        if len(decisions) != len(set(decisions)):
            raise ValueError(f"behavior rules[{index}].decision_codes must be unique")
        if len(objections) != len(set(objections)):
            raise ValueError(f"behavior rules[{index}].objection_codes must be unique")
        evidence_refs = _string_list(
            rule.get("evidence_refs", []), f"behavior rules[{index}].evidence_refs"
        )
        if not set(evidence_refs) <= sources:
            raise ValueError(f"behavior rules[{index}].evidence_refs references an unknown source")
    terminal_policy = rules.get("terminal_policy")
    if terminal_policy is not None:
        if not isinstance(terminal_policy, dict):
            raise ValueError("behavior terminal_policy must be a mapping")
        policy_phase = terminal_policy.get("phase_id")
        target_phase = terminal_policy.get("target_phase_id")
        outcome_issue = terminal_policy.get("issue_id")
        deadlock_code = terminal_policy.get("deadlock_decision_code")
        agreement_flag = terminal_policy.get("agreement_flag_id")
        deadlock_flag = terminal_policy.get("deadlock_flag_id")
        phase_by_id = {phase.id: phase for phase in definition.phases}
        issue_rule = next((item for item in rules["rules"] if item["issue_id"] == outcome_issue), None)
        flag_by_id = {item.id: item for item in definition.state_variables}
        if (
            not isinstance(policy_phase, str)
            or not isinstance(target_phase, str)
            or not isinstance(outcome_issue, str)
            or not isinstance(deadlock_code, str)
            or not isinstance(agreement_flag, str)
            or not isinstance(deadlock_flag, str)
            or policy_phase not in phase_by_id
            or target_phase not in phase_by_id
            or not phase_by_id[target_phase].is_terminal
            or target_phase not in phase_by_id[policy_phase].allowed_next_phase_ids
            or issue_rule is None
            or issue_rule["phase_id"] != policy_phase
            or deadlock_code not in issue_rule["decision_codes"]
            or agreement_flag == deadlock_flag
            or agreement_flag not in flag_by_id
            or deadlock_flag not in flag_by_id
            or flag_by_id[agreement_flag].kind != "boolean_flag"
            or flag_by_id[deadlock_flag].kind != "boolean_flag"
        ):
            raise ValueError("behavior terminal_policy must reference a valid final issue, phases, and flags")
        permitted_outcome_flags = {
            permission.target_id
            for permission in definition.allowed_events
            if permission.type == "set_flag"
        }
        if not {agreement_flag, deadlock_flag} <= permitted_outcome_flags:
            raise ValueError("behavior terminal_policy must have set_flag permissions for both outcomes")
        if not any(permission.type == "advance_phase" for permission in definition.allowed_events):
            raise ValueError("behavior terminal_policy requires an advance_phase permission")
        if flag_by_id[agreement_flag].initial is not False or flag_by_id[deadlock_flag].initial is not False:
            raise ValueError("behavior terminal outcome flags must be initially false")
    return rules


def _load_evidence_policy(path: Path, definition: ScenarioDefinition) -> dict[str, Any]:
    policy = _load_sidecar(path)
    _validate_sidecar_identity(policy, definition)
    if not isinstance(policy.get("citation_ready"), bool):
        raise ValueError("evidence policy citation_ready must be a boolean")
    entries = policy.get("source_access")
    if not isinstance(entries, list):
        raise ValueError("evidence policy source_access must be a list")
    source_ids = set(definition.source_refs)
    actor_ids = {actor.id for actor in definition.actors}
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"source_access[{index}] must be a mapping")
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or source_id not in source_ids or source_id in seen:
            raise ValueError(f"source_access[{index}].source_id is unknown or duplicated")
        seen.add(source_id)
        availability = entry.get("in_world_available_from")
        if not isinstance(availability, str):
            raise ValueError(f"source_access[{index}].in_world_available_from must be a string")
        if availability not in {"blocked", "postgame"}:
            try:
                date.fromisoformat(availability)
            except ValueError as exc:
                raise ValueError(
                    f"source_access[{index}].in_world_available_from must be blocked, postgame, or an ISO date"
                ) from exc
        scope = entry.get("scope")
        if not isinstance(scope, str) or scope not in VALID_SCOPES:
            raise ValueError(f"source_access[{index}].scope is unknown")
        entry_actors = _string_list(
            entry.get("actor_ids", []), f"source_access[{index}].actor_ids"
        )
        if not set(entry_actors) <= actor_ids:
            raise ValueError(f"source_access[{index}].actor_ids contains an unknown actor")
        if not isinstance(entry.get("citation_ready"), bool):
            raise ValueError(f"source_access[{index}].citation_ready must be a boolean")
    if seen != source_ids:
        raise ValueError("evidence policy must include every manifest source reference")
    return policy

def load_scenario(path: str | Path) -> LoadedScenario:
    manifest_path = Path(path)
    try:
        content = manifest_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read scenario manifest {manifest_path}: {exc}") from exc
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError(f"scenario manifest exceeds maximum size of {MAX_MANIFEST_BYTES} bytes")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"scenario manifest must be UTF-8: {manifest_path}") from exc
    loaded = load_scenario_text(text, manifest_path)
    behavior_path = manifest_path.with_name(f"{manifest_path.stem}_behaviors.yaml")
    evidence_path = manifest_path.with_name(f"{manifest_path.stem}_evidence.yaml")
    behavior_rules = (
        _load_behavior_rules(behavior_path, loaded.definition) if behavior_path.exists() else None
    )
    evidence_policy = (
        _load_evidence_policy(evidence_path, loaded.definition) if evidence_path.exists() else None
    )
    return LoadedScenario(
        loaded.definition,
        loaded.initial_state,
        loaded.content_hash,
        manifest_path,
        scenario_bundle_hash(manifest_path, content),
        behavior_rules,
        evidence_policy,
    )

def manifest_paths(root: str | Path) -> list[Path]:
    directory = Path(root)
    if not directory.is_dir():
        raise ValueError(f"scenario directory does not exist: {directory}")
    excluded = {"sources", "tests"}
    return sorted(
        path for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".yaml", ".yml"}
        and not (excluded & set(path.relative_to(directory).parts))
        and not path.stem.endswith(("_evidence", "_behaviors", "_retrieval_goldens", "_history_goldens"))
    )


def validate_all(root: str | Path) -> list[LoadedScenario]:
    paths = manifest_paths(root)
    if not paths:
        raise ValueError(f"no scenario manifests found under {root}")
    loaded = []
    for path in paths:
        try:
            loaded.append(load_scenario(path))
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
    return loaded
