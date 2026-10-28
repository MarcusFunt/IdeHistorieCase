#!/bin/sh
# Run from the repository root with the backend and external vLLM service running.
set -eu
BACKEND=deployment/backend/compose.yaml
BACKEND_ENV=deployment/backend/.env
API_PORT=$(sed -n 's/^API_HOST_PORT=//p' "$BACKEND_ENV" | tr -d '\r')
API_PORT=${API_PORT:-8000}

printf '%s\n' "Checking backend process and database readiness..."
curl -fsS "http://127.0.0.1:$API_PORT/healthz"
printf '\n'
curl -fsS "http://127.0.0.1:$API_PORT/readyz"
printf '\n'

printf '%s\n' "Checking external vLLM health on the private Docker network..."
docker run --rm --network ddu_inference_bus curlimages/curl:8.12.1 \
  -fsS http://ddu-llm:8080/health
printf '\n'

printf '%s\n' "Waiting for worker and inference status..."
STATUS=''
attempt=0
while [ "$attempt" -lt 20 ]; do
  STATUS=$(curl -fsS "http://127.0.0.1:$API_PORT/api/v1/status")
  if printf '%s' "$STATUS" | python -c 'import json,sys; d=json.load(sys.stdin); sys.exit(not (d.get("database") is True and d.get("worker")=="running" and d.get("inference")=="ready"))'; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
printf '%s\n' "$STATUS"
printf '%s' "$STATUS" | python -c 'import json,sys; d=json.load(sys.stdin); assert d.get("database") is True and d.get("worker")=="running" and d.get("inference")=="ready", d'

printf '%s\n' "Checking the app-owned Danish structured-output diagnostic..."
docker compose --env-file "$BACKEND_ENV" -f "$BACKEND" exec -T \
  turn-worker python -m app.workers.probe

printf '%s\n' "Checking the M2 fake turn and replay without calling the model..."
python scripts/m2_fake_smoke.py --base-url "http://127.0.0.1:$API_PORT"
printf '%s\n' "Backend and external inference smoke checks completed."
