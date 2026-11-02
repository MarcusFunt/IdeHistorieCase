# Consolidation validation record

This record separates static checks, unit tests, PostgreSQL integration, Compose smoke, live model checks, and GitHub Actions. A green result in one category does not imply acceptance in another.

## Static, package, and offline checks

Environment: Python 3.13.5 via `uv`; host `uv 0.11.21`; Docker production/test images use `python:3.13-slim`. The project lock was installed with `uv sync --frozen --extra dev`.

| Check | Result |
|---|---|
| `uv run --locked python -m compileall -q app migrations tests` | Passed. |
| `uv run --locked ruff check .` | Passed. |
| `uv run --locked alembic heads` | Exactly one head: `0006_history_authentic`. |
| `uv run --locked alembic history` | Linear 0001 → 0002 → 0003 → 0004 → 0005 → 0006. |
| `uv run --locked alembic upgrade 0005_historical_rag:head --sql` | Passed; emits only the additive 0006 upgrade. |
| Host `uv run --locked pytest -q tests` | 185 passed, 46 PostgreSQL-gated skipped. |
| Isolated PostgreSQL full suite | 231 passed, 0 skipped in 23.82 seconds on a fresh disposable `pgvector/pgvector:0.8.7-pg18-trixie` container, with both `DDU_TEST_DB=1` and `HISTORY_TEST_DATABASE_URL` configured. |
| Focused merge-readiness regression tests | 28 passed across evaluation manifest hashes, LITE reranker response validation, and RAG admission characterization. |
| CI coverage addition | Backend CI now runs the two database-backed history importer tests with the isolated CI PostgreSQL service; this was previously skipped because its dedicated test URL was absent. |

The offline BM25 pilot was run outside Git at `D:\projects\IdeHistorieCase-snapshots\cases-pilot-bm25-20261102`. It reports 40/40 reviewed pilot traces but is incomplete for other profiles and held-out data. The dataset validator reports 0 errors, 0/100 held-out queries, and `draft` status.

## PostgreSQL and migration checks

An isolated Docker Compose project `cases-consolidation-test` used its own `cases-consolidation-test_pgdata` volume and test-only database. It was not connected to the running Cases database volume. A fresh database migrated through all revisions to `0006_history_authentic`.

An initial current Cases pre-upgrade custom-format backup was restored into a separate database on the isolated PostgreSQL server and validated through upgrade and rollback. Immediately before the actual live upgrade, a fresh backup was made from the current database. After the live upgrade completed, that exact archive was restored into a second isolated database and upgraded as an additional verification. The exact archive reported `0005_historical_rag` and these counts before migration:

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

After upgrade, that exact-backup clone reported `0006_history_authentic`, five `history_authentic` tables, and the same M2/B3 counts. A downgrade to 0005 and upgrade back to 0006 also passed on the separate clone. The full PostgreSQL test suite ran against a fresh disposable PostgreSQL 18/pgvector 0.8.7 container with `DDU_TEST_DB=1` and `HISTORY_TEST_DATABASE_URL` pointing only to that container: **231 passed, 0 skipped in 23.82 seconds**. It includes clean migration, populated M2 migration preservation, scenario/API/RAG database tests, history importer idempotence/immutability and failure atomicity, and migration graph assertions. The disposable container was stopped and removed after the run; it did not use the live `ddu-backend_pgdata` volume.

Backup used for the clone:

```text
D:\projects\IdeHistorieCase-snapshots\cases-db-pre-0006-live-20261102-154751\ddu-backend-pre-0006.dump
SHA-256: `df19e9c42784a7865f8c10bfa5d7120b12a6182a91aea3c8a3f0bc904e2eb13d`
```

The fresh archive is 44,079 bytes with 91 restore-list entries. Its `backup-manifest.json` records container, revision, and volume. The earlier backup at `D:\projects\IdeHistorieCase-snapshots\cases-db-pre-0006-20261102-0d2479ec36c64a0bac6caa68c3a4979f` remains intact.

## Compose and API smoke

The isolated Compose project uses host port 18082 and its separate `cases-consolidation-test_pgdata` volume. `docker compose ... config --quiet` passed with the test-only env and overlay. The Python 3.13 production image and test runner image built successfully.

- `/healthz`: `{"status":"alive"}`.
- `/readyz`: `{"status":"ready","database":true}`.
- Deterministic M2 fake-turn smoke: `{"status":"ok","turn_number":1,"provider":"fake"}`; turn submission, idempotent replay, completion, resume, and history replay succeeded.
- Qwen embedding `/v1/models` advertised alias `ddu-embed`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, 1024 normalized float32 dimensions, query/document inputs, instruction version `qwen3-question-to-passages-en-v1`, and max batch size 8.
- The Cases `rag ingest` command sent the four checked-in passages to that worker in the isolated test DB. After a test-only review row was added (explicitly marked as not a production rights/evidence approval), the session evidence API returned `status=ok`, passage `fourteen_points_open_covenants`, source `R04` for an English query. No token was printed. The live API request exercised the Cases request/response path, not just the mocked client contract.
- The reranker was not started; production defaults keep reranking disabled.

During this live check Docker Desktop disconnected twice. Docker reported the test-started embed and previously running speech containers exited with code 255 and `OOMKilled=false`. The existing Cases API and database were rechecked after Docker Desktop recovered and `/readyz` returned ready. The pre-existing speech services were restarted from their LITE Compose project and are now healthy; Qwen TTS used 258 MiB / 1 GiB and reported no OOM. The embedding worker was not running before this test and is currently stopped. No database volume was deleted or recreated.

## Live retrieval and production database update

Live retrieval acceptance is recorded above, against the isolated database only. After [Actions run 41465086514](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/41465086514) passed on implementation commit `9584649cabb12ee263e3c42e5d25e4999f10989f`, the current Cases stack was migrated in place with the verified backup above. `alembic upgrade head` ran from the consolidation worktree using project `ddu-backend`; the existing `ddu-backend_pgdata` volume now reports `0006_history_authentic`. Counts remain: 2 sessions, 1 request, 1 committed turn, 1 event, 2 scenario versions; 0 RAG passage/review/embedding/policy rows; 5 tables in `history_authentic`; pgvector is present.

The API and turn-worker were rebuilt and recreated from the consolidation branch against the same database volume. Post-update checks returned `/healthz` alive, `/readyz` ready/database true, and `/api/v1/status` database true, worker running, inference ready. The port remains loopback `127.0.0.1:18081`. No synthetic session or passage approval was written to the production database. The live retrieval smoke used the isolated test database described above.

### Host source and migration compatibility checkpoint

A read-only host inspection on 2026-11-02 confirmed that pre-merge GitHub `main` is at migration `0004_multiple_events_per_turn`, while PR #11's branch and the running API/worker are compatible with `0006_history_authentic`. The original `D:\projects\IdeHistorieCase` checkout remains on the B3 feature branch and is not the running API source. The running API and worker Compose labels identify `D:\projects\IdeHistorieCase-consolidation`; PostgreSQL and an exited legacy `migrate` container retain Compose labels from the original checkout. This is mixed historical container metadata within the same Compose project, not a second active Cases database. The existing `ddu-backend_pgdata` volume was preserved.

The inspection independently matched 62/62 API, migration, and configuration source files against the consolidation checkout, plus five sampled worker files. These checksum comparisons are reported from that prior host inspection and were not repeated during this documentation/code hardening pass. Read-only database/API checks confirmed Alembic current/head `0006_history_authentic`, 2 sessions, 1 request, 1 committed turn, 1 event, 2 scenario versions, zero B3 passage/review/embedding/policy rows, five `history_authentic` tables with zero releases/documents/passages, and HTTP 200 from `/healthz`, `/readyz`, and `/api/v1/status`.

Do not run the pre-merge `main` or the older B3 checkout against this 0006 database. The fresh pre-upgrade dump above is from migration 0005, so restoring it can lose transactions written after its snapshot. Recovery and downgrade limits are documented in [the backend runbook](M1_RUNBOOK.md#current-host-compatibility-and-recovery) and [the history corpus guide](HISTORY_CORPUS.md#danmarks-breve-authentic-corpus).

## GitHub Actions

[Backend CI run 41465086514](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/41465086514) passed all steps on implementation commit `9584649cabb12ee263e3c42e5d25e4999f10989f`, including PostgreSQL integration, Compose validation, and no-inference backend build/boot. It is historical baseline evidence, not a check on the merge-readiness changes in this record. The final merge-readiness Actions URL and head SHA are recorded in draft PR #11 after that run completes; do not use the baseline run as proof for a later head.

## Known limits

- The held-out retrieval split is empty (0/100), so no held-out quality claim is possible.
- A real model comparison across BM25/dense/hybrid/reranked profiles is separate from the infrastructure/client smoke and remains conditional.
- The worker still uses `FakeProvider`; this branch does not implement model-generated game turns.
- The checked-in Versailles passages are not approved for gameplay.
- Public authentication, browser client, classroom review, backup/restore operations, and live-game security review remain future milestones.
