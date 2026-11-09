# CASES Baseline Audit — Goal 0

**Date:** 9 November 2026

**Scope:** Goal 0 only: reconcile source, migration, worktree, deployment, and recovery facts. No implementation or live service changes were made.

## Executive summary

- Fetched origin/main is db0350dc5287701d1ca59addc35566afe309e24a (merge of PR #11). It matches the v2 plan’s stated baseline.
- The original checkout is still on feat/cases-b3-historical-rag at b2490063914880af363a519db09d3015e1a61f5b; it is an ancestor of fetched origin/main and its migration head is 0005_historical_rag.
- Fetched origin/main includes 0006_history_authentic. The database volume’s last documented revision is also 0006_history_authentic, but the database container is stopped, so its present Alembic revision could not be queried.
- The CASES API, worker, migration, and PostgreSQL containers are all stopped. The retained database volume is ddu-backend_pgdata; it was not started, mounted, altered, or deleted.
- The only database dump archive found under the project snapshots is dated 2 November and represents migration 0005_historical_rag. Its checksum matches its manifest. It is stale relative to the last documented 0006 database and is not a current recovery point.
- No open pull requests were returned by the GitHub connector or gh pr list.
- A clean managed worktree has been selected at fetched origin/main, on branch codex/cases-ai-v2.

## Git and worktree state

git fetch origin completed successfully. Local main remains at b65faad; use fetched origin/main rather than that stale local branch for future work.

| Checkout | Branch | HEAD | State |
|---|---|---:|---|
| D:\projects\IdeHistorieCase | feat/cases-b3-historical-rag | b249006 | Dirty: contains user-provided attachments and the goals plan |
| C:\Users\marcu\.codex\worktrees\cases-ci-baseline | detached | 3670a9e | Clean |
| C:\Users\marcu\.codex\worktrees\danmarks-breve-pr1\IdeHistorieCase | codex/danmarks-breve-pr1 | 63d7607 | Clean |
| C:\Users\marcu\.codex\worktrees\m1-gpu-smoke\IdeHistorieCase | codex/external-vllm-cleanup | c592716 | Clean |
| C:\Users\marcu\.codex\worktrees\versailles-b1\IdeHistorieCase | codex/cases-ci-validation-main | 957578e | Dirty |
| D:\projects\IdeHistorieCase-consolidation | codex/cases-consolidation-2026-11-02 | afdc2ae | Clean; pre-merge PR #11 source |
| D:\projects\IdeHistorieCase-history-integration | codex/cases-history-integration | b249006 | Dirty |
| D:\projects\IdeHistorieCase-retrieval-evaluation | codex/retrieval-evaluation | 90dd6ca | Clean |
| D:\projects\IdeHistorieCase-vllm-migration | codex/external-vllm-client | 1d886e8 | Clean |
| C:\Users\marcu\.codex\worktrees\cases-ai-v2\IdeHistorieCase | codex/cases-ai-v2 | db0350d | Created clean from baseline; audit document is now the only local change |

The new worktree is based on the full fetched SHA db0350dc5287701d1ca59addc35566afe309e24a. Existing dirty worktrees were left untouched. The Alembic-head command created a git-ignored backend .venv with CPython 3.13.5; no tracked dependency files changed.

## Migration and deployed-service state

- Running uv run --project . alembic -c alembic.ini heads from the original checkout’s backend directory returned 0005_historical_rag.
- Fetched origin/main contains the linear migration chain through 0006_history_authentic; its migration declares 0005_historical_rag as its parent. In the selected worktree, running uv run --project . alembic -c alembic.ini heads from backend returned 0006_history_authentic without connecting to PostgreSQL.
- The last documented database inspection in docs/CONSOLIDATION_VALIDATION.md reports Alembic current/head 0006_history_authentic, two sessions, one request, one committed turn, one event, and two scenario versions. This is historical evidence from 2 November, not a live query today.
- Docker Desktop’s desktop-linux context reports project ddu-backend as exited. API and worker container labels point to the consolidation checkout; PostgreSQL and the legacy migrate container retain labels from the original checkout. Compose lists both checkout paths in the project configuration. The runbook describes this as mixed historical metadata within one stopped project.
- API and PostgreSQL exited on 5 November; the worker exited with code 137. The migration container exited successfully on 2 November. No CASES endpoint was queried because the services are stopped.
- Docker reports the separate Local-AI-API-LITE vLLM container healthy and running. Its service was not changed or used.

**Live database status:** present Alembic current/head is unknown until the owner explicitly starts or otherwise makes the database available for a read-only query. Do not use the original 0005 checkout against the retained 0006 database.

## Isolated database and recovery evidence

- The stopped cases-consolidation-test Compose project has its own cases-consolidation-test_pgdata volume. The main-branch validation record says it used a test-only database and host port 18082, separate from ddu-backend_pgdata.
- A separate stopped ddu-migration-smoke project has its own ddu-migration-smoke_pgdata volume.
- Main-branch docs/CONSOLIDATION_VALIDATION.md records a fresh database migration through 0006 and a restore/upgrade rehearsal using a pre-upgrade archive. That record is dated 2 November and was not repeated during Goal 0.
- The archive at D:\projects\IdeHistorieCase-snapshots\cases-db-pre-0006-live-20261102-154751\ddu-backend-pre-0006.dump is 44,079 bytes. Its SHA-256 is df19e9c42784a7865f8c10bfa5d7120b12a6182a91aea3c8a3f0bc904e2eb13d, matching backup-manifest.json. The manifest identifies its source as ddu-backend_pgdata at 0005_historical_rag and records 91 restore-list entries.
- No fresh post-0006 backup was identified in the project snapshot directory or tracked recovery records. No backup was restored as part of Goal 0.

**Procedure for later isolated migration work:** create a fresh backup from the verified database source; record and verify its checksum and archive listing; restore it into an isolated database using the dedicated test project and volume; verify Alembic state and required row counts before rehearsing an additive upgrade. Any production cutover requires a separately reviewed recovery plan and explicit authorization. The 2 November archive must not be used as a current rollback point because it predates 0006 and may omit later transactions. Secret-bearing .env files were not read.

## Open pull requests

Both gh pr list --repo MarcusFunt/IdeHistorieCase --state open and the connected GitHub pull-request query returned no open PRs.

## Goal 0 completion

The source and schema revision mismatch, local worktree states, stopped deployment state, isolated database volume, and backup limitations are documented. The new clean worktree is selected from verified origin/main. Goal 0 is complete; no later goal has started.
