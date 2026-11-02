# CASES B3 Historical RAG Design

## Intent

Add historical evidence retrieval for scenario sessions while preserving the M2 durable queue, immutable scenario versions, deterministic engine, existing REST response contracts, and the repository boundary with Local-AI-API-LITE. The first corpus is a small, staged Versailles 1919 set, with a labeled evaluation file for temporal, confidentiality, rights, and unsupported-claim cases.

## Goals and non-goals

- Store immutable passages with source and scenario-version provenance, SHA-256 hashes, document dates, in-world availability, actor visibility, and append-only review decisions.
- Provide bounded corpus validation/import and a separate human approval command. Only passages with current approval and approved rights can be retrieved.
- Use PostgreSQL full-text search and pgvector dense search, reciprocal-rank fusion, and optional injected reranking.
- Apply temporal, actor, scenario, and approval gates before candidate ranking and again before returning evidence. Return `RAG_NO_EVIDENCE` on an empty or irrelevant eligible result set.
- Add an authenticated, additive session evidence route. Derive actor and world date from the pinned scenario version and session phase.
- Keep the dialogue provider and M2 turn queue untouched. Do not edit Local-AI-API-LITE or add a frontend.

## Approaches

1. **Add retrieval to turn processing.** Rejected because B3 explicitly excludes real dialogue-provider integration and the M2 queue must remain durable and deterministic.
2. **Provide a separate session-scoped evidence route backed by a version-pinned retrieval service.** Selected because it exposes independently testable RAG without changing turn submission, replay, or state transitions.
3. **Keep retrieval internal with no caller-facing contract.** Rejected because approved results need a concrete exposure boundary where session ownership and temporal visibility can be enforced.

## Architecture

PostgreSQL stores append-only passage records, append-only review decisions, versioned embeddings, and an immutable per-scenario phase-date policy. A corpus manifest is bounded and validated before ingestion; ingestion stages passages and embeddings but never approves them. A reviewer command appends approval, rejection, or revocation decisions. Retrieval gets a query embedding from an injected `ddu-embed` client, runs scenario-filtered PostgreSQL lexical and dense searches, fuses the ranks, optionally calls an injected `ddu-rerank` client, and revalidates candidate approval and eligibility before constructing results.

The API route accepts only a session capability and query. The server pins scenario/version, derives actor from the session role and world date from its current phase, and returns either ranked, approved passages or the explicit `RAG_NO_EVIDENCE` code. Existing endpoints and M2 turn records are unchanged.

## Corpus and review policy

The initial Versailles corpus uses exact, anchored excerpts from the official FRUS pages already referenced by scenario source IDs. FRUS states its information is public domain, but checked-in records begin staged with rights/review pending; a deployment reviewer must append approval before they are searchable. URLs alone never create passages. Passage text and metadata are hashed and immutable after import. Scenario version provenance is pinned to the exact imported version row.

The evaluation cases include an answerable in-window query, date leakage, actor confidentiality, unapproved rights, and an unsupported query that must abstain. Test fixtures may append explicit approvals to exercise retrieval; checked-in corpus data does not self-approve.

## Constraints and remaining builder choices

- Use the existing FastAPI, SQLAlchemy/Alembic, PostgreSQL 18 with pgvector, Pydantic, httpx, and uv-locked dependency workflow.
- Keep embedding dimensions, result bounds, query limits, and relevance thresholds configurable and documented. Validate all external model responses and fail closed on malformed values.
- Additive network access for the API is limited to the existing private `ddu_inference_bus` so it can call `ddu-embed`; no inference service is built or changed in this repository.
- Builder chooses exact TEI request payload handling, corpus limits, and the initial default threshold values; these remain injectable/configurable for host validation.
