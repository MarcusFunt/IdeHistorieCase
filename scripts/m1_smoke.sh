#!/bin/sh
# Run from the repository root after both Compose deployments are up.
set -eu
BACKEND=deployment/backend/compose.yaml
BACKEND_ENV=deployment/backend/.env
API_PORT=$(sed -n 's/^API_HOST_PORT=//p' "$BACKEND_ENV")
API_PORT=${API_PORT:-8000}

echo "Checking backend process and DB readiness..."
curl -fsS "http://127.0.0.1:$API_PORT/healthz"
printf '\n'
curl -fsS "http://127.0.0.1:$API_PORT/readyz"
printf '\n'
curl -fsS "http://127.0.0.1:$API_PORT/api/v1/status"
printf '\n'

echo "Checking inference health over the private Docker network..."
docker run --rm --network ddu_inference_bus curlimages/curl:8.12.1 \
  -fsS http://ddu-llm:8080/health
printf '\n'

echo "Checking a genuine Danish schema-validated inference response..."
docker compose --env-file "$BACKEND_ENV" -f "$BACKEND" exec -T \
  turn-worker python -m app.workers.probe
printf '\nM1 smoke test completed.\n'
