"""Rehearse the B1 migration against a populated, disposable M2 database."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
BACKEND = Path(__file__).parents[1]


@pytest.mark.asyncio
async def test_upgrade_from_populated_m2_database_preserves_sessions_and_turn_history():
    database = f"ddu_migration_{uuid4().hex[:12]}"
    connection_options = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "ddu_test"),
        "password": os.environ.get("DB_PASSWORD", "ddu_test_password_only"),
    }
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        migration_env = {**os.environ, "DB_NAME": database}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0002_durable_turns"],
            cwd=BACKEND,
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
        db = await asyncpg.connect(database=database, **connection_options)
        try:
            await db.execute(
                """
                INSERT INTO game_sessions
                    (id, scenario_version_id, capability_hash, capability_expires_at,
                     turn_number, state)
                VALUES ($1, $2, $3, now() + interval '1 day', 1, $4::jsonb)
                """,
                "11111111-1111-4111-8111-111111111111",
                "d19c3d68-d9e7-4ed8-9c29-316a4d830001",
                "a" * 64,
                '{"phase":"opening","last_turn_number":1}',
            )
            await db.execute(
                """
                INSERT INTO turn_requests
                    (id, session_id, client_turn_id, expected_turn, text,
                     input_language, status, attempts)
                VALUES ($1, $2, $3, 0, 'existing M2 turn', 'da', 'completed', 1)
                """,
                "22222222-2222-4222-8222-222222222222",
                "11111111-1111-4111-8111-111111111111",
                "33333333-3333-4333-8333-333333333333",
            )
            await db.execute(
                """
                INSERT INTO committed_turns (id, session_id, request_id, turn_number, result)
                VALUES ($1, $2, $3, 1, '{"turn_number":1}'::jsonb)
                """,
                "44444444-4444-4444-8444-444444444444",
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
            await db.execute(
                """
                INSERT INTO game_events (id, committed_turn_id, event_type, payload)
                VALUES ($1, $2, 'player_argument', '{"text":"existing"}'::jsonb)
                """,
                "55555555-5555-4555-8555-555555555555",
                "44444444-4444-4444-8444-444444444444",
            )
        finally:
            await db.close()

        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
        db = await asyncpg.connect(database=database, **connection_options)
        try:
            preserved = await db.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM game_sessions WHERE id=$1) AS sessions,
                    (SELECT count(*) FROM turn_requests WHERE id=$2) AS requests,
                    (SELECT count(*) FROM committed_turns WHERE id=$3) AS turns,
                    (SELECT count(*) FROM game_events WHERE id=$4) AS events,
                    (SELECT player_role_id FROM game_sessions WHERE id=$1) AS role_id,
                    (SELECT is_active FROM scenario_versions
                     WHERE slug='versailles-1919' AND version='1') AS legacy_active
                """,
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
                "44444444-4444-4444-8444-444444444444",
                "55555555-5555-4555-8555-555555555555",
            )
            assert dict(preserved) == {
                "sessions": 1,
                "requests": 1,
                "turns": 1,
                "events": 1,
                "role_id": None,
                "legacy_active": True,
            }
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()
