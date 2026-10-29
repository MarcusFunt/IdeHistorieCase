"""PostgreSQL integration coverage for immutable scenario publication."""
import asyncio
import importlib.util
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.connection import engine, session_factory
from app.db.models import GameSession, ScenarioVersion
from app.scenarios.loader import load_scenario, load_scenario_text

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
MANIFEST = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "2.5.yaml"
@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


def repository_api():
    try:
        spec = importlib.util.find_spec("app.scenarios.repository")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "scenario database repository is not implemented"
    from app.scenarios.repository import (
        VersionContentConflict,
        activate_scenario_version,
        import_scenario,
    )

    return VersionContentConflict, activate_scenario_version, import_scenario


@pytest_asyncio.fixture
async def restore_legacy_active_version():
    yield
    _, activate, _ = repository_api()
    async with session_factory() as db, db.begin():
        await activate(db, "versailles-1919", "1")


async def test_legacy_seed_remains_an_active_unauthored_stub():
    repository_api()
    async with session_factory() as db:
        row = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.version == "1",
                )
            )
        ).scalar_one()

    assert row.is_active is True
    assert row.schema_version is None
    assert row.definition is None
    assert row.content_hash is None
    assert str(row.id) == "d19c3d68-d9e7-4ed8-9c29-316a4d830001"


async def test_import_is_idempotent_and_rejects_same_version_content_changes():
    conflict, _, import_scenario = repository_api()
    original = load_scenario(MANIFEST)
    async with session_factory() as db, db.begin():
        first = await import_scenario(db, original)
    async with session_factory() as db, db.begin():
        second = await import_scenario(db, original)

    assert second.status == "unchanged"
    assert second.scenario.id == first.scenario.id

    changed_text = MANIFEST.read_text(encoding="utf-8").replace(
        "Forhandl om sikkerhed, retfærdighed, selvbestemmelse og politisk repræsentation.",
        "Revised wording with the same release number.",
    )
    changed = load_scenario_text(changed_text)
    with pytest.raises(conflict, match="increment the scenario version"):
        async with session_factory() as db, db.begin():
            await import_scenario(db, changed)

    async with session_factory() as db:
        stored = await db.get(ScenarioVersion, first.scenario.id)

    assert stored.content_hash == original.content_hash


async def test_sidecar_change_cannot_reuse_an_imported_bundle_version(tmp_path: Path):
    conflict, _, import_scenario = repository_api()
    original = load_scenario(MANIFEST)
    copied_package = tmp_path / "versailles-1919"
    shutil.copytree(MANIFEST.parent, copied_package)
    copied_manifest = copied_package / MANIFEST.name
    changed_sidecar = copied_package / "2.5_behaviors.yaml"
    changed_sidecar.write_text(
        changed_sidecar.read_text(encoding="utf-8") + chr(10) + "# changed package bytes",
        encoding="utf-8",
    )
    changed = load_scenario(copied_manifest)

    assert changed.content_hash == original.content_hash
    assert changed.bundle_hash != original.bundle_hash
    async with session_factory() as db, db.begin():
        first = await import_scenario(db, original)
    assert first.status in {"created", "unchanged"}
    with pytest.raises(conflict, match="increment the scenario version"):
        async with session_factory() as db, db.begin():
            await import_scenario(db, changed)
    async with session_factory() as db:
        stored = await db.get(ScenarioVersion, first.scenario.id)
    assert stored.bundle_hash == original.bundle_hash

async def test_database_guard_rejects_direct_content_mutation():
    _, _, import_scenario = repository_api()
    loaded = load_scenario(MANIFEST)
    async with session_factory() as db, db.begin():
        result = await import_scenario(db, loaded)
    with pytest.raises(DBAPIError, match="immutable"):
        async with session_factory() as db, db.begin():
            row = await db.get(ScenarioVersion, result.scenario.id)
            row.content_hash = "0" * 64
            await db.flush()


async def test_activation_pins_new_and_existing_sessions_to_their_versions(
    restore_legacy_active_version,
):
    _, activate, import_scenario = repository_api()
    loaded = load_scenario(MANIFEST)
    async with session_factory() as db, db.begin():
        imported = await import_scenario(db, loaded)
    async with session_factory() as db, db.begin():
        legacy = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.version == "1",
                )
            )
        ).scalar_one()
        game = GameSession(
            id=uuid4(),
            scenario_version_id=legacy.id,
            capability_hash="a" * 64,
            capability_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            turn_number=0,
            state={"phase": "opening"},
        )
        db.add(game)
        await db.flush()
        old_id, old_state = game.scenario_version_id, dict(game.state)
        await activate(db, "versailles-1919", "2.5")
        assert (await db.get(GameSession, game.id)).scenario_version_id == old_id
        assert (await db.get(GameSession, game.id)).state == old_state
        await activate(db, "versailles-1919", "1")
        rows = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.is_active.is_(True),
                )
            )
        ).scalars().all()
        assert [row.version for row in rows] == ["1"]
        await db.delete(game)
    assert imported.scenario.version == "2.5"


async def test_session_resolution_waiting_on_activation_observes_new_active_version(
    restore_legacy_active_version,
):
    _, activate, _ = repository_api()
    from app.scenarios.repository import get_active_scenario

    activated = asyncio.Event()
    release = asyncio.Event()

    async def hold_activation():
        async with session_factory() as db, db.begin():
            selected = await activate(db, "versailles-1919", "2.5")
            assert selected.version == "2.5"
            activated.set()
            await release.wait()

    async def resolve_active():
        async with session_factory() as db, db.begin():
            row = await get_active_scenario(db, "versailles-1919", lock=True)
            return row.version if row else None

    activation_task = asyncio.create_task(hold_activation())
    await asyncio.wait_for(activated.wait(), timeout=2)
    session_task = asyncio.create_task(resolve_active())
    await asyncio.sleep(0.1)
    assert not session_task.done()
    release.set()
    await asyncio.wait_for(activation_task, timeout=2)

    assert await asyncio.wait_for(session_task, timeout=2) == "2.5"

async def test_competing_activations_leave_exactly_one_active_version(
    restore_legacy_active_version,
):
    _, activate, import_scenario = repository_api()
    async with session_factory() as db, db.begin():
        await import_scenario(db, load_scenario(MANIFEST))

    async def activate_one(version):
        async with session_factory() as db, db.begin():
            await activate(db, "versailles-1919", version)

    await asyncio.gather(activate_one("1"), activate_one("2.5"))
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.is_active.is_(True),
                )
            )
        ).scalars().all()

    assert len(rows) == 1
