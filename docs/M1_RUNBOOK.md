# M1 runbook: independent model inference + operational backend

## Scope

The first milestone implements two Compose projects, a llama.cpp CUDA server definition, PostgreSQL 18, migration, health/readiness API, worker heartbeat, and an actual Danish schema-constrained generation probe. The existing AI-Stack is not referenced. The game, sessions, RAG and audio are intentionally deferred.

This source has not yet been executed against your NVIDIA host. A real GGUF and GPU smoke test remain required before claiming that M1 has passed on the target machine.

## Setup

Follow [README](../README.md). Choose a compatible quantized *instruction-tuned* GGUF and confirm its chat template works under the selected llama.cpp image. The model directory must exist on the **Docker daemon's** host. Record the model's SHA256, license, quantization and version; don't ship model weights inside the repository.

Suggested starting settings are `LLM_CTX_SIZE=4096`, `LLM_PARALLEL=1`, and `LLM_GPU_LAYERS=99`. If VRAM is insufficient, lower context, model size or GPU layers; these are not guaranteed optimal values. Avoid GPU contention with existing AI-Stack jobs during first tests.

The API is bound to host loopback, and only the backend worker joins the shared inference network. The worker polls `http://ddu-llm:8080/health` periodically and writes its result to PostgreSQL. The API reads that status from PostgreSQL rather than joining the inference network.

## M1 acceptance gates

1. Create/inspect the `ddu_inference_bus` network without modifying any AI-Stack network.
2. Both `docker compose ... config --quiet` commands succeed.
3. Start `ddu-inference` while `ddu-backend` is stopped; the model's `/health` eventually returns status 200 and JSON `{"status":"ok"}`.
4. Start backend; migration exits successfully, API and worker stay up; `/healthz` and `/readyz` return 200.
5. `/api/v1/status` reports worker running and inference ready after heartbeat.
6. Execute `python -m app.workers.probe` inside the worker container; inspect its schema-valid Danish output.
7. Stop inference only: backend stays healthy while inference becomes degraded after a poll.
8. Restart inference only: worker status recovers without backend restart.
9. CI passes Python tests, database migration/integration tests and Compose validation.
10. Verify `docker compose ps` / port listing: neither PostgreSQL nor inference publishes a host port.

## Troubleshooting

- **GPU errors:** Confirm `nvidia-smi` on the host and that NVIDIA Container Toolkit grants GPU access to Docker.
- **Container starts but model unhealthy:** llama.cpp `/health` can return 503 while loading. Check inference logs and model file paths.
- **Worker degraded:** Verify both Compose deployments attached the external network and `ddu-llm` resolves from a diagnostic container on it.
- **Database migration failed:** Read `migrate` logs, verify `postgres` readiness, correct credentials, and the PostgreSQL volume. Note changing POSTGRES_PASSWORD in env does not change a previously initialized database user's password.
- **Probe rejects JSON:** Check model instruction-tuning/chat template and server's `response_format` schema support. Do not silently replace malformed output with success.
- **VRAM exhausted:** Shut down competing workloads safely and adjust model/quantization/offload/context.

## Safety boundaries and remaining work

The `/api/v1/status` endpoint is intentionally unauthenticated but only bound to localhost in M1. Do not expose it publicly; implement access controls before classroom deployment. The database application role is also the migration role in M1: split least-privileged runtime credentials from the migration account before production. Add database backup/recovery, durable turns, sessions and authentication in the next milestone.

Docker image tags and Python dependency version ranges are **not** immutable. Once tested, pin digests and lock dependencies. Document real cold-start time, p95 response time, inference GPU memory consumption and actual model checksum.

Relevant upstream documentation:
- https://docs.docker.com/compose/how-tos/networking/
- https://docs.docker.com/compose/how-tos/gpu-support/
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
