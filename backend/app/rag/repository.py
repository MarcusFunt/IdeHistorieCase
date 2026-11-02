"""PostgreSQL persistence for immutable passage corpora and review history."""
from dataclasses import dataclass
from math import isfinite
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    HistoricalPassage,
    HistoricalPassageEmbedding,
    HistoricalPassageReview,
    RagScenarioPolicy,
    ScenarioVersion,
)
from app.rag.corpus import LoadedCorpus

EMBEDDING_DIMENSIONS = 1024
MAX_REVIEW_NOTE_CHARS = 2000


class RagCorpusConflict(ValueError):
    """Immutable corpus identity or passage content conflicts with stored data."""


class RagScenarioNotFound(ValueError):
    """The target authored scenario version is not imported."""


class RagPassageNotFound(ValueError):
    """A review command referred to a passage that was not staged."""


@dataclass(frozen=True)
class IngestSummary:
    status: str
    passages: int
    embeddings: int


def _validate_embeddings(vectors: list[list[float]], expected_count: int) -> None:
    if len(vectors) != expected_count:
        raise ValueError("embedding response count does not match passage count")
    for index, vector in enumerate(vectors):
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding {index} must contain exactly {EMBEDDING_DIMENSIONS} values"
            )
        if any(not isinstance(value, (int, float)) or not isfinite(value) for value in vector):
            raise ValueError(f"embedding {index} contains a non-finite or non-numeric value")
        if not any(value != 0 for value in vector):
            raise ValueError(f"embedding {index} must not be the zero vector")


def _validate_scenario_policy(loaded: LoadedCorpus, scenario: ScenarioVersion) -> None:
    if scenario.definition is None:
        raise RagScenarioNotFound("RAG ingestion requires an authored scenario version")
    phase_ids = {phase["id"] for phase in scenario.definition.get("phases", [])}
    if set(loaded.corpus.phase_world_dates) != phase_ids:
        raise ValueError("phase_world_dates must cover the exact phases in the pinned scenario version")

    actor_ids = {actor["id"] for actor in scenario.definition.get("actors", [])}
    source_ids = set(scenario.definition.get("source_refs", []))
    source_access = {
        entry.get("source_id"): entry
        for entry in (scenario.evidence_policy or {}).get("source_access", [])
        if isinstance(entry, dict) and isinstance(entry.get("source_id"), str)
    }
    for passage in loaded.corpus.passages:
        if passage.source_identifier not in source_ids or passage.source_identifier not in source_access:
            raise ValueError(
                f"passage {passage.key} must reference a source declared by the scenario"
            )
        if not set(passage.actor_ids) <= actor_ids:
            raise ValueError(f"passage {passage.key} references an unknown scenario actor")
        access = source_access[passage.source_identifier]
        availability = access.get("in_world_available_from")
        allowed_actors = access.get("actor_ids")
        if availability in {"blocked", "postgame"}:
            if availability != "postgame" or not passage.postgame_only:
                raise ValueError(f"passage {passage.key} conflicts with source availability policy")
        else:
            from datetime import date

            try:
                policy_date = date.fromisoformat(availability)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"source {passage.source_identifier} has malformed availability policy"
                ) from exc
            if passage.postgame_only or passage.in_world_available_from < policy_date:
                raise ValueError(f"passage {passage.key} broadens source availability")
        if (
            not isinstance(allowed_actors, list)
            or any(not isinstance(actor, str) for actor in allowed_actors)
            or not set(passage.actor_ids) <= set(allowed_actors)
        ):
            raise ValueError(f"passage {passage.key} broadens source actor visibility")
        scope = access.get("scope", "")
        if (str(scope).startswith(("confidential", "restricted"))) != (
            passage.visibility_scope == "confidential"
        ):
            raise ValueError(f"passage {passage.key} visibility scope conflicts with source policy")


async def ingest_corpus(
    db: AsyncSession,
    loaded: LoadedCorpus,
    embeddings: list[list[float]],
    *,
    model_id: str,
) -> IngestSummary:
    """Stage a bounded corpus and embeddings without creating approval decisions."""
    model_id = model_id.strip()
    if not model_id or len(model_id) > 160:
        raise ValueError("embedding model_id must contain 1 to 160 characters")
    _validate_embeddings(embeddings, len(loaded.corpus.passages))

    scenario = (
        await db.execute(
            select(ScenarioVersion).where(
                ScenarioVersion.slug == loaded.corpus.scenario_slug,
                ScenarioVersion.version == loaded.corpus.scenario_version,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if scenario is None:
        raise RagScenarioNotFound(
            f"scenario version not found: {loaded.corpus.scenario_slug}@{loaded.corpus.scenario_version}"
        )
    _validate_scenario_policy(loaded, scenario)

    policy = (
        await db.execute(
            select(RagScenarioPolicy).where(
                RagScenarioPolicy.scenario_version_id == scenario.id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    passages = (
        await db.execute(
            select(HistoricalPassage).where(HistoricalPassage.scenario_version_id == scenario.id)
        )
    ).scalars().all()
    by_key = {row.passage_key: row for row in passages}
    incoming = {passage.key: passage for passage in loaded.corpus.passages}

    if policy is not None:
        if policy.corpus_sha256 != loaded.corpus_sha256:
            raise RagCorpusConflict(
                "this scenario version already has a different immutable RAG corpus; "
                "increment the scenario version"
            )
        if set(by_key) != set(incoming) or any(
            by_key[key].passage_sha256 != passage.passage_sha256
            for key, passage in incoming.items()
        ):
            raise RagCorpusConflict("stored passages do not match the immutable corpus hash")
        passage_ids = {row.passage_key: row for row in passages}
        existing_embeddings = set(
            (
                await db.execute(
                    select(HistoricalPassageEmbedding.passage_id).where(
                        HistoricalPassageEmbedding.model_id == model_id,
                        HistoricalPassageEmbedding.passage_id.in_(
                            [row.id for row in passages]
                        ),
                    )
                )
            ).scalars().all()
        )
        missing = [
            (passage, passage_ids[passage.key])
            for passage in loaded.corpus.passages
            if passage_ids[passage.key].id not in existing_embeddings
        ]
        vector_by_key = {
            passage.key: vector
            for passage, vector in zip(loaded.corpus.passages, embeddings, strict=True)
        }
        for passage, row in missing:
            db.add(
                HistoricalPassageEmbedding(
                    id=uuid4(), passage_id=row.id, passage_sha256=passage.passage_sha256,
                    model_id=model_id, embedding=vector_by_key[passage.key],
                )
            )
        if missing:
            await db.flush()
            return IngestSummary("embeddings_added", len(passages), len(missing))
        return IngestSummary("unchanged", len(passages), 0)

    db.add(
        RagScenarioPolicy(
            id=uuid4(), scenario_version_id=scenario.id,
            scenario_slug=scenario.slug, scenario_version=scenario.version,
            phase_world_dates={
                phase_id: world_date.isoformat()
                for phase_id, world_date in loaded.corpus.phase_world_dates.items()
            },
            corpus_sha256=loaded.corpus_sha256,
        )
    )
    inserted = []
    for passage in loaded.corpus.passages:
        row = HistoricalPassage(
            id=uuid4(), scenario_version_id=scenario.id, scenario_slug=scenario.slug,
            scenario_version=scenario.version, passage_key=passage.key,
            source_identifier=passage.source_identifier, source_title=passage.source_title,
            source_url=passage.source_url, source_anchor=passage.source_anchor,
            rights_basis=passage.rights_basis,
            passage_text=passage.passage_text, passage_sha256=passage.passage_sha256,
            document_date=passage.document_date,
            in_world_available_from=passage.in_world_available_from,
            postgame_only=passage.postgame_only, actor_ids=passage.actor_ids,
            visibility_scope=passage.visibility_scope, corpus_sha256=loaded.corpus_sha256,
        )
        inserted.append((passage, row))
        db.add(row)
    await db.flush()
    for (passage, row), vector in zip(inserted, embeddings, strict=True):
        db.add(
            HistoricalPassageEmbedding(
                id=uuid4(), passage_id=row.id, passage_sha256=passage.passage_sha256,
                model_id=model_id, embedding=vector,
            )
        )
    await db.flush()
    return IngestSummary("created", len(inserted), len(inserted))


async def record_review(
    db: AsyncSession,
    passage_id,
    *,
    reviewer: str,
    decision: str,
    rights_status: str,
    notes: str | None = None,
) -> HistoricalPassageReview:
    """Append a reviewer decision; approval never mutates the passage."""
    reviewer = reviewer.strip()
    if not reviewer or len(reviewer) > 128:
        raise ValueError("reviewer must contain 1 to 128 characters")
    if decision not in {"approved", "rejected", "revoked"}:
        raise ValueError("decision must be approved, rejected, or revoked")
    if rights_status not in {"approved", "pending", "denied", "restricted"}:
        raise ValueError("rights_status must be approved, pending, denied, or restricted")
    if notes is not None and len(notes) > MAX_REVIEW_NOTE_CHARS:
        raise ValueError(f"review notes exceed maximum of {MAX_REVIEW_NOTE_CHARS} characters")
    passage = await db.get(HistoricalPassage, passage_id, with_for_update=True)
    if passage is None:
        raise RagPassageNotFound(f"passage not found: {passage_id}")
    revision = (
        await db.execute(
            select(func.coalesce(func.max(HistoricalPassageReview.revision), 0)).where(
                HistoricalPassageReview.passage_id == passage_id
            )
        )
    ).scalar_one() + 1
    review = HistoricalPassageReview(
        id=uuid4(), passage_id=passage_id, revision=revision,
        review_status=decision, rights_status=rights_status,
        reviewer=reviewer, notes=notes.strip() if notes is not None else None,
    )
    db.add(review)
    await db.flush()
    return review
