# Backend runbook and external inference diagnostics

## Scope

This repository owns the history application: PostgreSQL, migrations, the FastAPI service, worker monitoring, and the app-specific Danish diagnostic. Model serving is owned by [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE), which runs a pinned vLLM image and model independently. The M2 fake turn flow works without that service.

The earlier M1/M1.5 llama.cpp and GGUF setup is historical and is not the current deployment. See [M1.5 validation](M1_5_VALIDATION.md) for measurements from that earlier setup.

## Current host compatibility and recovery

> **Deployment warning — reviewed 2026-11-02, before PR #11 merges:** GitHub `main` is at migration `0004_multiple_events_per_turn`; PR #11 adds `0005_historical_rag` and `0006_history_authentic`. The running API and turn worker were built from `D:\projects\IdeHistorieCase-consolidation`, and the existing PostgreSQL volume `ddu-backend_pgdata` is already at `0006_history_authentic`. The original `D:\projects\IdeHistorieCase` checkout is still the B3 feature branch and is **not** the deployment source. Do not run either old `main` or that old checkout against the migrated database.

The live API is bound to loopback `127.0.0.1:18081`; host port 8000 belongs to another application. This is one Cases stack with mixed historical Compose labels: API and worker labels identify the consolidation checkout, while the unchanged PostgreSQL container and exited legacy `migrate` container retain labels from the original checkout. These labels do not indicate a second active backend or database. Do not recreate PostgreSQL to normalize labels.

The existing source compatibility checkpoint is:

| Item | Verified state |
|---|---|
| Pre-merge `main` | Migration `0004_multiple_events_per_turn` |
| PR #11 branch | Migration head `0006_history_authentic` |
| Running API / worker | Consolidation checkout; 62/62 app, migration, and config files matched by SHA-256; five additional critical worker files matched |
| Running PostgreSQL | Existing `ddu-backend_pgdata` volume, current/head `0006_history_authentic` |
| M2 rows | 2 sessions, 1 request, 1 committed turn, 1 event, 2 scenario versions |
| B3 rows | 0 passages, 0 reviews, 0 embeddings, 0 policies |
| `history_authentic` | Five tables; 0 releases, 0 documents, 0 passages |
| Health | `/healthz`, `/readyz`, `/api/v1/status` returned HTTP 200; database ready, worker running, inference ready |

These facts were checked read-only on 2026-11-02. The backup made immediately before the 0006 migration is outside Git at `D:\projects\IdeHistorieCase-snapshots\cases-db-pre-0006-live-20261102-154751\ddu-backend-pre-0006.dump` (SHA-256 `df19e9c42784a7865f8c10bfa5d7120b12a6182a91aea3c8a3f0bc904e2eb13d`). A restored clone was verified before upgrade. That archive represents the database at 0005; restoring it later can discard transactions written after the snapshot, so it is not a routine rollback button.

For read-only inspection, always name the compatible source and project explicitly; never infer deployment source from the current directory:

```powershell
$repo = 'D:\projects\IdeHistorieCase-consolidation'
$compose = Join-Path $repo 'deployment\backend\compose.yaml'
$casesEnvFile = 'D:\projects\IdeHistorieCase\deployment\backend\.env'
# This existing local file contains secrets. Never print or commit it.
docker compose --project-directory $repo -p ddu-backend -f $compose --env-file $casesEnvFile config --quiet
docker exec ddu-backend-api-1 alembic current
docker exec ddu-backend-api-1 alembic heads
```

After PR #11 merges, use one explicitly selected checkout at a revision containing both migrations, one absolute Compose-file path from that checkout, and one documented project-directory policy for every operation. Recheck `alembic current` and `alembic heads` before any deploy. Do not use the pre-merge `main` or any migration tree ending at 0004 with the 0006 database. Do not use `git checkout`, `git reset`, or an older image as a database rollback.

**Recovery rules for the 0006 database:**

- Prefer a forward application fix when the schema is already at 0006.
- Do not use Alembic downgrade as routine production recovery. Revision `0006_history_authentic` drops the `history_authentic` schema with `CASCADE`, removing its five tables and any stored releases, documents, and passages.
- A downgrade/re-upgrade tested on a disposable clone does not authorize that operation on the live database. For a data-bearing deployment, require a fresh, independently verified full database backup and a separately reviewed recovery/cutover plan.
- Before any restore, restore the selected backup into an isolated database, verify migration state and required data, and assess transactions that would be lost. Cut over only through a separately reviewed operation with explicit authorization.
- Never run `docker compose down -v`, remove `ddu-backend_pgdata`, or use broad Docker cleanup that removes volumes.

The commands under [Start the backend](#start-the-backend) below are for a fresh or deliberately isolated development deployment. They are not instructions to redeploy this host from pre-merge `main`. There is no need to restart or rebuild production for this documentation-only hardening.

## Start the backend

Follow the backend-only instructions in [README](../README.md). Create the external `ddu_inference_bus` network if it does not exist, copy `deployment/backend/.env.example` to `deployment/backend/.env`, set a long random database password, then validate and start only this repository's backend Compose project:

```sh
docker network inspect ddu_inference_bus >/dev/null 2>&1 || docker network create ddu_inference_bus
cp deployment/backend/.env.example deployment/backend/.env
# Edit deployment/backend/.env and set POSTGRES_PASSWORD.
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env config --quiet
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env up -d --build
```

`/healthz` checks process liveness. `/readyz` checks PostgreSQL only, so it remains ready when inference is unavailable. `/api/v1/status` reports the worker heartbeat and inference state separately. PostgreSQL and model ports are not published; the API binds to host loopback.

## Optional real-model diagnostic

Start Local-AI-API-LITE using its own runbook and configuration. It must join `ddu_inference_bus`, advertise the configured `LLM_MODEL_ALIAS` (default `ddu-narrator`), and have no published model port. The backend `turn-worker` uses the network for the optional narrator diagnostic; the API also joins it for the separately configured `ddu-embed` retrieval service. PostgreSQL stays off the inference network.

With both deployments running, run from this repository root:

```sh
bash scripts/m1_smoke.sh
```

The script checks backend and database health, the private narrator health endpoint, worker/model status, the history application's strict Danish response schema, and the deterministic fake M2 turn. The model diagnostic is separate from M2 session processing; M2 turn results continue to come from the fake provider. Historical evidence queries additionally require the pinned `ddu-embed` retrieval worker; `/readyz` and fake turns do not.

To check inference failure and recovery, stop and restart the model through the Local-AI-API-LITE project only. The backend and database should remain up; after the heartbeat poll, `/api/v1/status` reports `degraded`, then returns to `ready` when the model is available again. `/readyz` stays 200 because it reflects database readiness. The old pinned llama.cpp runtime and verified GGUF have also been exercised as a rollback path without resetting the application database; the evidence is recorded in Local-AI-API-LITE `docs/CUTOVER_AND_ROLLBACK.md`.

## Troubleshooting

- **Model health fails:** check the Local-AI-API-LITE service logs, its Docker network attachment, model loading, and GPU availability.
- **Wrong model alias:** confirm `LLM_MODEL_ALIAS` matches an ID returned by the external server's `/v1/models` endpoint.
- **Worker status is stale:** check the worker container, database connectivity, and `WORKER_POLL_SECONDS` / `WORKER_STALE_SECONDS` settings.
- **Danish probe rejects output:** inspect probe errors and confirm the external service supports strict JSON schema output and the configured chat-template options. Invalid, empty, and truncated output remains a failure.
- **Database migration failed:** inspect `migrate` logs and verify PostgreSQL readiness and credentials. Changing `POSTGRES_PASSWORD` does not change the password of an already initialized database volume.
- **VRAM exhausted:** adjust the externally owned model serving profile or stop competing workloads using Local-AI-API-LITE's runbook.

## Safety and remaining work

The diagnostic `/api/v1/status` endpoint is unauthenticated and must not be exposed through a public gateway. The M2 capability mechanism is a local development boundary, not classroom authentication. Add TLS, a recovery and rotation policy, rate limits, retention/deletion controls, least-privileged runtime database credentials, backup/restore, and a security review before classroom deployment.
