"""Migration and append-only storage checks against disposable PostgreSQL."""
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
async def test_historical_rag_migration_adds_vector_store_without_changing_m2():
    database = f"ddu_rag_migration_{uuid4().hex[:12]}"
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
            [sys.executable, "-m", "alembic", "upgrade", "0004_multiple_events_per_turn"],
            cwd=BACKEND,
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
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
            tables = await db.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
            table_names = {row["tablename"] for row in tables}
            assert {
                "rag_scenario_policies",
                "historical_passages",
                "historical_passage_reviews",
                "historical_passage_embeddings",
            } <= table_names

            embedding_type = await db.fetchval(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                WHERE a.attrelid='historical_passage_embeddings'::regclass
                  AND a.attname='embedding'
                """
            )
            assert embedding_type == "vector(1024)"
            assert await db.fetchval("SELECT to_regclass('public.historical_passages_search_idx')")
            assert await db.fetchval("SELECT to_regclass('public.historical_embeddings_hnsw_idx')")

            m2_columns = await db.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='turn_requests'
                ORDER BY ordinal_position
                """
            )
            assert [row["column_name"] for row in m2_columns] == [
                "id", "session_id", "client_turn_id", "expected_turn", "text",
                "input_language", "status", "attempts", "lease_token", "leased_until",
                "next_attempt_at", "failure_code", "created_at", "updated_at",
            ]

            scenario_id = await db.fetchval(
                "SELECT id FROM scenario_versions WHERE slug='versailles-1919' AND version='1'"
            )
            policy_id, passage_id = uuid4(), uuid4()
            await db.execute(
                """
                INSERT INTO rag_scenario_policies
                    (id, scenario_version_id, scenario_slug, scenario_version,
                     phase_world_dates, corpus_sha256)
                VALUES ($1, $2, 'versailles-1919', '1', '{\"opening\":\"1919-03-20\"}'::jsonb, $3)
                """,
                policy_id,
                scenario_id,
                "a" * 64,
            )
            await db.execute(
                """
                INSERT INTO historical_passages
                    (id, scenario_version_id, scenario_slug, scenario_version, passage_key,
                     source_identifier, source_title, source_url, source_anchor, rights_basis, passage_text,
                     passage_sha256, document_date, in_world_available_from, postgame_only,
                     actor_ids, visibility_scope, corpus_sha256)
                VALUES ($1, $2, 'versailles-1919', '1', 'test-passage', 'R04', 'Source',
                        'https://example.test/source', 'p. 1', 'Test rights basis.', 'Exact passage.', $3,
                        DATE '1918-01-08', DATE '1918-01-08', false,
                        '[\"britain\"]'::jsonb, 'actor_limited', $4)
                """,
                passage_id,
                scenario_id,
                "b" * 64,
                "a" * 64,
            )
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                await db.execute(
                    "UPDATE historical_passages SET passage_text='changed' WHERE id=$1",
                    passage_id,
                )
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                await db.execute("DELETE FROM historical_passages WHERE id=$1", passage_id)

            review_id = uuid4()
            await db.execute(
                """
                INSERT INTO historical_passage_reviews
                    (id, passage_id, revision, review_status, rights_status, reviewer)
                VALUES ($1, $2, 1, 'approved', 'approved', 'migration-test')
                """,
                review_id,
                passage_id,
            )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await db.execute(
                    "UPDATE historical_passage_reviews SET review_status='revoked' WHERE id=$1",
                    review_id,
                )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await db.execute("DELETE FROM historical_passage_reviews WHERE id=$1", review_id)
        finally:
            await db.close()

        subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0004_multiple_events_per_turn"],
            cwd=BACKEND,
            env=migration_env,
            check=True,
            capture_output=True,
            text=True,
        )
        db = await asyncpg.connect(database=database, **connection_options)
        try:
            assert await db.fetchval("SELECT to_regclass('public.historical_passages')") is None
            assert await db.fetchval("SELECT to_regclass('public.turn_requests')")
        finally:
            await db.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()
