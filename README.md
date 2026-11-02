# IdeHistorieCase — deterministic backend and historical retrieval prototype

The repository contains the M2 durable backend, authored Versailles 1919 scenario, B3 session-scoped historical RAG, an offline retrieval-evaluation harness, and an isolated Danmarks Breve source-library importer. The game turn worker still uses a deterministic fake provider. This is **not** a complete playable game or a classroom-ready deployment.

## Service ownership

| Part | Owner | Current role |
|---|---|---|
| History API, database, migrations, durable queue and fake provider | This repository (`ddu-backend`) | Application state and deterministic M2 development |
| vLLM, embedding, reranking, model cache, checkpoints, GPU and serving limits | [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE) | Independently owned inference services |
| History-specific model request and Danish diagnostic schema | This repository | A narrow client adapter used by the worker monitor and explicit probe |
| Scenario-approved historical evidence and retrieval policy | This repository | Session-scoped evidence search; the checked-in Versailles excerpts remain unapproved |
| Retrieval evaluation and authentic history library | This repository | Offline benchmark and a separate, immutable `history_authentic` schema |

`turn-worker`, the API, and the optional evaluation client join the external Docker network `ddu_inference_bus`. The API uses it for historical retrieval; `turn-worker` remains on the deterministic fake provider. PostgreSQL does not join that network. Cases does not start or change model services. The llama.cpp/GGUF runs recorded in [M1.5 validation](docs/M1_5_VALIDATION.md) are historical evidence; they are not the current inference deployment.

## Run M2 locally without a model

Requirements: Docker Compose v2 and Python 3.13 for local test commands. The backend works with no inference service running.

```sh
docker network inspect ddu_inference_bus >/dev/null 2>&1 || docker network create ddu_inference_bus
cp deployment/backend/.env.example deployment/backend/.env
```

Edit `deployment/backend/.env` and set a long random `POSTGRES_PASSWORD`. Fake turns work without inference. Historical RAG requires a reachable `ddu-embed` service on `ddu_inference_bus`; its URL and model ID can stay at their defaults when the shared deployment uses those names. Then start the backend project:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env config --quiet
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env up -d --build
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/api/v1/status
python scripts/m2_fake_smoke.py --base-url http://127.0.0.1:8000
```

The host API port defaults to `8000`. If another local service uses it, set `API_HOST_PORT=18081` (or another free port) in `deployment/backend/.env` and use that port for the health checks and smoke command. Compose binds the API to `127.0.0.1` in either case.

`/healthz` is process liveness and `/readyz` checks PostgreSQL. Neither depends on inference. With no model service, `/api/v1/status` reports inference as degraded while the fake turn and replay still work. The smoke script writes one synthetic session to the configured database and prints no bearer capability. The API is bound to host loopback; Compose publishes no database port.

Do not use `docker compose down -v` for routine shutdown; it can remove the PostgreSQL volume.

## Optional external model diagnostic

For a real vLLM endpoint, start and validate [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE) using its own runbook. Configure the backend `.env` with the trusted private `LLM_BASE_URL`, `LLM_MODEL_ALIAS`, and `LLM_ENABLE_THINKING` values. The default network URL is `http://ddu-llm:8080`, alias `ddu-narrator`; only the backend worker calls that LLM endpoint. The API also joins the same private network for the separate `ddu-embed` RAG endpoint. With both services running, use:

```sh
bash scripts/m1_smoke.sh
```

This checks backend health, worker/model status, the existing history-specific structured Danish probe, and the M2 fake turn. The real model request is a diagnostic only; the M2 turn provider remains deterministic and fake.

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
uv run --project backend ruff check --select E4,E7,E9,F,I scripts/m2_fake_smoke.py backend/tests
```

GitHub Actions runs the PostgreSQL migrations and full M2 integration tests, validates only the backend Compose project, and boots the fake backend with inference absent. GPU/model validation lives in Local-AI-API-LITE and does not run in application CI.
