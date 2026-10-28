# IdeHistorieCase — independently containerized history game

**Implemented in milestone M1:** FastAPI liveness/readiness, PostgreSQL 18 with Alembic migration, independent inference-health worker, a real Danish structured-generation diagnostic, separate Docker Compose projects, and automated unit/integration tests.

**Not yet implemented:** Godot game, sessions, turn queue, deterministic gameplay, RAG, embedding/STT/TTS containers, and classroom authentication. The code is not yet a playable game.

## Deployments

- **ddu-inference**: one independent llama.cpp CUDA container for a user-supplied, compatible instruct/chat GGUF file.
- **ddu-backend**: PostgreSQL, a one-shot migration service, FastAPI, and an inference-health worker.
- **ddu_inference_bus**: external Docker network between the backend worker and inference. Neither database nor API is on this shared network.

The existing AI-Stack is not required. Physical GPU VRAM is still shared if it happens to run concurrently.

## Requirements

Docker Engine with Compose v2, NVIDIA GPU runtime / Container Toolkit, a Docker-host-accessible folder containing a suitable GGUF model, and enough GPU VRAM to load it. Model weights are not included or automatically downloaded.

Run from repository root:

```bash
docker network inspect ddu_inference_bus >/dev/null 2>&1 || docker network create ddu_inference_bus
cp deployment/inference/.env.example deployment/inference/.env
cp deployment/backend/.env.example deployment/backend/.env
```

**Edit the new .env files before launching.** Set a long random POSTGRES_PASSWORD, LLM_MODELS_DIR to the absolute directory containing your GGUF, and LLM_GGUF_FILE to its exact filename. Local .env files are ignored by Git.

```bash
docker compose -f deployment/inference/compose.yaml --env-file deployment/inference/.env config --quiet
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env config --quiet

# These two commands are independent of each other and of AI-Stack.
docker compose -f deployment/inference/compose.yaml --env-file deployment/inference/.env up -d
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env up -d --build

curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/api/v1/status

# From an ephemeral container on the shared, non-published inference network:
docker run --rm --network ddu_inference_bus curlimages/curl:8.12.1 -fsS http://ddu-llm:8080/health

# Verify the worker-to-LLM path with real schema-constrained Danish generation:
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec -T turn-worker python -m app.workers.probe
```

A successful probe prints strict JSON with `language: "da"` and a nonempty `reply`; it exits nonzero if the model is unavailable or its output is invalid. This does not prove historical accuracy or fluency.


### Optional verified starter model

For a concrete first test on the RTX 3060, a compact Qwen3.5-4B Q4_K_M GGUF is available from Unsloth (Apache-2.0 model license). Download it deliberately on your Docker host rather than during container startup:

```bash
bash scripts/download_m1_model.sh /srv/ddu-models
```

The script pins a Hugging Face commit and verifies a published SHA-256. It downloads roughly 2.74 GB and is optional; any compatible GGUF can be selected in the inference environment file. The probe requests that the model's chat template disable extended thinking, leaving its output budget for the short JSON response. The downloaded weights are outside Git and separate from AI-Stack.

## Independent failure test

Stop **only inference**:

```bash
docker compose -f deployment/inference/compose.yaml --env-file deployment/inference/.env stop
```

After the worker poll (default 10 seconds), the backend remains operational: `/healthz` and `/readyz` should return 200 while `/api/v1/status` reports inference `degraded`. Restart inference using its Compose project, with no backend restart.

Do not routinely use `docker compose down -v` because it can delete the database volume. For detailed setup, tests, troubleshooting and security boundaries see [M1 runbook](docs/M1_RUNBOOK.md), then [implementation plan](IMPLEMENTATION_PLAN.md).

For the shared HTTP contract and an independent two-workload client, see [Connecting an independent backend to shared inference](docs/SHARED_INFERENCE.md).

## Development tests

```bash
python -m pip install -e "./backend[dev]"
cd backend
pytest -q
ruff check .
```

A separate PostgreSQL integration test is enabled with `DDU_TEST_DB=1` after `alembic upgrade head`. GitHub Actions provisions PostgreSQL, runs migrations and tests, and checks both Compose configurations. Docker runtime tests with a real model/GPU must be performed on the target host.
