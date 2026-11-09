"""PostgreSQL acceptance coverage for immutable v2 scenario publication."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.connection import engine, session_factory
from app.db.models import (
    GameSession,
    ScenarioContentItem,
    ScenarioVersion,
)
from app.scenarios.cli import _evidence_readiness
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
        with pytest.raises(ValueError, match="schema version 2.*not supported"):
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
        assert active.version == "1"
        assert imported.scenario.is_active is False
        await db.delete(game)


async def test_import_preview_activation_inspection_and_playable_check_commands(tmp_path):
    path, version = copy_release(tmp_path)
    preview = run_cli("preview", path)
    validated = run_cli("validate", path, "--json")
    imported = run_cli("import", path)
    repeated = run_cli("import", path)
    activated = run_cli("activate", "versailles-1919", "--version", version)
    shown = run_cli("show", "versailles-1919", "--version", version)
    checked = run_cli("playable-check", "versailles-1919", "--version", version)

    assert preview.returncode == 0, preview.stderr
    assert "opening_briefing" in preview.stdout
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["checks"]["schema_valid"]["status"] == "valid"
    assert imported.returncode == repeated.returncode == 0, imported.stderr + repeated.stderr
    assert activated.returncode == 2
    assert "schema version 2 is not supported" in activated.stderr.lower()
    assert shown.returncode == 0 and "opening_briefing" in shown.stdout
    assert checked.returncode == 1
    report = json.loads(checked.stdout)
    assert report["checks"]["schema_valid"]["status"] == "valid"
    assert report["checks"]["publication_integrity"]["status"] == "valid"
    assert report["checks"]["runtime_compatibility"]["status"] == "unsupported"
    assert report["checks"]["author_content_readiness"]["status"] == "not_ready"
    assert report["checks"]["evidence_readiness"]["status"] == "not_required"
    assert report["playable"] is False


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

    async with session_factory() as db, db.begin():
        row = (
            await db.execute(
                select(ScenarioContentItem).where(
                    ScenarioContentItem.scenario_version_id == imported.scenario.id
                )
            )
        ).scalar_one()
        original = (row.body, row.sha256)
    for mutation in (
        "UPDATE scenario_content_items SET body = 'changed', sha256 = '"
        + "0" * 64
        + "' WHERE scenario_version_id = :id",
        "DELETE FROM scenario_content_items WHERE scenario_version_id = :id",
        "TRUNCATE TABLE scenario_content_items",
    ):
        with pytest.raises(DBAPIError, match="immutable|truncate"):
            async with session_factory() as db, db.begin():
                if ":id" in mutation:
                    await db.execute(text(mutation), {"id": imported.scenario.id})
                else:
                    await db.execute(text(mutation))
        async with session_factory() as db:
            row = (
                await db.execute(
                    select(ScenarioContentItem).where(
                        ScenarioContentItem.scenario_version_id == imported.scenario.id
                    )
                )
            ).scalar_one()
            assert (row.body, row.sha256) == original


async def test_sql_guard_rejects_content_metadata_that_disagrees_with_manifest(tmp_path):
    path, version = copy_release(tmp_path)
    loaded = load_scenario(path)
    version_id = uuid4()
    item = loaded.definition.content_items[0]
    body = loaded.content_items[item.key]

    with pytest.raises(DBAPIError, match="does not match its immutable manifest"):
        async with session_factory() as db, db.begin():
            scenario = ScenarioVersion(
                id=version_id,
                slug=loaded.definition.slug,
                version=version,
                schema_version=2,
                definition=loaded.definition.model_dump(mode="json"),
                initial_state={},
                content_hash=loaded.content_hash,
                bundle_hash=loaded.bundle_hash,
                evidence_policy=loaded.evidence_policy,
                is_active=False,
            )
            db.add(scenario)
            await db.flush()
            db.add(
                ScenarioContentItem(
                    id=uuid4(),
                    scenario_version_id=version_id,
                    item_key=item.key,
                    kind=item.kind,
                    language=loaded.definition.metadata.language,
                    body=body,
                    authoritative_scope="author_context",
                    availability_date=item.availability_date,
                    world_valid_from=item.world_valid_from,
                    world_valid_to=item.world_valid_to,
                    visibility=list(item.visibility),
                    mode="postgame",
                    review_status=item.review_status,
                    source_refs=list(item.source_refs),
                    metadata_json={"source_file": item.file},
                    sha256=sha256(body.encode("utf-8")).hexdigest(),
                )
            )
            await db.flush()

    async with session_factory() as db:
        assert await db.get(ScenarioVersion, version_id) is None


async def test_sql_guard_rejects_empty_v2_content_manifest(tmp_path):
    path, version = copy_release(tmp_path)
    loaded = load_scenario(path)
    version_id = uuid4()
    invalid_definition = loaded.definition.model_dump(mode="json")
    invalid_definition["content_items"] = []

    with pytest.raises(DBAPIError, match="ck_scenario_v2_manifest_nonempty_content_items"):
        async with session_factory() as db, db.begin():
            db.add(
                ScenarioVersion(
                    id=version_id,
                    slug=loaded.definition.slug,
                    version=version,
                    schema_version=2,
                    definition=invalid_definition,
                    initial_state=loaded.initial_state,
                    content_hash=loaded.content_hash,
                    bundle_hash=loaded.bundle_hash,
                    evidence_policy=loaded.evidence_policy,
                    is_active=False,
                )
            )
            await db.flush()

    async with session_factory() as db:
        assert await db.get(ScenarioVersion, version_id) is None


async def test_sql_guard_rejects_content_hash_that_disagrees_with_body(tmp_path):
    path, version = copy_release(tmp_path)
    loaded = load_scenario(path)
    version_id = uuid4()
    item = loaded.definition.content_items[0]
    body = loaded.content_items[item.key]

    with pytest.raises(DBAPIError, match="SHA-256 does not match its body"):
        async with session_factory() as db, db.begin():
            db.add(
                ScenarioVersion(
                    id=version_id,
                    slug=loaded.definition.slug,
                    version=version,
                    schema_version=2,
                    definition=loaded.definition.model_dump(mode="json"),
                    initial_state=loaded.initial_state,
                    content_hash=loaded.content_hash,
                    bundle_hash=loaded.bundle_hash,
                    evidence_policy=loaded.evidence_policy,
                    is_active=False,
                )
            )
            await db.flush()
            db.add(
                ScenarioContentItem(
                    id=uuid4(),
                    scenario_version_id=version_id,
                    item_key=item.key,
                    kind=item.kind,
                    language=loaded.definition.metadata.language,
                    body=body,
                    authoritative_scope="author_context",
                    availability_date=item.availability_date,
                    world_valid_from=item.world_valid_from,
                    world_valid_to=item.world_valid_to,
                    visibility=list(item.visibility),
                    mode=item.mode,
                    review_status=item.review_status,
                    source_refs=list(item.source_refs),
                    metadata_json={"source_file": item.file},
                    sha256="0" * 64,
                )
            )
            await db.flush()

    async with session_factory() as db:
        assert await db.get(ScenarioVersion, version_id) is None


async def test_concurrent_conflicting_v2_imports_leave_one_complete_release(tmp_path):
    path, version = copy_release(tmp_path)
    first = load_scenario(path)
    note = path.parent / "notes" / "opening_briefing.md"
    note.write_text(note.read_text(encoding="utf-8") + " A ændring.", encoding="utf-8")
    second = load_scenario(path)

    async def import_once(loaded):
        async with session_factory() as db, db.begin():
            return await import_scenario(db, loaded)

    results = await asyncio.gather(
        import_once(first),
        import_once(second),
        return_exceptions=True,
    )
    assert sum(isinstance(result, VersionContentConflict) for result in results) == 1
    created = next(result for result in results if not isinstance(result, Exception))
    async with session_factory() as db:
        stored = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "versailles-1919",
                    ScenarioVersion.version == version,
                )
            )
        ).scalar_one()
        items = (
            await db.execute(
                select(ScenarioContentItem).where(
                    ScenarioContentItem.scenario_version_id == stored.id
                )
            )
        ).scalars().all()
    assert stored.id == created.scenario.id
    assert stored.bundle_hash == created.scenario.bundle_hash
    assert len(items) == 1


async def test_readiness_does_not_count_approved_passage_for_blocked_source():
    from types import SimpleNamespace

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Database:
        def __init__(self):
            self.results = iter((Result(uuid4()), Result(["R99"])))

        async def execute(self, _query):
            return next(self.results)

    scenario = SimpleNamespace(
        id=uuid4(),
        slug="versailles-1919",
        version="test-blocked-source",
        evidence_policy={
            "scenario_slug": "versailles-1919",
            "scenario_version": "test-blocked-source",
            "policy_version": 1,
            "citation_ready": False,
            "source_access": [
                {
                    "source_id": "R99",
                    "in_world_available_from": "blocked",
                    "scope": "secondary_context_only",
                    "actor_ids": ["britain"],
                    "citation_ready": False,
                }
            ],
        },
    )
    definition = SimpleNamespace(source_refs=("R99",))

    readiness = await _evidence_readiness(Database(), scenario, definition)

    assert readiness["approved_source_ids"] == ["R99"]
    assert readiness["status"] == "not_ready"
    assert readiness["blocked_policy_source_ids"] == ["R99"]
