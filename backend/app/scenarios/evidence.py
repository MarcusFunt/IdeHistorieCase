"""Fail-closed date and role policy for scenario evidence sidecars.

This module controls source eligibility only. It never treats an eligible link as a
verified passage or citation.
"""
from datetime import date
from typing import Any, Literal

EvidenceMode = Literal["gameplay", "postgame", "evaluator"]
VALID_MODES = {"gameplay", "postgame", "evaluator"}
VALID_SCOPES = frozenset({
    "archival_authentication_only",
    "delegation_submission",
    "confidential_council_record",
    "confidential_council_record_selected_by_document",
    "final_treaty_or_postgame",
    "later_chronology_only",
    "later_interpretation_only",
    "lead_full_text_unverified",
    "link_only_page_access_and_rights_pending",
    "link_only_page_and_rights_review_pending",
    "link_only_selected_pages_unreviewed",
    "link_only_transcript_and_rights_pending",
    "period_statement_paraphrase_only",
    "plenary_record_after_meeting",
    "postgame_only",
    "restricted_minutes_and_distinct_submitted_appendices",
    "secondary_context_only",
    "written_exchange_after_draft_delivery",
})


def _available(entry: dict[str, Any], world_date: date, actor_id: str | None, mode: str) -> bool:
    if mode not in VALID_MODES:
        return False
    availability = entry.get("in_world_available_from")
    scope = entry.get("scope")
    actor_ids = entry.get("actor_ids")
    if (
        not isinstance(scope, str)
        or scope not in VALID_SCOPES
        or not isinstance(actor_ids, list)
        or any(not isinstance(actor, str) or not actor for actor in actor_ids)
    ):
        return False
    if availability == "blocked":
        return False
    if availability == "postgame":
        if mode == "gameplay":
            return False
    elif isinstance(availability, str):
        try:
            available_date = date.fromisoformat(availability)
        except ValueError:
            return False
        if mode == "gameplay" and available_date > world_date:
            return False
    else:
        return False

    if scope.startswith("confidential") or scope.startswith("restricted"):
        if mode != "evaluator" and actor_id not in actor_ids:
            return False
    elif mode == "gameplay" and actor_id not in actor_ids:
        return False
    return True


def available_source_ids(
    policy: dict[str, Any],
    world_date: date,
    *,
    actor_id: str | None,
    mode: EvidenceMode,
) -> set[str]:
    """Return eligible source IDs; malformed or blocked policy entries fail closed."""
    if mode not in VALID_MODES or not isinstance(policy, dict):
        return set()
    entries = policy.get("source_access")
    if not isinstance(entries, list):
        return set()
    return {
        entry["source_id"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("source_id"), str)
        and _available(entry, world_date, actor_id, mode)
    }


def citation_source_ids(policy: dict[str, Any], available_ids: set[str]) -> set[str]:
    """Only return IDs when the policy explicitly certifies citation readiness.

    A source link or temporal eligibility alone does not make a citation ready; B3
    must add approved passage records and a reviewed claim-to-passage binding.
    """
    if not isinstance(policy, dict) or policy.get("citation_ready") is not True:
        return set()
    entries = policy.get("source_access")
    if not isinstance(entries, list):
        return set()
    ready = {
        entry.get("source_id")
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("source_id"), str)
        and entry.get("citation_ready") is True
    }
    return available_ids & ready
