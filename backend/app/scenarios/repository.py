"""Transactional persistence for authored, immutable scenario definitions."""
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScenarioContentItem, ScenarioVersion
from app.scenarios.content_integrity import (
    ScenarioContentIntegrityError,
    verify_v2_content_rows,
)
from app.scenarios.loader import LoadedScenario
from app.scenarios.runtime_compatibility import require_runtime_compatible
from app.scenarios.schema_v2 import ScenarioDefinitionV2


class VersionContentConflict(ValueError):
    """An immutable slug/version pair was requested with different content."""


class ScenarioVersionNotFound(ValueError):
    """A scenario version does not exist or cannot be activated."""


@dataclass(frozen=True)
class ImportResult:
    status: str
    scenario: ScenarioVersion


async def _advisory_lock(db: AsyncSession, key: str) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def _advisory_shared_lock(db: AsyncSession, key: str) -> None:
    await db.execute(select(func.pg_advisory_xact_lock_shared(func.hashtextextended(key, 0))))


def _expected_content_items(loaded: LoadedScenario) -> list[dict]:
    if not isinstance(loaded.definition, ScenarioDefinitionV2):
        if loaded.content_items:
            raise ValueError("schema v1 releases cannot contain v2 scenario content items")
        return []

    expected_keys = {item.key for item in loaded.definition.content_items}
    if set(loaded.content_items) != expected_keys:
        raise ValueError("v2 import requires loaded text for every manifest content item")
    expected = []
    for item in loaded.definition.content_items:
        body = loaded.content_items[item.key]
        expected.append(
            {
                "item_key": item.key,
                "kind": item.kind,
                "language": loaded.definition.metadata.language,
                "body": body,
                "authoritative_scope": "author_context",
                "availability_date": item.availability_date,
                "world_valid_from": item.world_valid_from,
                "world_valid_to": item.world_valid_to,
                "visibility": list(item.visibility),
                "mode": item.mode,
                "review_status": item.review_status,
                "source_refs": list(item.source_refs),
                "metadata_json": {"source_file": item.file},
                "sha256": sha256(body.encode("utf-8")).hexdigest(),
            }
        )
    return sorted(expected, key=lambda value: value["item_key"])


async def _stored_content_matches(
    db: AsyncSession,
    scenario_version_id,
    expected: list[dict],
) -> bool:
    rows = (
        await db.execute(
            select(ScenarioContentItem)
            .where(ScenarioContentItem.scenario_version_id == scenario_version_id)
            .order_by(ScenarioContentItem.item_key)
        )
    ).scalars().all()
    if len(rows) != len(expected):
        return False
    fields = (
        "item_key",
        "kind",
        "language",
        "body",
        "authoritative_scope",
        "availability_date",
        "world_valid_from",
        "world_valid_to",
        "visibility",
        "mode",
        "review_status",
        "source_refs",
        "metadata_json",
        "sha256",
    )
    return all(
        all(getattr(row, field) == expected_item[field] for field in fields)
        for row, expected_item in zip(rows, expected, strict=True)
    )


async def scenario_matches_loaded(
    db: AsyncSession,
    existing: ScenarioVersion,
    loaded: LoadedScenario,
) -> bool:
    """Check both immutable manifest fields and the full SQL-backed note set."""
    definition = loaded.definition
    if not (
        existing.schema_version == definition.schema_version
        and existing.content_hash == loaded.content_hash
        and existing.bundle_hash == loaded.bundle_hash
        and existing.behavior_rules == loaded.behavior_rules
        and existing.evidence_policy == loaded.evidence_policy
        and existing.definition == definition.model_dump(mode="json")
        and existing.initial_state == loaded.initial_state
    ):
        return False
    return await _stored_content_matches(
        db,
        existing.id,
        _expected_content_items(loaded),
    )


async def import_scenario(db: AsyncSession, loaded: LoadedScenario) -> ImportResult:
    definition = loaded.definition
    expected_content = _expected_content_items(loaded)
    await _advisory_lock(db, f"scenario-import:{definition.slug}:{definition.version}")
    existing = (
        await db.execute(
            select(ScenarioVersion)
            .where(
                ScenarioVersion.slug == definition.slug,
                ScenarioVersion.version == definition.version,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        if await scenario_matches_loaded(db, existing, loaded):
            return ImportResult("unchanged", existing)
        raise VersionContentConflict(
            f"{definition.slug}@{definition.version} already contains different immutable "
            "content; increment the scenario version"
        )

    scenario = ScenarioVersion(
        id=uuid4(),
        slug=definition.slug,
        version=definition.version,
        initial_state=loaded.initial_state,
        schema_version=definition.schema_version,
        runtime_version="v2_ai" if definition.schema_version == 2 else "v1_legacy",
        definition=definition.model_dump(mode="json"),
        content_hash=loaded.content_hash,
        bundle_hash=loaded.bundle_hash,
        behavior_rules=loaded.behavior_rules,
        evidence_policy=loaded.evidence_policy,
        is_active=False,
    )
    db.add(scenario)
    await db.flush()
    for item in expected_content:
        db.add(
            ScenarioContentItem(
                id=uuid4(),
                scenario_version_id=scenario.id,
                **item,
            )
        )
    if expected_content:
        await db.flush()
    return ImportResult("created", scenario)


async def activate_scenario_version(
    db: AsyncSession,
    slug: str,
    version: str,
) -> ScenarioVersion:
    await _advisory_lock(db, f"scenario-activate:{slug}")
    versions = (
        await db.execute(
            select(ScenarioVersion)
            .where(ScenarioVersion.slug == slug)
            .order_by(ScenarioVersion.version)
            .with_for_update()
        )
    ).scalars().all()
    selected = next((row for row in versions if row.version == version), None)
    if selected is None:
        raise ScenarioVersionNotFound(f"scenario version not found: {slug}@{version}")
    is_legacy_rollback = slug == "versailles-1919" and version == "1"
    if selected.definition is None and not is_legacy_rollback:
        raise ScenarioVersionNotFound(
            f"{slug}@{version} is a legacy stub and cannot be activated"
        )
    if selected.schema_version == 2:
        rows = (
            await db.execute(
                select(ScenarioContentItem)
                .where(ScenarioContentItem.scenario_version_id == selected.id)
                .order_by(ScenarioContentItem.item_key)
            )
        ).scalars().all()
        try:
            verify_v2_content_rows(selected, rows)
        except ScenarioContentIntegrityError as exc:
            raise ScenarioVersionNotFound(
                f"{slug}@{version} has incomplete or inconsistent SQL-backed content"
            ) from exc
    elif selected.definition is not None:
        require_runtime_compatible(selected)

    for row in versions:
        row.is_active = False
    await db.flush()
    selected.is_active = True
    await db.flush()
    return selected


async def get_active_scenario(
    db: AsyncSession,
    slug: str,
    *,
    lock: bool = False,
) -> ScenarioVersion | None:
    if lock:
        # Share the activation lock so a session waits for an in-flight switch, then
        # resolves the newly active row using a fresh READ COMMITTED statement snapshot.
        await _advisory_shared_lock(db, f"scenario-activate:{slug}")
    statement = select(ScenarioVersion).where(
        ScenarioVersion.slug == slug,
        ScenarioVersion.is_active.is_(True),
    )
    if lock:
        statement = statement.with_for_update(read=True)
    return (await db.execute(statement)).scalar_one_or_none()

async def list_scenario_versions(db: AsyncSession) -> list[ScenarioVersion]:
    return list(
        (
            await db.execute(
                select(ScenarioVersion).order_by(
                    ScenarioVersion.slug,
                    ScenarioVersion.is_active.desc(),
                    ScenarioVersion.created_at,
                    ScenarioVersion.version,
                )
            )
        ).scalars()
    )
