# Consolidation validation record

This record separates static checks, unit tests, PostgreSQL integration, Compose smoke, live model checks, and GitHub Actions. A green result in one category does not imply acceptance in another.

## Static, package, and offline checks

Environment: Python 3.13 in `python:3.13-slim`; host `uv 0.11.21`. The project lock was installed with `uv sync --frozen --extra dev`.

| Check | Result |
|---|---|
| `uv run --locked python -m compileall -q app migrations tests` | Passed. |
| `uv run --locked ruff check .` | Passed. |
| `uv run --locked alembic heads` | Exactly one head: `0006_history_authentic`. |
| `uv run --locked alembic history` | Linear 0001 → 0002 → 0003 → 0004 → 0005 → 0006. |
| `uv run --locked alembic upgrade 0005_historical_rag:head --sql` | Passed; emits only the additive 0006 upgrade. |
| Host `uv run --locked pytest -q tests` | 167 passed, 46 PostgreSQL-gated skipped. |
| Isolated PostgreSQL full suite | 213 passed, 0 skipped (details below). |

The offline BM25 pilot was run outside Git at `D:\projects\IdeHistorieCase-snapshots\cases-pilot-bm25-20261102`. It reports 40/40 reviewed pilot traces but is incomplete for other profiles and held-out data. The dataset validator reports 0 errors, 0/100 held-out queries, and `draft` status.

## PostgreSQL and migration checks

An isolated Docker Compose project `cases-consolidation-test` used its own `cases-consolidation-test_pgdata` volume and test-only database. It was not connected to the running Cases database volume. A fresh database migrated through all revisions to `0006_history_authentic`.

The current Cases pre-upgrade custom-format backup was restored into a separate database on the isolated PostgreSQL server. Before the upgrade it reported:

```text
revision=0005_historical_rag
game_sessions=2, turn_requests=1, committed_turns=1, game_events=1
scenario_versions=2
historical_passages=0, reviews=0, embeddings=0, rag_policies=0
```

After `alembic upgrade head` on that restored clone:

```text
revision=0006_history_authentic
game_sessions=2, turn_requests=1, committed_turns=1, game_events=1
scenario_versions=2
historical_passages=0, reviews=0, embeddings=0, rag_policies=0
history_authentic tables=5, vector extension=present
```

The clone proves the new migration preserves the backed-up row counts while adding the isolated history schema. The full PostgreSQL test suite ran with `DDU_TEST_DB=1` and a disposable history test database: **213 passed in 18.84 seconds**. It includes clean migration, populated M2 migration preservation, scenario/API/RAG database tests, history importer idempotence/immutability, and migration graph assertions.

Backup used for the clone:

```text
D:\projects\IdeHistorieCase-snapshots\cases-db-pre-0006-20261102-0d2479ec36c64a0bac6caa68c3a4979f\ddu-backend-pre-0006.dump
SHA-256: `780e83c0c229febf6b9de493e139870fa8a51d8146818c0071b2e9d5b6559e14`
```

## Compose and API smoke

The isolated Compose project uses host port 18082 and its separate `cases-consolidation-test_pgdata` volume. `docker compose ... config --quiet` passed with the test-only env and overlay. The Python 3.13 production image and test runner image built successfully.

- `/healthz`: `{"status":"alive"}`.
- `/readyz`: `{"status":"ready","database":true}`.
- Deterministic M2 fake-turn smoke: `{"status":"ok","turn_number":1,"provider":"fake"}`; turn submission, idempotent replay, completion, resume, and history replay succeeded.
- Qwen embedding `/v1/models` advertised alias `ddu-embed`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, 1024 normalized float32 dimensions, query/document inputs, instruction version `qwen3-question-to-passages-en-v1`, and max batch size 8.
- The Cases `rag ingest` command sent the four checked-in passages to that worker in the isolated test DB. After a test-only review row was added (explicitly marked as not a production rights/evidence approval), the session evidence API returned `status=ok`, passage `fourteen_points_open_covenants`, source `R04` for an English query. No token was printed. The live API request exercised the Cases request/response path, not just the mocked client contract.
- The reranker was not started; production defaults keep reranking disabled.

During this live check Docker Desktop disconnected twice. Docker reported the test-started embed and previously running speech containers exited with code 255 and `OOMKilled=false`. The existing Cases API and database were rechecked after Docker Desktop recovered and `/readyz` returned ready; the live DB remained at `0005_historical_rag`. The pre-existing speech services were restarted from their LITE Compose project. The embedding worker was not running before this test and was left stopped after the smoke. No database volume was deleted or recreated.

## Live retrieval and production database update

Live retrieval acceptance is recorded above, against the isolated database only. The existing production Cases DB was at `0005_historical_rag` when the backup above was taken. The user explicitly instructed that this running stack be updated in place; after the review branch Actions pass, run the already tested migration on that existing volume and update only its API/turn-worker images. No live database mutation has occurred yet.

## GitHub Actions

Pending final pushed head. PR review does not imply merge; this consolidation remains a review branch until the user merges it.

## Known limits

- The held-out retrieval split is empty (0/100), so no held-out quality claim is possible.
- A real model comparison across BM25/dense/hybrid/reranked profiles is separate from the infrastructure/client smoke and remains conditional.
- The worker still uses `FakeProvider`; this branch does not implement model-generated game turns.
- The checked-in Versailles passages are not approved for gameplay.
- Public authentication, browser client, classroom review, backup/restore operations, and live-game security review remain future milestones.
