"""Transactional persistence for authored, immutable scenario definitions."""
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScenarioVersion
from app.scenarios.loader import LoadedScenario


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

async def import_scenario(db: AsyncSession, loaded: LoadedScenario) -> ImportResult:
    definition = loaded.definition
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
        same_content = (
            existing.schema_version == definition.schema_version
            and existing.content_hash == loaded.content_hash
            and existing.bundle_hash == loaded.bundle_hash
            and existing.behavior_rules == loaded.behavior_rules
            and existing.evidence_policy == loaded.evidence_policy
            and existing.definition == definition.model_dump(mode="json")
            and existing.initial_state == loaded.initial_state
        )
        if same_content:
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
        definition=definition.model_dump(mode="json"),
        content_hash=loaded.content_hash,
        bundle_hash=loaded.bundle_hash,
        behavior_rules=loaded.behavior_rules,
        evidence_policy=loaded.evidence_policy,
        is_active=False,
    )
    db.add(scenario)
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
