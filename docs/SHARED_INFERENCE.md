# External inference client contract

Model serving belongs to [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE), a separate repository that owns the vLLM container, pinned model revision, GPU settings, benchmarks, and inference lifecycle. This repository owns only the history application's client adapter, request, Danish response schema, validation, and credentials. Inference is a proposal source; application code remains responsible for authoritative game state.

## Network boundary

The external service and the backend `turn-worker` share the explicitly created private Docker bridge `ddu_inference_bus`. The API and PostgreSQL do not join that network. The model API has no host port mapping. This bridge is suitable only for trusted local containers and is not a multi-tenant security boundary.

For local M2 development, inference is optional: the deterministic fake provider and fake-turn smoke test do not call the model. Configure `LLM_BASE_URL`, `LLM_MODEL_ALIAS`, and `LLM_ENABLE_THINKING` in `deployment/backend/.env` only when running the real diagnostic.

## HTTP contract

The application adapter sends `POST /v1/chat/completions` with its model alias, application-specific messages, strict `json_schema` response format, token and temperature limits, and `stream: false`. `LLM_ENABLE_THINKING` controls the Qwen-specific `chat_template_kwargs: {"enable_thinking": ...}` option.

The adapter checks the health response status, expected served model alias during explicit setup validation, HTTP completion status, completion envelope, finish reason, non-empty content, JSON syntax, and the app-owned Pydantic schema. Empty, truncated, malformed, or schema-invalid output is an error. Prompts and generated responses should not be written to operational logs.

The app-specific probe can be run from the backend worker container after both services are running:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec -T \
  turn-worker python -m app.workers.probe
```

For complete deployment ownership, GPU validation, concurrency measurements, and the synthetic two-workload test, use the Local-AI-API-LITE repository. This repository no longer carries a copied infrastructure smoke client.
