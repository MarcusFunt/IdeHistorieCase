# External inference client contract

Model serving belongs to [Local-AI-API-LITE](https://github.com/MarcusFunt/Local-AI-API-LITE), a separate repository that owns the vLLM and retrieval containers, pinned model revisions, model cache, resource limits, benchmarks, and inference lifecycle. This repository owns only the application client adapters, requests, response validation, and credentials. Narration inference is a proposal source; application code remains responsible for authoritative game state.

## Network boundary

The backend `api` and `turn-worker` join the explicitly created private Docker bridge `ddu_inference_bus`; PostgreSQL does not. The API uses the bridge for Qwen historical retrieval, and the worker uses it for the optional narrator diagnostic. An evaluation client joins only for an explicitly requested model run. Model services have no host port mappings. This bridge is suitable only for trusted local containers and is not a multi-tenant security boundary.

For local M2 development, inference is optional: the deterministic fake provider and fake-turn smoke test do not call the model. Configure `LLM_BASE_URL`, `LLM_MODEL_ALIAS`, and `LLM_ENABLE_THINKING` in `deployment/backend/.env` only when running the real diagnostic.

## HTTP contract

The application adapter sends `POST /v1/chat/completions` with its model alias, application-specific messages, strict `json_schema` response format, token and temperature limits, and `stream: false`. `LLM_ENABLE_THINKING` controls the Qwen-specific `chat_template_kwargs: {"enable_thinking": ...}` option.

The adapter checks the health response status, expected served model alias during explicit setup validation, HTTP completion status, completion envelope, finish reason, non-empty content, JSON syntax, and the app-owned Pydantic schema. Empty, truncated, malformed, or schema-invalid output is an error. Prompts and generated responses should not be written to operational logs.

## Historical retrieval contract

The Cases API calls Local-AI-API-LITE `POST /v1/embeddings` at `ddu-embed` using `input_type: "query"` or `"document"`; the query instruction is applied by the worker. The adapter verifies the `ddu-embed` alias, Qwen revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, the 1,024-dimensional normalized float32 capability, the `qwen3-question-to-passages-en-v1` instruction version, and a batch limit of eight. Stored vector identity includes the pinned revision.

Optional reranking uses `POST /v1/rerank` at `ddu-rerank`, with GTE revision `8215cf04918ba6f7b6a62bb44238ce2953d8831c` and custom-code revision `40ced75c3017eb27626c9d4ea981bde21a2662f4`. Cases validates raw ranking capability and bounded candidate limits; raw scores are converted monotonically for the existing policy threshold and are not probabilities. Reranking is disabled by default. See the [B3 runbook](B3_RAG_RUNBOOK.md) for operational commands and the [retrieval evaluation guide](RETRIEVAL_EVALUATION.md) for benchmark use.

The app-specific probe can be run from the backend worker container after both services are running:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec -T \
  turn-worker python -m app.workers.probe
```

For complete deployment ownership, GPU validation, concurrency measurements, and the synthetic two-workload test, use the Local-AI-API-LITE repository. This repository no longer carries a copied infrastructure smoke client.
