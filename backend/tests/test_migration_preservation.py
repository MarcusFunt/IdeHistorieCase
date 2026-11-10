"""Rehearse the B1 migration against a populated, disposable M2 database."""
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.scenarios.loader import load_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
BACKEND = Path(__file__).parents[1]
RELEASE = Path(__file__).parents[2] / "scenarios" / "versailles-1919" / "3.0-ai.yaml"


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
                    (SELECT runtime_version FROM game_sessions WHERE id=$1) AS runtime_version,
                    (SELECT is_active FROM scenario_versions
                     WHERE slug='versailles-1919' AND version='1') AS legacy_active,
                    (to_regclass('public.scenario_content_items') IS NOT NULL) AS has_v2_content_table
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
                "runtime_version": "v1_legacy",
                "legacy_active": True,
                "has_v2_content_table": True,
            }
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_upgrade_from_previous_scenario_revision_preserves_release_and_content():
    database = f"ddu_migration_v2_{uuid4().hex[:12]}"
    connection_options = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "ddu_test"),
        "password": os.environ.get("DB_PASSWORD", "ddu_test_password_only"),
    }
    loaded = load_scenario(RELEASE)
    scenario_id = uuid4()
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        migration_env = {**os.environ, "DB_NAME": database}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0007_scenario_content_items"],
            cwd=BACKEND,
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
        db = await asyncpg.connect(database=database, **connection_options)
        try:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT INTO scenario_versions
                    (id, slug, version, initial_state, schema_version, definition,
                     content_hash, bundle_hash, evidence_policy, is_active)
                VALUES ($1, $2, $3, $4::jsonb, 2, $5::jsonb, $6, $7, $8::jsonb, false)
                """,
                scenario_id,
                loaded.definition.slug,
                loaded.definition.version,
                json.dumps(loaded.initial_state),
                json.dumps(loaded.definition.model_dump(mode="json"), ensure_ascii=False),
                loaded.content_hash,
                loaded.bundle_hash,
                json.dumps(loaded.evidence_policy) if loaded.evidence_policy is not None else None,
            )
            for item in loaded.definition.content_items:
                body = loaded.content_items[item.key]
                await db.execute(
                    """
                    INSERT INTO scenario_content_items
                        (id, scenario_version_id, item_key, kind, language, body,
                         authoritative_scope, availability_date, world_valid_from,
                         world_valid_to, visibility, mode, review_status, source_refs,
                         metadata, sha256)
                    VALUES ($1, $2, $3, $4, $5, $6, 'author_context', $7, $8, $9,
                            $10::jsonb, $11, $12, $13::jsonb, $14::jsonb, $15)
                    """,
                    uuid4(),
                    scenario_id,
                    item.key,
                    item.kind,
                    loaded.definition.metadata.language,
                    body,
                    item.availability_date,
                    item.world_valid_from,
                    item.world_valid_to,
                    json.dumps(item.visibility),
                    item.mode,
                    item.review_status,
                    json.dumps(item.source_refs),
                    json.dumps({"source_file": item.file}),
                    sha256(body.encode("utf-8")).hexdigest(),
                )
            await db.execute("COMMIT")
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
                    SELECT v.is_active, v.schema_version, v.runtime_version, v.bundle_hash, c.item_key,
                       c.body, c.sha256,
                       (SELECT version_num FROM alembic_version) AS migration_head
                FROM scenario_versions AS v
                JOIN scenario_content_items AS c ON c.scenario_version_id = v.id
                WHERE v.id = $1
                """,
                scenario_id,
            )
            assert dict(preserved) == {
                "is_active": False,
                "schema_version": 2,
                "runtime_version": "v2_ai",
                "bundle_hash": loaded.bundle_hash,
                "item_key": loaded.definition.content_items[0].key,
                "body": loaded.content_items[loaded.definition.content_items[0].key],
                "sha256": sha256(
                    loaded.content_items[loaded.definition.content_items[0].key].encode("utf-8")
                ).hexdigest(),
                "migration_head": "0012_initialization_provenance",
            }
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_downgrade_refuses_to_drop_published_v2_content():
    database = f"ddu_migration_downgrade_{uuid4().hex[:12]}"
    connection_options = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "ddu_test"),
        "password": os.environ.get("DB_PASSWORD", "ddu_test_password_only"),
    }
    loaded = load_scenario(RELEASE)
    scenario_id = uuid4()
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        migration_env = {**os.environ, "DB_NAME": database}
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
            async with db.transaction():
                await db.execute(
                    """
                    INSERT INTO scenario_versions
                        (id, slug, version, initial_state, schema_version, runtime_version, definition,
                         content_hash, bundle_hash, evidence_policy, is_active)
                    VALUES ($1, $2, $3, $4::jsonb, 2, 'v2_ai', $5::jsonb, $6, $7, $8::jsonb, false)
                    """,
                    scenario_id,
                    loaded.definition.slug,
                    loaded.definition.version,
                    json.dumps(loaded.initial_state),
                    json.dumps(loaded.definition.model_dump(mode="json"), ensure_ascii=False),
                    loaded.content_hash,
                    loaded.bundle_hash,
                    json.dumps(loaded.evidence_policy) if loaded.evidence_policy is not None else None,
                )
                for item in loaded.definition.content_items:
                    body = loaded.content_items[item.key]
                    await db.execute(
                        """
                        INSERT INTO scenario_content_items
                            (id, scenario_version_id, item_key, kind, language, body,
                             authoritative_scope, availability_date, world_valid_from,
                             world_valid_to, visibility, mode, review_status, source_refs,
                             metadata, sha256)
                        VALUES ($1, $2, $3, $4, $5, $6, 'author_context', $7, $8, $9,
                                $10::jsonb, $11, $12, $13::jsonb, $14::jsonb, $15)
                        """,
                        uuid4(),
                        scenario_id,
                        item.key,
                        item.kind,
                        loaded.definition.metadata.language,
                        body,
                        item.availability_date,
                        item.world_valid_from,
                        item.world_valid_to,
                        json.dumps(item.visibility),
                        item.mode,
                        item.review_status,
                        json.dumps(item.source_refs),
                        json.dumps({"source_file": item.file}),
                        sha256(body.encode("utf-8")).hexdigest(),
                    )
        finally:
            await db.close()

        downgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0006_history_authentic"],
            cwd=BACKEND,
            env=migration_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert downgrade.returncode != 0
        assert "cannot drop scenario_content_items" in downgrade.stderr

        db = await asyncpg.connect(database=database, **connection_options)
        try:
            preserved = await db.fetchrow(
                """
                SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                       (SELECT count(*) FROM scenario_content_items
                        WHERE scenario_version_id=$1) AS content_rows
                """,
                scenario_id,
            )
            assert dict(preserved) == {
                "migration_head": "0012_initialization_provenance",
                "content_rows": len(loaded.definition.content_items),
            }
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_downgrade_refuses_to_drop_pending_initialization_and_create_key():
    database = f"ddu_migration_init_downgrade_{uuid4().hex[:12]}"
    connection_options = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "ddu_test"),
        "password": os.environ.get("DB_PASSWORD", "ddu_test_password_only"),
    }
    session_id = uuid4()
    job_id = uuid4()
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        migration_env = {**os.environ, "DB_NAME": database}
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
            async with db.transaction():
                await db.execute(
                    """
                    INSERT INTO game_sessions
                        (id, scenario_version_id, capability_hash, creation_key_hash,
                         capability_expires_at, turn_number, state)
                    VALUES ($1, $2, $3, $4, now() + interval '1 day', 0, '{}'::jsonb)
                    """,
                    session_id,
                    "d19c3d68-d9e7-4ed8-9c29-316a4d830001",
                    "a" * 64,
                    "b" * 64,
                )
                await db.execute(
                    """
                    INSERT INTO session_initialization_jobs (id, session_id, status)
                    VALUES ($1, $2, 'queued')
                    """,
                    job_id,
                    session_id,
                )
        finally:
            await db.close()

        downgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0010_manifest_guard"],
            cwd=BACKEND,
            env=migration_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert downgrade.returncode != 0
        assert "cannot drop durable session initialization data" in downgrade.stderr

        db = await asyncpg.connect(database=database, **connection_options)
        try:
            preserved = await db.fetchrow(
                """
                SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                       (SELECT count(*) FROM session_initialization_jobs WHERE id=$1) AS jobs,
                       (SELECT creation_key_hash FROM game_sessions WHERE id=$2) AS create_key
                """,
                job_id,
                session_id,
            )
            assert dict(preserved) == {
                "migration_head": "0012_initialization_provenance",
                "jobs": 1,
                "create_key": "b" * 64,
            }
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_downgrade_refuses_to_drop_committed_initialization_provenance():
    database = f"ddu_migration_provenance_{uuid4().hex[:12]}"
    connection_options = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "ddu_test"),
        "password": os.environ.get("DB_PASSWORD", "ddu_test_password_only"),
    }
    session_id = uuid4()
    job_id = uuid4()
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        migration_env = {**os.environ, "DB_NAME": database}
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
            async with db.transaction():
                await db.execute(
                    """
                    INSERT INTO game_sessions
                        (id, scenario_version_id, capability_hash, creation_key_hash,
                         capability_expires_at, turn_number, state)
                    VALUES ($1, $2, $3, $4, now() + interval '1 day', 0, '{}'::jsonb)
                    """,
                    session_id,
                    "d19c3d68-d9e7-4ed8-9c29-316a4d830001",
                    "c" * 64,
                    "d" * 64,
                )
                await db.execute(
                    """
                    INSERT INTO session_initialization_jobs
                        (id, session_id, status, repair_used, attempt_metadata, result)
                    VALUES ($1, $2, 'ready', true, $3::jsonb, $4::jsonb)
                    """,
                    job_id,
                    session_id,
                    json.dumps([{"attempt_number": 1, "outcome": "accepted"}]),
                    json.dumps({"opening": {}, "initial_state": {}, "provenance": {}}),
                )
        finally:
            await db.close()

        downgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0011_session_initialization"],
            cwd=BACKEND,
            env=migration_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert downgrade.returncode != 0
        assert "cannot drop initialization repair, provenance, or failure-stage data" in downgrade.stderr

        db = await asyncpg.connect(database=database, **connection_options)
        try:
            preserved = await db.fetchrow(
                """
                SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                       repair_used, attempt_metadata, result
                FROM session_initialization_jobs WHERE id=$1
                """,
                job_id,
            )
            assert preserved["migration_head"] == "0012_initialization_provenance"
            assert preserved["repair_used"] is True
            attempt_metadata = preserved["attempt_metadata"]
            if isinstance(attempt_metadata, str):
                attempt_metadata = json.loads(attempt_metadata)
            assert attempt_metadata == [{"attempt_number": 1, "outcome": "accepted"}]
            assert "provenance" in preserved["result"]
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_downgrade_refuses_to_drop_initialization_failure_stage():
    database = f"ddu_migration_failure_stage_{uuid4().hex[:12]}"
    connection_options = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "ddu_test"),
        "password": os.environ.get("DB_PASSWORD", "ddu_test_password_only"),
    }
    session_id = uuid4()
    job_id = uuid4()
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        migration_env = {**os.environ, "DB_NAME": database}
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
            async with db.transaction():
                await db.execute(
                    """
                    INSERT INTO game_sessions
                        (id, scenario_version_id, capability_hash, capability_expires_at,
                         turn_number, state)
                    VALUES ($1, $2, $3, now() + interval '1 day', 0, '{}'::jsonb)
                    """,
                    session_id,
                    "d19c3d68-d9e7-4ed8-9c29-316a4d830001",
                    "e" * 64,
                )
                await db.execute(
                    """
                    INSERT INTO session_initialization_jobs
                        (id, session_id, status, failure_code, failure_stage)
                    VALUES ($1, $2, 'failed', 'MODEL_OUTPUT_INVALID', 'validation')
                    """,
                    job_id,
                    session_id,
                )
        finally:
            await db.close()

        downgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0011_session_initialization"],
            cwd=BACKEND,
            env=migration_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert downgrade.returncode != 0
        assert "cannot drop initialization repair, provenance, or failure-stage data" in downgrade.stderr

        db = await asyncpg.connect(database=database, **connection_options)
        try:
            preserved = await db.fetchrow(
                """
                SELECT (SELECT version_num FROM alembic_version) AS migration_head,
                       failure_code, failure_stage
                FROM session_initialization_jobs WHERE id=$1
                """,
                job_id,
            )
            assert dict(preserved) == {
                "migration_head": "0012_initialization_provenance",
                "failure_code": "MODEL_OUTPUT_INVALID",
                "failure_stage": "validation",
            }
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()
