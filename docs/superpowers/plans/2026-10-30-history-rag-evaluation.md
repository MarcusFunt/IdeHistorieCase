# Historical RAG Evaluation Implementation Plan

> **For agentic workers:** Implement this plan task by task in the current session. The user approved the revised design and asked to continue.

**Goal:** Build a retrieval-only historical evaluation harness that compares BM25, real Qwen dense retrieval, hybrid RRF, and real GTE reranking over exact source passages, with quality metrics on a reviewed sample and a small blind human answer study as a separate follow-on stage.

**Architecture:** Keep evaluation and its source corpus in IdeHistorieCase, separate from scenarios and the production worker. Call the existing Local-AI-API-LITE embedding/reranking APIs directly; run BM25, vector ranking, fusion, metrics, pooling, and reporting locally in a bounded client container. Host-side measurement stays outside the client and never starts or stops services.

**Tech Stack:** Python 3.12+, existing httpx, PyYAML, Pydantic v2, FastAPI-independent CLI package, JSONL/JSON, PowerShell host sampler, Docker Compose/CLI.

**Spec:** docs/superpowers/specs/2026-10-30-retrieval-evaluation-design.md

## Global Constraints

- Stage 1 is retrieval-only and does not require a generator or AI judge.
- Preserve Local-AI-API-LITE retrieval services; use ddu-embed:8080/v1/embeddings and ddu-rerank:8080/v1/rerank.
- Verify /health, /ready, and /v1/models identity before real-model runs; pin Qwen revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 and GTE revision 8215cf04918ba6f7b6a62bb44238ce2953d8831c.
- Data remains outside scenario bundles and citation_ready remains false.
- Target a 40-query pilot and 100-query held-out set; compute graded scores over human- or agent-reviewed query samples and report coverage without requiring exhaustive review.
- Separate retrospective QA from actor/time simulation and keep document creation, publication, and actor knowledge availability distinct.
- No evaluation command may use PostgreSQL, Docker socket, or start/stop inference services.
- Do not add or run tests in this implementation turn; the higher-level session instruction limits testing to explicit user requests.

## Review Focus

- Incorrect endpoint model identity or revision must fail before retrieval; the real-model adapter task owns validation.
- Partial, unordered, non-finite, or wrong-dimension embeddings must fail closed; the adapter task owns validation.
- Duplicate or tie-scored passages must have deterministic ordering; the retrieval task owns it.
- Queries with incomplete judgments must report coverage and stay out of final graded aggregates; pooling/metrics task owns it.
- Simulation queries must filter by actor/time knowledge availability while retrospective QA remains unrestricted; dataset/retrieval task owns it.

---

### Task 1: Evaluation data contracts and source fixture

**Files:**
- Create: backend/app/evaluation/__init__.py
- Create: backend/app/evaluation/schemas.py
- Create: backend/app/evaluation/dataset.py
- Create: backend/evaluation_data/versailles-1919/manifest.json
- Create: backend/evaluation_data/versailles-1919/sources.jsonl
- Create: backend/evaluation_data/versailles-1919/passages.jsonl
- Create: backend/evaluation_data/versailles-1919/pilot_queries.jsonl
- Create: backend/evaluation_data/versailles-1919/heldout_queries.jsonl
- Create: backend/evaluation_data/versailles-1919/judgments.jsonl

**Interfaces:**
- Produces load_dataset(root: Path, split: str) -> EvaluationDataset and validate_dataset(root: Path) -> ValidationSummary.
- Query schema carries query_id, split, language, text, topic, kind, answerable, actor_id, as_of, source_creation_cutoff, publication_cutoff, and actor knowledge rules.
- Passage schema carries exact text/hash, source ID, anchor, source creation/publication dates, and actor knowledge availability as separate fields.
- Judgment schema carries relevance 0–3 and zero or more required evidence groups with alternate supporting passage IDs.
- Manifest records target/actual counts, draft/review status, source/query/judgment hashes, and the pinned model revisions.

- [ ] Define strict Pydantic v2 schemas. Reject unknown fields, invalid dates, duplicate IDs, missing references, invalid language/split/kind, malformed SHA-256, and grades outside 0–3.
- [ ] Add a source-backed initial fixture with exact passages, attribution, URLs, and anchors. Seed pilot query topics from the existing 12 history goldens; preserve review state as provenance and allow selective human or agent review.
- [ ] Add separate query and judgment JSONL files. Record 40/100 as target counts and actual counts separately; do not manufacture reviewed labels for the held-out set.
- [ ] Implement dataset load/validation and temporal passage filtering. Retrospective QA ignores actor-knowledge filtering; simulation requires actor, as-of time, and availability rules.

### Task 2: Existing LITE model clients

**Files:**
- Create: backend/app/evaluation/model_clients.py
- Modify: backend/pyproject.toml (evaluation console entry point and package list)

**Interfaces:**
- LiteEmbeddingClient(base_url: str, expected_revision: str, http: httpx.Client) with preflight(), embed_queries(texts), and embed_documents(texts).
- LiteRerankerClient(base_url: str, expected_revision: str, http: httpx.Client) with preflight() and rerank(query, documents, top_n).
- Results retain model alias, revision, response token usage, and elapsed endpoint time.

- [ ] Probe /health, /ready, and /v1/models. Require alias ddu-embed or ddu-rerank, task, and exact expected model revision.
- [ ] Call the actual embedding and reranking endpoints. Batch embeddings at most 8 and reranking candidates at most 20; preserve input order/index mapping and reject malformed/non-finite/wrong-dimension results.
- [ ] Apply input_type=query/document so the worker owns the Qwen query instruction. Never fake service results in real-model mode.

### Task 3: Retrieval profiles and ranking metrics

**Files:**
- Create: backend/app/evaluation/retrieval.py
- Create: backend/app/evaluation/metrics.py

**Interfaces:**
- run_retrieval(dataset, split, profiles, embedding_client, reranker_client, timings) -> list[QueryTrace].
- Profiles are exactly bm25, dense, hybrid, and hybrid_rerank.
- Metric functions calculate Recall@5, MRR@10, graded nDCG@10, and required evidence coverage@5/@10. RRF uses a manifest-pinned constant and deterministic tie breaking.

- [ ] Implement deterministic bilingual BM25 tokenizer/scoring with configured parameters.
- [ ] Build exact cosine rankings over normalized Qwen vectors; batch corpus embeddings in groups of eight and report index-build time apart from query time.
- [ ] Implement fixed reciprocal-rank fusion and hybrid candidate-pool GTE reranking. Preserve both pre- and post-rerank rankings.
- [ ] Calculate per-query metrics and paired rerank movement. Exclude unjudged passages from final graded metrics and report judgment coverage.
- [ ] Add bootstrap confidence intervals and aggregate by split, language, topic, and query kind.

### Task 4: Pooling, run artifacts, and CLI

**Files:**
- Create: backend/app/evaluation/pooling.py
- Create: backend/app/evaluation/runner.py
- Create: backend/app/evaluation/cli.py
- Modify: backend/pyproject.toml
- Modify: backend/.gitignore

**Interfaces:**
- CLI: historical-eval validate, historical-eval retrieve, historical-eval pool, historical-eval import-judgments, and historical-eval report.
- Each run writes run.json, traces.jsonl, pooled_candidates.jsonl when requested, and summary.json/summary.md into an ignored output directory.
- Traces include exact passage IDs/text, ranks/scores, temporal filters, model identity/revisions, endpoint durations, usage, data hashes, location/cache labels, and timestamps.

- [ ] Implement deterministic pool de-duplication of top-10 results across all profiles and randomized candidate IDs that hide profile/rank from reviewers.
- [ ] Validate imported 0–3 labels and evidence-group links, map reviewer IDs back to passages, and record judged/unjudged coverage.
- [ ] Implement CLI validation and reporting; retrieve fails before querying if a real-model preflight fails.
- [ ] Add a client Dockerfile and Compose client definition with read-only evaluation data, bounded CPU/memory, private writable results, ddu_inference_bus, and no DB, Docker socket, or production API access.

### Task 5: Timing/resource wrapper and runbook

**Files:**
- Create: scripts/evaluation-host.ps1
- Create: docs/RETRIEVAL_EVALUATION.md

**Interfaces:**
- Host wrapper accepts split, output path, cache-state, execution-location, and optional worker container IDs; it runs the short-lived client and samples only the existing embedding/reranking containers.
- Resource JSONL contains UTC timestamp, current evaluation phase, container alias, CPU percent, memory usage/limit, sample interval, and host identity.

- [ ] Implement Docker stats sampling without starting/stopping workers and collect container start timestamps for operator-managed cold runs.
- [ ] Keep first readiness/request timing distinct from warm percentiles; record cold process/model load only, not download.
- [ ] Document retrieval service prerequisites, explicit operator start/stop commands by reference to the existing LITE runbook, local/remote setup, warm/cold protocol, review workflow, incomplete-data behavior, and report interpretation.

### Task 6: Staged answer-quality study specification and packet formats

**Files:**
- Create: backend/app/evaluation/answer_study.py
- Create: backend/app/evaluation/ratings.py
- Create: backend/app/evaluation/prompts.py
- Modify: docs/RETRIEVAL_EVALUATION.md

**Interfaces:**
- Answer study selects 25–40 held-out cases stratified by language/topic/kind/answerability.
- Retrieved and oracle conditions use the same max passage count and evidence-token budget; oracle fills that budget with highest-graded evidence.
- Human rating fields separately represent factual accuracy, evidence support/contradiction, citation correctness, coverage, relevance, and temporal consistency.

- [ ] Implement stable stratified sampling, answer packet anonymization, rating import validation, and separate claim-level fact/support/citation scoring.
- [ ] Keep no-context factual status independent from evidence support; do not report unsupported-as-false.
- [ ] Do not call the answer generator during Stage 1 or require answer-study artifacts for retrieval acceptance.
