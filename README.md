# DDU independent Docker architecture — illustrative blueprint

This package contains a detailed implementation plan and two **structurally parsed Compose examples**. It is **not** a complete runnable application: the `backend/` source tree, model weights, embedding service, STT/TTS services, actual secrets, and security hardening must be implemented according to the plan. Images have not been exercised on the target machine.

Architecture:
- `ddu-backend`: API + durable turn worker + PostgreSQL
- `ddu-inference`: initially one `llama.cpp` CUDA server; add embedding/STT/TTS as separate containers in the same project
- `ddu_inference_bus`: separate external Docker network connecting only backend worker to inference services.
- No connection to AI-Stack.

First steps (run from a Linux shell at the repository/project root **after** implementing the backend code and choosing a GGUF):

```bash
docker network create ddu_inference_bus
cp deployment/inference/.env.example deployment/inference/.env
cp deployment/backend/.env.example deployment/backend/.env
# Edit both .env files; never commit secrets.
docker compose -f deployment/inference/compose.yaml --env-file deployment/inference/.env config
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env config
```

The backend example references a build context `../../backend` and `app.workers.main`; these are implementation targets, not packaged completed code. The `turn-worker` points to `ddu-embedding:8090`, which only becomes available when the custom embedding container is implemented in milestone M5.

For operational procedures, prerequisites, risk decisions and test gates, read `IMPLEMENTATION_PLAN.md`.
