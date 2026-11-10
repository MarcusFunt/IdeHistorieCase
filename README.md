# IdeHistorieCase — durable historical gameplay runtime

The repository contains a durable backend, authored scenarios, a structured model-driven v2 turn runtime, B3 session-scoped historical RAG, an offline retrieval-evaluation harness, and an isolated Danmarks Breve source-library importer. Model proposals are validated by the application before their events and state projection are committed. This is **not** a complete classroom-ready deployment.

## Service ownership

| Part | Owner | Current role |
|---|---|---|
| History API, database, migrations, durable queue and turn validation | This repository (`ddu-backend`) | Application state and validated v2 gameplay |
| vLLM, embedding, reranking, model cache, checkpoints, GPU and serving limits | [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE) | Independently owned inference services |
| History-specific model request and Danish diagnostic schema | This repository | A narrow client adapter used by the worker monitor and explicit probe |
| Scenario-approved historical evidence and retrieval policy | This repository | Session-scoped evidence search; the checked-in Versailles excerpts remain unapproved |
| Retrieval evaluation and authentic history library | This repository | Offline benchmark and a separate, immutable `history_authentic` schema |

`turn-worker`, the API, and the optional evaluation client join the external Docker network `ddu_inference_bus`. The API uses it for historical retrieval; the worker calls the configured structured inference service for v2 turns. If inference is unavailable, the durable request receives a retryable failure and no canned reply is committed. PostgreSQL does not join that network. Cases does not start or change model services. The llama.cpp/GGUF runs recorded in [M1.5 validation](docs/M1_5_VALIDATION.md) are historical evidence; they are not the current inference deployment.

> Before starting or redeploying an existing Cases host, read the [current host compatibility and recovery runbook](docs/M1_RUNBOOK.md#current-host-compatibility-and-recovery). The current host database is at migration `0006_history_authentic`; a pre-merge `main` checkout ending at `0004_multiple_events_per_turn` is not compatible with it.

## Run the backend without a model

Requirements: Docker Compose v2 and Python 3.13 for local test commands. Health and database operations work with no inference service. V2 initialization and turns require real inference and remain retryable while it is unavailable.

```sh
docker network inspect ddu_inference_bus >/dev/null 2>&1 || docker network create ddu_inference_bus
cp deployment/backend/.env.example deployment/backend/.env
```

Edit `deployment/backend/.env` and set a long random `POSTGRES_PASSWORD`. Historical RAG requires a reachable `ddu-embed` service on `ddu_inference_bus`; its URL and model ID can stay at their defaults when the shared deployment uses those names. Then start the backend project:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env config --quiet
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env up -d --build
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/api/v1/status
```

The host API port defaults to `8000`. If another local service uses it, set `API_HOST_PORT=18081` (or another free port) in `deployment/backend/.env` and use that port for the health checks and smoke command. Compose binds the API to `127.0.0.1` in either case.

`/healthz` is process liveness and `/readyz` checks PostgreSQL. Neither depends on inference. With no model service, `/api/v1/status` reports inference as degraded; v2 generation failures are retryable and do not create committed turns. The API is bound to host loopback; Compose publishes no database port.

Do not use `docker compose down -v` for routine shutdown; it can remove the PostgreSQL volume.

## Optional external model diagnostic

For a real vLLM endpoint, start and validate [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE) using its own runbook. Configure the backend `.env` with the trusted private `LLM_BASE_URL`, `LLM_MODEL_ALIAS`, and `LLM_ENABLE_THINKING` values. The default network URL is `http://ddu-llm:8080`, alias `ddu-narrator`; only the backend worker calls that LLM endpoint. The API also joins the same private network for the separate `ddu-embed` RAG endpoint.

Run a gameplay smoke only against a disposable database that already has the selected v2 scenario release active. Set `CASES_ISOLATED_SMOKE=1` and `CASES_SMOKE_DB_NAME` to that database's name:

```sh
CASES_ISOLATED_SMOKE=1 CASES_SMOKE_DB_NAME=cases_smoke \
  CASES_SMOKE_BASE_URL=http://127.0.0.1:8000 python scripts/ai_gameplay_smoke.py
```

The script checks database identity before creating a session, waits for a real opening, submits three actions including an unexpected one, retries one submission idempotently, and verifies the committed history after reload. It prints the model and turn trace but never prints the bearer capability.

See the [backend runbook](docs/M1_RUNBOOK.md), [shared inference client contract](docs/SHARED_INFERENCE.md), and [M2 development guide](docs/M2_DEVELOPMENT.md). For the current inference deployment and synthetic two-workload tests, use the external [Local-AI-API-LITE repository](https://github.com/MarcusFunt/Local-AI-API-LITE).

## Historical RAG (CASES B3)

The additive session evidence endpoint returns only immutable passages that have a current approved review and approved rights status. The checked-in [Versailles 1919 corpus](scenarios/versailles-1919/rag/corpus.yaml) is staged and pending review; it returns `RAG_NO_EVIDENCE` until an operator approves passages. Source catalog URLs alone are not searchable evidence. Follow the [B3 RAG runbook](docs/B3_RAG_RUNBOOK.md) to validate, ingest, review, and query passages.

The endpoint is `POST /api/v1/sessions/{session_id}/evidence/search` with the existing session bearer token and `{"query":"...","top_k":5}`. Actor, pinned scenario version, phase date, and the reflection postgame view come from persisted session/scenario data. Historical RAG does not participate in the M2 turn queue or dialogue provider. The Danmarks Breve library remains separate and is not automatically eligible as game evidence; see [HISTORY_CORPUS](docs/HISTORY_CORPUS.md).

## Retrieval evaluation

The offline harness compares BM25, dense Qwen, hybrid, and hybrid plus GTE retrieval. The current dataset has 40 agent-reviewed pilot queries and an empty 0/100 held-out split. Its manifest remains `draft`; no held-out or model-comparison acceptance claim is supported yet. See [RETRIEVAL_EVALUATION](docs/RETRIEVAL_EVALUATION.md) for validation, traces, review provenance, and worker pins.

## Test the current data and commands

```sh
uv run --locked --project backend python -m app.scenarios.cli validate-all scenarios
uv run --locked --project backend python -m app.scenarios.cli verify-bundle-pins scenarios
uv run --locked --project backend rag validate scenarios/versailles-1919/rag/corpus.yaml
uv run --locked --project backend rag validate-eval backend/evaluation_data/versailles-1919/rag_cases.yaml
uv run --locked --project backend historical-eval validate --dataset backend/evaluation_data/versailles-1919
```

`historical-eval retrieve --split pilot --profiles bm25 --output <outside-the-repository>` runs the lexical pilot without contacting model services. Dense and reranked profiles require the matching live Local-AI-API-LITE workers. Keep generated traces and review packets out of Git.

## Development checks

```sh
uv sync --project backend --extra dev
uv run --project backend pytest backend/tests -q
uv run --project backend ruff check backend
uv run --project backend ruff check --select E4,E7,E9,F,I backend/tests scripts
```

GitHub Actions runs the PostgreSQL migrations and backend integration tests, validates the Compose project, and boots the backend with inference absent. GPU/model validation lives in Local-AI-API-LITE; real gameplay smoke is run separately against a declared disposable stack.
