# CASES B3 Historical RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add version-pinned, rights-reviewed historical RAG for Versailles 1919 without changing M2 turn processing or Local-AI-API-LITE.

**Architecture:** Append-only passage and review records feed a bounded PostgreSQL lexical + pgvector dense retriever. A session-scoped API derives actor/date from persisted scenario state, applies eligibility before search and before response, and returns an explicit no-evidence code when appropriate.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, SQLAlchemy async, Alembic, PostgreSQL full text, pgvector, httpx, pytest, Ruff, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-02-cases-b3-historical-rag-design.md`

## Global Constraints

- Preserve M2 durable turn queue, scenario versioning, deterministic engine, existing REST contracts, and Local-AI-API-LITE separation.
- Do not integrate the real dialogue provider, build a frontend, or modify Local-AI-API-LITE in this PR.
- Source catalog URLs alone are never approved passages.
- Passage ingestion is bounded; only separately approved and rights-cleared passages can be returned.
- Model clients and retrieval components accept injected clients/repositories for isolated tests.

## Review Focus

- Future or postgame passages do not appear before their in-world availability; test the 1921 schedule at a 1919 date.
- Confidential or actor-limited passages do not appear to an unauthorized session actor; test Chinese access to R11.
- Staged or rights-pending records do not appear even when they match; test matching pending passage.
- Catalog entries containing only a URL cannot be ingested as evidence; test corpus validation.
- Unsupported queries produce `RAG_NO_EVIDENCE`; test both service and API contract.

---

### Task 1: Passage store and append-only approval lifecycle

**Files:**
- Create: `backend/migrations/versions/0005_historical_rag.py`
- Modify: `backend/app/db/models.py`
- Test: `backend/tests/test_rag_migration.py`

**Interfaces:**
- Store immutable `HistoricalPassage`, `RagScenarioPolicy`, and `HistoricalPassageEmbedding` rows.
- Store append-only `HistoricalPassageReview` decisions; the latest decision controls approval and rights eligibility.
- Use 1024-dimensional pgvector embeddings, keyed to passage hash and model ID.

- [x] Add migration tests for pgvector extension/table/index creation, immutable passage and decision guards, and no changes to M2 tables.
- [x] Run the migration test against PostgreSQL and observe expected failures before migration implementation.
- [x] Add tables, FKs, constraints, GIN lexical and HNSW cosine indexes, immutable triggers, and reversible migration.
- [x] Run migration tests and Alembic upgrade/downgrade checks.

### Task 2: Bounded corpus validation, staging, and review CLI

**Files:**
- Create: `backend/app/rag/__init__.py`, `schemas.py`, `corpus.py`, `repository.py`, `cli.py`
- Create: `backend/tests/test_rag_corpus.py`, `backend/tests/test_rag_workflow.py`
- Modify: `backend/pyproject.toml`, `backend/uv.lock`

**Interfaces:**
- `load_corpus(path) -> RagCorpus` validates file, passage count/size, exact hash, source refs, scenario identity, dates, actors, and required anchors.
- `ingest_corpus(db, corpus, embedder) -> IngestSummary` is idempotent, stages records, and refuses immutable ID/hash conflicts.
- `record_review(db, passage_id, reviewer, decision, rights_status, notes)` appends a decision; no status mutation occurs on passage rows.
- CLI commands: `validate`, `ingest`, `review`, `list`.

- [x] Write failing tests for URL-only records, oversized corpora, bad hashes, duplicate IDs, repeat ingestion, immutable conflicts, pending defaults, and review decisions.
- [x] Run tests to confirm failures are caused by the missing workflow.
- [x] Implement validation, hash verification, bounded embedding calls, idempotent staging, append-only reviews, and CLI output.
- [x] Run workflow tests and formatting/lint for the new package.

### Task 3: Eligibility policy, TEI clients, hybrid retrieval, and no-evidence behavior

**Files:**
- Create: `backend/app/rag/policy.py`, `clients.py`, `retrieval.py`
- Create: `backend/tests/test_rag_policy.py`, `backend/tests/test_rag_clients.py`, `backend/tests/test_rag_retrieval.py`
- Modify: `backend/app/core/settings.py`, `backend/app/db/` only if query helpers need extraction

**Interfaces:**
- `EvidenceContext` pins scenario-version UUID, slug/version, actor ID, world date, and mode.
- `is_eligible(passage, context, decision, source_policy) -> bool` fails closed.
- `EmbeddingClient.embed_query(text) -> list[float]`; `RerankClient.rerank(query, passages) -> list[float]` are injectable protocols.
- `RagSearchService.search(query, context, top_k) -> RagSearchResult` returns ranked passage metadata or code `RAG_NO_EVIDENCE`.

- [x] Write failing tests for temporal/actor/rights gates, malformed model vectors/scores, hybrid fusion, optional reranking, threshold abstention, and post-retrieval approval revocation.
- [x] Run those tests to confirm the expected missing behavior.
- [x] Implement TEI adapters, PostgreSQL prefilter queries, reciprocal-rank fusion, optional rerank, relevance thresholds, and final eligibility recheck.
- [x] Run unit tests using injected fake clients and repositories.

### Task 4: Authenticated session evidence endpoint and API configuration

**Files:**
- Create: `backend/app/rag/routes.py`, `backend/tests/test_rag_api.py`
- Modify: `backend/app/main.py`, `backend/app/core/settings.py`, `deployment/backend/compose.yaml`, `deployment/backend/.env.example`

**Interfaces:**
- `POST /api/v1/sessions/{session_id}/evidence/search` accepts `{query, top_k}` and existing bearer capability.
- Session pin and role determine scenario/version/actor; `RagScenarioPolicy.phase_world_dates` maps current phase to date.
- Response is `{status: "ok", passages: [...]}` or `{status: "RAG_NO_EVIDENCE", passages: []}`.

- [x] Test auth, pinned-version resolution, phase date, actor restrictions, additive response shape, and no-evidence code with injected RAG service.
- [x] Run API tests and observe missing route behavior.
- [x] Add the route and client configuration; connect only the API service to the already external `ddu_inference_bus` for the injected embedding service.
- [x] Run API and existing REST contract tests.

### Task 5: Curated Versailles corpus, labeled evaluation cases, runbooks, and CI

**Files:**
- Create: `scenarios/versailles-1919/rag/corpus.yaml`
- Create: `backend/evaluation_data/versailles-1919/rag_cases.yaml`
- Create: `backend/tests/test_rag_evaluation_set.py`, `backend/tests/test_rag_postgres_integration.py`
- Create: `docs/B3_RAG_RUNBOOK.md`
- Modify: `.github/workflows/backend-ci.yml`, `README.md`

- [x] Add exact source-anchored FRUS passages in staged/pending status and evaluation labels for answerable, date-leak, confidentiality, rights, and unsupported claims.
- [x] Test corpus and evaluation schema, labels, hashes, and expected abstention cases.
- [x] Add PostgreSQL tests for lexical/dense search, scenario filtering, eligibility, and final recheck with deterministic fake embeddings.
- [x] Add CI validation for corpus/evaluation and execute PostgreSQL migration/integration checks.
- [x] Document service endpoint/model requirements, approval workflow, startup, health checks, operator commands, and known limitations.
- [x] Run full CI-equivalent verification and confirm no files changed in Local-AI-API-LITE.
