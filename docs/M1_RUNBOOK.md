# Backend runbook and external inference diagnostics

## Scope

This repository owns the history application: PostgreSQL, migrations, the FastAPI service, worker monitoring, and the app-specific Danish diagnostic. Model serving is owned by [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE), which runs a pinned vLLM image and model independently. The M2 fake turn flow works without that service.

The earlier M1/M1.5 llama.cpp and GGUF setup is historical and is not the current deployment. See [M1.5 validation](M1_5_VALIDATION.md) for measurements from that earlier setup.

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

Start Local-AI-API-LITE using its own runbook and configuration. It must join `ddu_inference_bus`, advertise the configured `LLM_MODEL_ALIAS` (default `ddu-narrator`), and have no published model port. The backend worker is the only backend service attached to that network.

With both deployments running, run from this repository root:

```sh
bash scripts/m1_smoke.sh
```

The script checks backend and database health, the private model health endpoint, worker/model status, the history application's strict Danish response schema, and the deterministic fake M2 turn. The model diagnostic is separate from M2 session processing; M2 turn results continue to come from the fake provider.

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
