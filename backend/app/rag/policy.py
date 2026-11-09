"""Fail-closed source, temporal, actor and rights eligibility for RAG passages."""
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from typing import Literal
from uuid import UUID

from app.scenarios.evidence import VALID_MODES, available_source_ids

EvidenceMode = Literal["gameplay", "postgame", "evaluator"]


@dataclass(frozen=True)
class EvidenceContext:
    scenario_version_id: UUID
    scenario_slug: str
    scenario_version: str
    actor_id: str | None
    world_date: date
    mode: EvidenceMode = "gameplay"
    evidence_policy: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EvidencePassage:
    id: UUID
    scenario_version_id: UUID
    scenario_slug: str
    scenario_version: str
    passage_key: str
    source_identifier: str
    source_title: str
    source_url: str
    source_anchor: str
    text: str
    passage_sha256: str
    document_date: date
    in_world_available_from: date | None
    postgame_only: bool
    actor_ids: tuple[str, ...]
    visibility_scope: str


@dataclass(frozen=True)
class PassageApproval:
    review_status: str
    rights_status: str
    revision: int


def _source_entry(source_policy: dict, source_identifier: str) -> dict | None:
    entries = source_policy.get("source_access") if isinstance(source_policy, dict) else None
    if not isinstance(entries, list):
        return None
    return next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("source_id") == source_identifier
        ),
        None,
    )


def is_eligible(
    passage: EvidencePassage,
    context: EvidenceContext,
    decision: PassageApproval | None,
    source_policy: dict,
) -> bool:
    """Require exact scenario provenance, current approval and date/actor access."""
    if context.mode not in VALID_MODES:
        return False
    if sha256(passage.text.encode("utf-8")).hexdigest() != passage.passage_sha256:
        return False
    if (
        passage.scenario_version_id != context.scenario_version_id
        or passage.scenario_slug != context.scenario_slug
        or passage.scenario_version != context.scenario_version
        or decision is None
        or decision.review_status != "approved"
        or decision.rights_status != "approved"
    ):
        return False

    available = available_source_ids(
        source_policy,
        context.world_date,
        actor_id=context.actor_id,
        mode=context.mode,
    )
    if passage.source_identifier not in available:
        return False

    entry = _source_entry(source_policy, passage.source_identifier)
    if entry is None:
        return False
    source_scope = entry.get("scope")
    source_is_restricted = isinstance(source_scope, str) and source_scope.startswith(
        ("confidential", "restricted")
    )
    if source_is_restricted != (passage.visibility_scope == "confidential"):
        return False

    if context.mode == "postgame" and passage.source_identifier in available:
        policy_actor_ids = entry.get("actor_ids")
        public_postgame = (
            passage.postgame_only
            and passage.visibility_scope == "postgame"
            and not passage.actor_ids
            and policy_actor_ids == []
        )
        if not public_postgame and (
            context.actor_id is None
            or not isinstance(policy_actor_ids, list)
            or context.actor_id not in policy_actor_ids
        ):
            return False

    source_availability = entry.get("in_world_available_from")
    if source_availability == "postgame":
        if not passage.postgame_only or passage.in_world_available_from is not None:
            return False
    elif source_availability == "blocked" or not isinstance(source_availability, str):
        return False
    else:
        try:
            policy_date = date.fromisoformat(source_availability)
        except ValueError:
            return False
        if (
            passage.postgame_only
            or passage.in_world_available_from is None
            or passage.in_world_available_from < policy_date
        ):
            return False

    if passage.postgame_only:
        if context.mode == "gameplay" or passage.visibility_scope != "postgame":
            return False
        if context.actor_id is None:
            return not passage.actor_ids and entry.get("actor_ids") == []
        if context.actor_id in passage.actor_ids:
            return True
        # A public postgame-only source may be used in a role-aware reflection
        # even though its passage and policy have no actor assignment.
        return not passage.actor_ids and entry.get("actor_ids") == []

    if context.mode == "gameplay":
        if passage.document_date > context.world_date:
            return False
        if (
            passage.in_world_available_from is None
            or passage.in_world_available_from > context.world_date
        ):
            return False
    if context.actor_id is None or context.actor_id not in passage.actor_ids:
        return False
    return True
