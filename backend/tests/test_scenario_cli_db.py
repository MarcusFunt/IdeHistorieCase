"""PostgreSQL-backed developer CLI import and publishing contracts."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.connection import engine, session_factory
from app.db.models import ScenarioVersion

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
BACKEND = Path(__file__).parents[1]
FIXTURE = Path(__file__).parents[2] / "scenarios" / "_fixtures" / "example-second-scenario.yaml"
@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "app.scenarios.cli", *map(str, args)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_import_dry_run_is_read_only_and_reports_database_action():
    async with session_factory() as db:
        before = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "civic-assembly-example",
                    ScenarioVersion.version == "1",
                )
            )
        ).scalar_one_or_none()

    dry_run = run_cli("import", FIXTURE, "--dry-run")

    async with session_factory() as db:
        after = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == "civic-assembly-example",
                    ScenarioVersion.version == "1",
                )
            )
        ).scalar_one_or_none()

    assert dry_run.returncode == 0, dry_run.stderr
    assert "dry run" in dry_run.stdout.lower()
    assert (before is None) == (after is None)


async def test_import_activate_list_and_show_commands_work_without_model_service():
    imported = run_cli("import", FIXTURE)
    repeated = run_cli("import", FIXTURE)
    activated = run_cli("activate", "civic-assembly-example", "--version", "1")
    listed = run_cli("list")
    shown = run_cli("show", "civic-assembly-example", "--version", "1")

    assert imported.returncode == repeated.returncode == 0
    assert "civic-assembly-example@1" in imported.stdout + repeated.stdout
    assert activated.returncode == 0, activated.stderr
    assert "active" in activated.stdout.lower()
    assert listed.returncode == 0 and "civic-assembly-example@1" in listed.stdout
    assert shown.returncode == 0 and "sha256:" in shown.stdout
