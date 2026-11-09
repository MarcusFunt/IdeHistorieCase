"""PostgreSQL acceptance coverage for immutable v2 scenario publication."""
import asyncio
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.connection import engine, session_factory
from app.db.models import GameSession, ScenarioContentItem, ScenarioVersion
from app.scenarios.loader import load_scenario
from app.scenarios.repository import (
    VersionContentConflict,
    activate_scenario_version,
    import_scenario,
)

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
BACKEND = Path(__file__).parents[1]
RELEASE = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "3.0-ai.yaml"


@pytest_asyncio.fixture(autouse=True)
async def restore_legacy_activation_and_dispose_engine():
    yield
    try:
        async with session_factory() as db, db.begin():
            await activate_scenario_version(db, "versailles-1919", "1")
    finally:
        await engine.dispose()


def copy_release(tmp_path: Path, *, version: str | None = None) -> tuple[Path, str]:
    version = version or f"test-{uuid4().hex[:12]}"
    target = tmp_path / "versailles-1919"
    target.mkdir(parents=True)
    shutil.copy2(RELEASE, target / RELEASE.name)
    shutil.copytree(RELEASE.parent / "notes", target / "notes")
    path = target / RELEASE.name
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('version: "3.0-ai"', f'version: "{version}"'), encoding="utf-8")
    return path, version


async def import_path(path: Path):
    loaded = load_scenario(path)
    async with session_factory() as db, db.begin():
        return await import_scenario(db, loaded)


def run_cli(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.scenarios.cli", *map(str, args)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_v2_release_and_content_are_sql_backed_after_bundle_files_are_removed(tmp_path):
    path, version = copy_release(tmp_path)
    loaded = load_scenario(path)
    shutil.rmtree(path.parent)

    async with session_factory() as db, db.begin():
        result = await import_scenario(db, loaded)

    async with session_factory() as db:
        stored = await db.get(ScenarioVersion, result.scenario.id)
        items = (
            await db.execute(
                select(ScenarioContentItem).where(
                    ScenarioContentItem.scenario_version_id == result.scenario.id
                )
            )
        ).scalars().all()

    assert result.status == "created"
    assert result.scenario.version == version
    assert result.scenario.is_active is False
    assert result.scenario.initial_state == {}
    assert stored.definition == loaded.definition.model_dump(mode="json")
    assert [(item.item_key, item.body) for item in items] == [
        ("opening_briefing", loaded.content_items["opening_briefing"])
    ]
    assert len(items[0].sha256) == 64


async def test_v2_import_is_idempotent_and_changed_note_bytes_conflict_atomically(tmp_path):
    path, _ = copy_release(tmp_path)
    loaded = load_scenario(path)
    async with session_factory() as db, db.begin():
        first = await import_scenario(db, loaded)
    async with session_factory() as db, db.begin():
        repeated = await import_scenario(db, loaded)

    note = path.parent / "notes" / "opening_briefing.md"
    note.write_text(note.read_text(encoding="utf-8") + " En ændring.", encoding="utf-8")
    changed = load_scenario(path)
    assert changed.content_hash == loaded.content_hash
    assert changed.bundle_hash != loaded.bundle_hash
    with pytest.raises(VersionContentConflict, match="increment the scenario version"):
        async with session_factory() as db, db.begin():
            await import_scenario(db, changed)

    async with session_factory() as db:
        stored = await db.get(ScenarioVersion, first.scenario.id)
        items = (
            await db.execute(
                select(ScenarioContentItem).where(
                    ScenarioContentItem.scenario_version_id == first.scenario.id
                )
            )
        ).scalars().all()

    assert repeated.status == "unchanged"
    assert repeated.scenario.id == first.scenario.id
    assert stored.bundle_hash == loaded.bundle_hash
    assert items[0].body == loaded.content_items["opening_briefing"]
    assert len(items) == 1


async def test_concurrent_identical_v2_imports_publish_one_release(tmp_path):
    path, _ = copy_release(tmp_path)
    loaded = load_scenario(path)

    async def import_once():
        async with session_factory() as db, db.begin():
            return await import_scenario(db, loaded)

    first, second = await asyncio.gather(import_once(), import_once())

    assert {first.status, second.status} == {"created", "unchanged"}
    assert first.scenario.id == second.scenario.id
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.version == loaded.definition.version,
                )
            )
        ).scalars().all()
        items = (
            await db.execute(
                select(ScenarioContentItem).where(
                    ScenarioContentItem.scenario_version_id == first.scenario.id
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert len(items) == 1


async def test_activation_does_not_repin_existing_sessions_to_v2(tmp_path):
    path, version = copy_release(tmp_path)
    imported = await import_path(path)
    session_id = uuid4()
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
            id=session_id,
            scenario_version_id=legacy.id,
            capability_hash="a" * 64,
            capability_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            turn_number=0,
            state={"phase": "opening", "world_date": "1919-01-18"},
        )
        db.add(game)
        await db.flush()
        pinned_id, pinned_state = game.scenario_version_id, dict(game.state)
        await activate_scenario_version(db, "versailles-1919", version)
        after = await db.get(GameSession, session_id)
        active = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.is_active.is_(True),
                )
            )
        ).scalar_one()
        assert after.scenario_version_id == pinned_id
        assert after.state == pinned_state
        assert active.id == imported.scenario.id
        await db.delete(game)


async def test_import_preview_activation_inspection_and_playable_check_commands(tmp_path):
    path, version = copy_release(tmp_path)
    preview = run_cli("preview", path)
    imported = run_cli("import", path)
    repeated = run_cli("import", path)
    activated = run_cli("activate", "versailles-1919", "--version", version)
    shown = run_cli("show", "versailles-1919", "--version", version)
    checked = run_cli("playable-check", "versailles-1919", "--version", version)

    assert preview.returncode == 0, preview.stderr
    assert "opening_briefing" in preview.stdout
    assert imported.returncode == repeated.returncode == 0, imported.stderr + repeated.stderr
    assert activated.returncode == 0, activated.stderr
    assert shown.returncode == 0 and "opening_briefing" in shown.stdout
    assert checked.returncode == 0, checked.stderr
    assert "evidence readiness is separate" in checked.stdout.lower()


async def test_sql_guard_rejects_mutating_published_content(tmp_path):
    path, _ = copy_release(tmp_path)
    imported = await import_path(path)
    with pytest.raises(DBAPIError, match="immutable"):
        async with session_factory() as db, db.begin():
            row = (
                await db.execute(
                    select(ScenarioContentItem).where(
                        ScenarioContentItem.scenario_version_id == imported.scenario.id
                    )
                )
            ).scalar_one()
            row.body = "changed after publication"
            await db.flush()
