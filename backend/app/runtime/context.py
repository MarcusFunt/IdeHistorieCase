"""Build bounded, release-pinned prompt context for schema-v2 sessions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GameSession, ScenarioContentItem, ScenarioVersion
from app.rag.policy import EvidenceContext, is_eligible
from app.rag.retrieval import RAG_NO_EVIDENCE, RagSearchService, SearchCandidate
from app.scenarios.evidence import available_source_ids, citation_source_ids
from app.scenarios.schema_v2 import ScenarioDefinitionV2
from app.scenarios.validation import validate_v2_definition

ContextStage = Literal["initialization", "turn"]
ContextMode = Literal["gameplay", "postgame"]
AUTHOR_CONTEXT_TRUST = "author_context_not_historical_evidence"
HISTORICAL_EVIDENCE_TRUST = "historical_evidence"
COUNTERFACTUAL_CONSTRAINT = (
    "Scenarieforløb og simulerede samtaler er kontrafaktiske. Skeln mellem simulation, "
    "forfatterkontekst og historisk belæg."
)
EVIDENCE_CONSTRAINT = (
    "Brug kun de medsendte historiske passager som kildebelæg. Opfind ikke kilder, citater "
    "eller kildehenvisninger. Hvis der ikke er godkendt belæg, skal du sige det tydeligt."
)
TEMPORAL_CONSTRAINT = (
    "Brug ikke oplysninger fra senere end den aktuelle verdensdato i spillet. Følg rollens "
    "vidensbegrænsninger og opfind ikke historiske fakta."
)


@dataclass(frozen=True)
class SessionPromptContext:
    """Serialized prompt payload plus explicit retrieval and budget metadata."""

    payload: dict
    world_date: date
    evidence_status: str
    serialized_chars: int
    truncated_note_keys: tuple[str, ...] = ()
    omitted_evidence_count: int = 0


def _serialized_chars(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parse_release(scenario: ScenarioVersion) -> ScenarioDefinitionV2:
    if scenario.schema_version != 2 or not isinstance(scenario.definition, dict):
        raise ValueError("session context requires a pinned schema-v2 scenario release")
    try:
        definition = ScenarioDefinitionV2.model_validate(scenario.definition)
        validate_v2_definition(definition)
    except Exception as exc:
        raise ValueError("pinned schema-v2 scenario definition is invalid") from exc
    if definition.slug != scenario.slug or definition.version != scenario.version:
        raise ValueError("pinned scenario identity does not match its stored definition")
    return definition


def _world_date(
    definition: ScenarioDefinitionV2,
    session: GameSession,
    stage: ContextStage,
) -> date:
    if stage == "initialization":
        current = definition.chronology.opening_date
    else:
        state = session.state if isinstance(session.state, dict) else {}
        value = state.get("world_date")
        if not isinstance(value, str):
            raise ValueError("turn context requires a persisted world_date")
        try:
            current = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("turn context requires a persisted world_date in ISO format") from exc
        if current.isoformat() != value:
            raise ValueError("turn context requires a persisted world_date in ISO format")

    period = definition.metadata.historical_period
    if not period.start <= current <= period.end:
        raise ValueError("world_date must fall within the scenario historical period")
    if current < definition.chronology.opening_date:
        raise ValueError("world_date cannot precede the release opening date")
    return current


def _assert_content_rows(
    scenario: ScenarioVersion,
    definition: ScenarioDefinitionV2,
    rows: list[ScenarioContentItem],
) -> dict[str, ScenarioContentItem]:
    expected = {item.key: item for item in definition.content_items}
    actual: dict[str, ScenarioContentItem] = {}
    for row in rows:
        if row.item_key in actual:
            raise ValueError("pinned scenario has duplicate SQL content item keys")
        actual[row.item_key] = row
    if set(actual) != set(expected):
        raise ValueError("pinned scenario SQL content rows do not match its manifest")

    for key, item in expected.items():
        row = actual[key]
        body_hash = sha256(row.body.encode("utf-8")).hexdigest()
        if not (
            row.scenario_version_id == scenario.id
            and row.item_key == item.key
            and row.kind == item.kind
            and row.language == definition.metadata.language
            and row.authoritative_scope == "author_context"
            and row.availability_date == item.availability_date
            and row.world_valid_from == item.world_valid_from
            and row.world_valid_to == item.world_valid_to
            and row.visibility == list(item.visibility)
            and row.mode == item.mode
            and row.review_status == item.review_status
            and row.source_refs == list(item.source_refs)
            and row.metadata_json == {"source_file": item.file}
            and row.sha256 == body_hash
        ):
            raise ValueError(f"pinned scenario SQL content item {key!r} does not match its manifest")
    return actual


def _eligible_author_notes(
    definition: ScenarioDefinitionV2,
    rows: dict[str, ScenarioContentItem],
    *,
    role_id: str,
    world_date: date,
    mode: ContextMode,
) -> list[dict]:
    notes = []
    for item in definition.content_items:
        row = rows[item.key]
        if (
            row.review_status != "author_reviewed"
            or row.mode != mode
            or (row.visibility and role_id not in row.visibility)
            or (row.availability_date is not None and row.availability_date > world_date)
            or (row.world_valid_from is not None and row.world_valid_from > world_date)
            or (row.world_valid_to is not None and row.world_valid_to < world_date)
        ):
            continue
        notes.append(
            {
                "item_key": row.item_key,
                "kind": row.kind,
                "body": row.body,
                "trust_category": AUTHOR_CONTEXT_TRUST,
                "source_refs_are_not_evidence": bool(row.source_refs),
            }
        )
    return sorted(notes, key=lambda note: note["item_key"])


def _actor_for_role(definition: ScenarioDefinitionV2, role_id: str):
    role = next((item for item in definition.roles if item.id == role_id), None)
    if role is None or not role.playable:
        raise ValueError("session player_role_id must identify a playable role in the pinned release")
    actor = next((item for item in definition.actors if item.id == role.actor_id), None)
    if actor is None:
        raise ValueError("selected role references an unknown actor in the pinned release")
    return role, actor


def _passage_payload(
    candidate: SearchCandidate,
    *,
    citation_ready: bool,
) -> dict:
    passage = candidate.passage
    return {
        "scenario_slug": passage.scenario_slug,
        "scenario_version": passage.scenario_version,
        "passage_key": passage.passage_key,
        "source_identifier": passage.source_identifier,
        "source_title": passage.source_title,
        "source_url": passage.source_url,
        "source_anchor": passage.source_anchor,
        "passage_text": passage.text,
        "passage_sha256": passage.passage_sha256,
        "document_date": passage.document_date.isoformat(),
        "in_world_available_from": (
            passage.in_world_available_from.isoformat()
            if passage.in_world_available_from
            else None
        ),
        "postgame_only": passage.postgame_only,
        "visibility_scope": passage.visibility_scope,
        "review": {
            "status": candidate.approval.review_status,
            "rights_status": candidate.approval.rights_status,
            "revision": candidate.approval.revision,
        },
        "citation_ready": citation_ready,
        "trust_category": HISTORICAL_EVIDENCE_TRUST,
    }


def _fit_context(
    core: dict,
    notes: list[dict],
    passages: list[dict],
    *,
    max_chars: int,
) -> tuple[dict, tuple[str, ...], int]:
    """Keep the protected core, then passages, then bounded optional author notes."""
    payload = {**core, "author_notes": [], "evidence": {"status": RAG_NO_EVIDENCE, "passages": []}}
    if _serialized_chars(payload) > max_chars:
        raise ValueError("max_chars is too small for protected scenario constraints")

    omitted_evidence = 0
    for passage in passages:
        proposed = [*payload["evidence"]["passages"], passage]
        status = "available"
        candidate = {**payload, "evidence": {"status": status, "passages": proposed}}
        if _serialized_chars(candidate) <= max_chars:
            payload = candidate
        else:
            omitted_evidence += 1
    if not payload["evidence"]["passages"]:
        payload["evidence"] = {"status": RAG_NO_EVIDENCE, "passages": []}

    truncated_keys: list[str] = []
    for index, note in enumerate(notes):
        proposed = {**payload, "author_notes": [*payload["author_notes"], note]}
        if _serialized_chars(proposed) <= max_chars:
            payload = proposed
            continue

        truncated_keys.append(note["item_key"])
        body = note["body"]
        marker = " …[afkortet]"
        low, high = 0, len(body)
        best = None
        while low <= high:
            middle = (low + high) // 2
            truncated = {**note, "body": body[:middle].rstrip() + marker}
            candidate = {
                **payload,
                "author_notes": [*payload["author_notes"], truncated],
            }
            if _serialized_chars(candidate) <= max_chars:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            payload = best
        truncated_keys.extend(item["item_key"] for item in notes[index + 1 :])
        break
    return payload, tuple(truncated_keys), omitted_evidence


async def build_session_context(
    db: AsyncSession,
    session: GameSession,
    *,
    stage: ContextStage,
    query: str,
    retrieval_service: RagSearchService,
    mode: ContextMode = "gameplay",
    top_k: int = 5,
    max_chars: int = 18_000,
) -> SessionPromptContext:
    """Build prompt context only from the release pinned by the game session."""
    if stage not in {"initialization", "turn"}:
        raise ValueError("stage must be initialization or turn")
    if mode not in {"gameplay", "postgame"}:
        raise ValueError("mode must be gameplay or postgame")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValueError("top_k must be between 1 and 10")

    scenario = await db.get(ScenarioVersion, session.scenario_version_id)
    if scenario is None:
        raise ValueError("session pinned scenario release was not found")
    definition = _parse_release(scenario)
    world_date = _world_date(definition, session, stage)
    role_id = session.player_role_id
    if not isinstance(role_id, str) or not role_id:
        raise ValueError("session player_role_id must identify a playable role in the pinned release")
    role, actor = _actor_for_role(definition, role_id)

    rows = (
        await db.execute(
            select(ScenarioContentItem)
            .where(ScenarioContentItem.scenario_version_id == scenario.id)
            .order_by(ScenarioContentItem.item_key)
        )
    ).scalars().all()
    content_rows = _assert_content_rows(scenario, definition, rows)
    author_notes = _eligible_author_notes(
        definition,
        content_rows,
        role_id=role_id,
        world_date=world_date,
        mode=mode,
    )

    evidence_context = EvidenceContext(
        scenario_version_id=scenario.id,
        scenario_slug=scenario.slug,
        scenario_version=scenario.version,
        actor_id=None if mode == "postgame" else actor.id,
        world_date=world_date,
        mode=mode,
        evidence_policy=scenario.evidence_policy or {},
    )
    eligible_sources = available_source_ids(
        evidence_context.evidence_policy,
        world_date,
        actor_id=evidence_context.actor_id,
        mode=mode,
    )
    retrieved: list[SearchCandidate] = []
    evidence_status = RAG_NO_EVIDENCE
    if eligible_sources and query.strip():
        result = await retrieval_service.search(query, evidence_context, top_k=top_k)
        for candidate in result.passages:
            if is_eligible(
                candidate.passage,
                evidence_context,
                candidate.approval,
                evidence_context.evidence_policy,
            ):
                retrieved.append(candidate)
        if retrieved:
            evidence_status = "available"

    citation_ids = citation_source_ids(evidence_context.evidence_policy, eligible_sources)
    evidence_passages = [
        _passage_payload(candidate, citation_ready=candidate.passage.source_identifier in citation_ids)
        for candidate in retrieved
    ]

    core = {
        "pinned_scenario": {
            "slug": scenario.slug,
            "version": scenario.version,
            "schema_version": 2,
            "release_id": str(scenario.id),
        },
        "world_date": world_date.isoformat(),
        "selected_role": {
            "id": role.id,
            "title": role.title,
            "actor_id": actor.id,
            "actor_name": actor.name,
            "introduction": role.introduction,
            "motivations": actor.motivations,
            "knowledge_constraints": [*actor.knowledge_constraints, *role.knowledge_constraints],
        },
        "historical_context": {
            "introduction": definition.historical_context.introduction,
            "counterfactual_disclaimer": (
                definition.historical_context.counterfactual_disclaimer
                + " Scenarieforløbet er kontrafaktisk."
            ),
        },
        "chronology": {
            "historical_period": {
                "start": definition.metadata.historical_period.start.isoformat(),
                "end": definition.metadata.historical_period.end.isoformat(),
            },
            "opening_date": definition.chronology.opening_date.isoformat(),
            "current_world_date": world_date.isoformat(),
            "max_days_per_advance": definition.chronology.max_days_per_advance,
        },
        "learning_objectives": [
            {
                "id": item.id,
                "description": item.description,
                "concepts": item.concepts,
                "observable_behaviors": item.observable_behaviors,
            }
            for item in definition.learning_objectives
        ],
        "completion_policy": definition.completion_policy.model_dump(mode="json"),
        "optional_beats": [item.model_dump(mode="json") for item in definition.optional_beats],
        "interaction_guidance": definition.interaction_guidance.model_dump(mode="json"),
        "constraints": [COUNTERFACTUAL_CONSTRAINT, EVIDENCE_CONSTRAINT, TEMPORAL_CONSTRAINT],
    }
    payload, truncated_keys, omitted_evidence = _fit_context(
        core,
        author_notes,
        evidence_passages,
        max_chars=max_chars,
    )
    if not payload["evidence"]["passages"]:
        evidence_status = RAG_NO_EVIDENCE
    return SessionPromptContext(
        payload=payload,
        world_date=world_date,
        evidence_status=evidence_status,
        serialized_chars=_serialized_chars(payload),
        truncated_note_keys=truncated_keys,
        omitted_evidence_count=omitted_evidence,
    )
