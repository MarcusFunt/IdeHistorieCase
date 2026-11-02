# CASES B3 Historical RAG Runbook

## Scope

Historical retrieval is an additive, session-scoped API. It does not call the dialogue provider and does not change M2 turn submission, queueing, event validation, or replay. Every result is pinned to the session's immutable scenario version. Source catalog entries remain references; only ingested passage text with current review and rights approval can be returned.

The first corpus is `scenarios/versailles-1919/rag/corpus.yaml`. Its four excerpts begin with no approval rows, so the expected initial response is `RAG_NO_EVIDENCE`.

## Host requirements

- PostgreSQL 18 with pgvector available to the database user; migration `0005_historical_rag` installs the shared `vector` extension and creates a 1024-dimensional vector column plus HNSW index.
- The API container must be attached to the existing external `ddu_inference_bus` network and be able to reach `RAG_EMBED_BASE_URL`.
- Embeddings use Local-AI-API-LITE `POST /v1/embeddings` with API model `ddu-embed`, `input_type: "document"` for passages, and `input_type: "query"` for search questions. The Qwen worker applies `Instruct: Given a question, retrieve relevant passages that answer the question` to queries only; its advertised instruction version is `qwen3-question-to-passages-en-v1`. The adapter reads `/v1/models` and fails closed unless the API alias, Qwen revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, 1024 dimensions, and instruction version match. It batches at most eight inputs per request. `RAG_EMBED_MODEL_ID` records the pinned model identity in stored vectors: `Qwen/Qwen3-Embedding-0.6B@97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`.
- Reranking is optional. To enable it, set `RAG_RERANK_ENABLED=true` and `RAG_RERANK_BASE_URL` to Local-AI-API-LITE. The adapter uses `POST /v1/rerank` with API model `ddu-rerank`, query, and documents; it verifies the GTE revision `8215cf04918ba6f7b6a62bb44238ce2953d8831c` and reviewed code revision `40ced75c3017eb27626c9d4ea981bde21a2662f4` from `/v1/models`. GTE returns raw ranking signals, which Cases maps through a monotonic sigmoid into the existing `[0,1]` threshold range; this is a bounded ranking score, not a calibrated probability. Requests are batched at no more than 20 passages. Stored model identity is `Alibaba-NLP/gte-multilingual-reranker-base@8215cf04918ba6f7b6a62bb44238ce2953d8831c+code@40ced75c3017eb27626c9d4ea981bde21a2662f4`.
- A passage qualifies when any configured lexical, dense, or rerank threshold passes. With reranking enabled, a low rerank score changes ordering but does not veto a candidate that passed lexical or dense admission; the approval, rights, actor/date eligibility checks and final revalidation still apply. Treat score thresholds as retrieval tuning, never as access control.
- Import the exact authored scenario version before ingesting its corpus. The corpus phase-date map must cover every phase in that version. Importing or activating another scenario version does not change existing sessions' pins.
- A human reviewer must verify each excerpt against its source anchor, document date, in-world availability, actor visibility, confidentiality, and deployment rights before appending approval. The corpus's rights basis is review context, not a rights decision.

The API's `/readyz` endpoint checks PostgreSQL and intentionally does not require an inference service. RAG queries require `ddu-embed`; a missing or malformed model response fails the evidence request. No inference service is started or changed by this repository.

## Deploy and stage

Copy the example environment file, configure the database password and RAG service URLs, then start the backend as documented in the root README. Keep the pinned model identity values in `RAG_EMBED_MODEL_ID` and `RAG_RERANK_MODEL_ID` unless a full re-embedding/re-evaluation is planned. The API receives the scenario directory as a read-only mount at `/srv/scenarios` for operator commands.

Validate the scenario, corpus, and labeled evaluation set:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.scenarios.cli validate /srv/scenarios/versailles-1919/2.5.yaml
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.rag.cli validate /srv/scenarios/versailles-1919/rag/corpus.yaml
```

Import the immutable scenario and activate it for new sessions when appropriate:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.scenarios.cli import /srv/scenarios/versailles-1919/2.5.yaml
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.scenarios.cli activate versailles-1919 --version 2.5
```

Ingest the bounded corpus. Ingestion calls the embedding service and writes immutable passages and embeddings; it never approves them.

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.rag.cli ingest /srv/scenarios/versailles-1919/rag/corpus.yaml
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.rag.cli list versailles-1919 2.5
```

Each corpus is limited to 32 passages, 4,000 characters per passage, 64,000 total passage characters, and 256 KiB of YAML. Repeating the same manifest is idempotent. A changed corpus for an imported scenario version is rejected; author a new scenario version to change passage content or provenance.

## Review and rights approval

Inspect each staged passage's exact source, anchor, hash, date fields, source policy, actor list, and visibility scope. Append a separate decision for each passage only after that review:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env exec api \
  python -m app.rag.cli review versailles-1919 2.5 fourteen_points_open_covenants \
  --reviewer "reviewer-id" --decision approved --rights-status approved \
  --notes "Source anchor, transcript wording, and reuse status checked."
```

Valid decisions are `approved`, `rejected`, and `revoked`. Rights states are `approved`, `pending`, `denied`, and `restricted`. Retrieval requires the latest review to say both `approved`; a missing decision is pending. A new review appends a higher revision and never changes the passage row. Revoke or restrict access by appending a new decision; the final retrieval check reads the latest revision again before exposing a passage.

Do not approve the confidential R11 minutes for actors outside their source policy. The R11A Chinese delegation submission is a separate source passage and actor scope. The R03 1921 payment schedule is postgame-only and must not appear during 1919 gameplay.

## Query the session endpoint

```http
POST /api/v1/sessions/{session_id}/evidence/search
Authorization: Bearer <session capability>
Content-Type: application/json

{"query":"What did Wilson propose about open covenants?","top_k":5}
```

The server derives the pinned scenario version and actor from the session and maps its current phase to the imported phase-world-date policy. In the `reflection` phase it uses the postgame evidence mode. Clients cannot supply a role, date, or retrieval mode. The success body is `{ "status": "ok", "passages": [...] }`; an empty, irrelevant, unapproved, rights-pending, future, unauthorized, or missing-corpus result is `{ "status": "RAG_NO_EVIDENCE", "passages": [] }`.

Returned passage metadata includes source identifier and anchor, exact passage text and hash, document and availability dates, scenario version provenance, review revision, and ranking scores. A source URL is provenance only; it never substitutes for returned approved passage text.

## Evaluation and CI

The labeled set is `backend/evaluation_data/versailles-1919/rag_cases.yaml`; it covers answerable cases, the 1921 date leak, confidential minutes, pending rights, actor visibility, and an unsupported exact-amount claim.

```sh
uv run --locked --project backend python -m app.rag.cli validate-eval \
  backend/evaluation_data/versailles-1919/rag_cases.yaml
uv run --locked --project backend pytest -q backend/tests/test_rag_*.py
uv run --locked --project backend ruff check backend
```

CI validates the corpus and evaluation manifest, runs injected-fake unit tests, applies migrations to PostgreSQL, and tests lexical/vector search, scenario and eligibility filters, approval revocation, authenticated API behavior, and append-only storage. No real dialogue model is used by these checks.

## Configuration

The API accepts these environment settings: `RAG_EMBED_BASE_URL`, `RAG_EMBED_MODEL_ID`, `RAG_EMBED_TIMEOUT_SECONDS`, `RAG_CANDIDATE_LIMIT`, `RAG_RRF_K`, `RAG_MIN_LEXICAL_SCORE`, `RAG_MIN_DENSE_SIMILARITY`, and `RAG_MIN_RERANK_SCORE`. Reranking uses `RAG_RERANK_ENABLED`, `RAG_RERANK_BASE_URL`, `RAG_RERANK_MODEL_ID`, and `RAG_RERANK_TIMEOUT_SECONDS`. Candidate count is bounded to 2–100 and exposed `top_k` to 1–10.

## Known limits

- The checked-in corpus is small and staged; no passage is preapproved.
- Rights and source accuracy approval remain an operator responsibility. The system stores review decisions but does not determine legal status.
- Relevance thresholds are configurable starting values and require evaluation against the deployed embedding model and real classroom queries.
- The retriever returns evidence passages only. It does not generate explanations or bind free-form model claims to citations.
- This change does not alter or configure Local-AI-API-LITE. The host must provide a compatible private embedding endpoint.
