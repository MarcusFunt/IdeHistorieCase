# M2 Durable Turns Implementation Plan

> Implement the existing project plan's M2 backend slice in this worktree.

**Goal:** An authenticated session can submit, resume and replay one durable fake-provider turn while inference is offline.

**Architecture:** PostgreSQL stores immutable scenario versions, session capabilities, queued requests and committed events. FastAPI performs session-scoped authorization and idempotent submission; an independent worker leases jobs, calls the deterministic fake provider outside transactions and commits a turn exactly once.

**Tech stack:** FastAPI, Pydantic v2, async SQLAlchemy, PostgreSQL, Alembic and pytest.

**Spec:** `IMPLEMENTATION_PLAN.md` sections 5, 6 and 14, and the attached M1.5 plan's section 7.

## Constraints

- No real inference, RAG, Godot or production classroom authentication in this slice.
- Session capability secrets are returned once, stored only as hashes and expire.
- One outstanding turn per session; retries return the same request and cannot mutate committed state twice.
- Worker leases use PostgreSQL row locks, expiry, a bounded attempt count and lease ownership checks.
- No database transaction remains open during provider generation.
- Preserve the M1 health and inference-monitor behavior.

## Tasks

1. Add PostgreSQL integration tests for authenticated session creation, submission, idempotent retries, conflict handling and cross-session authorization. Verify they fail before implementation.
2. Add scenario, session, request, committed-turn and event models plus an Alembic migration. Add session and turn API routes with capability validation and bounded inputs.
3. Add tests for worker claiming, lease expiry, stale-owner rejection, atomic completion and replay. Implement deterministic fake generation and the worker loop.
4. Run unit, migration, PostgreSQL integration and backend Compose checks without inference. Document the API, security boundary and remaining M2 work.
