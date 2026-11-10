# M2 development: durable turn queue (historical guide)

This document describes the original fake-provider prototype. `FakeProvider` is
no longer a production default and the old `m2_fake_smoke.py` script was removed.
For current v2 model turns and the isolated real-model smoke, see the repository
[README](../README.md#optional-external-model-diagnostic).

M2 starts a minimal backend flow using PostgreSQL and a deterministic fake
provider. It runs while the independent inference server is stopped. It is
not yet the playable Versailles scenario or a classroom deployment.

## Current development path

Use the [README](../README.md) for backend startup and the disposable real-model
gameplay smoke. The original M2 smoke command depended on the removed production
fake-provider default and is intentionally not available.

## API contract

- `GET /api/v1/scenarios` lists available scenario versions.
- `POST /api/v1/sessions` accepts `{"scenario":"versailles-1919"}` and
  returns a session ID and a bearer capability. The capability is returned once,
  stored only as a SHA-256 hash and expires after 24 hours.
- `GET /api/v1/sessions/{id}` resumes the committed state and reports an
  outstanding request. Send `Authorization: Bearer <capability>`.
- `POST /api/v1/sessions/{id}/turns` requires that bearer token, an
  `Idempotency-Key` UUID and a JSON body with the same `client_turn_id`,
  `expected_turn`, Danish `text` (1–2000 characters) and
  `input_language: "da"`. It returns 202, the request ID and a `Location`
  header for polling.
- `GET /api/v1/turn-requests/{id}` returns the request status and completed
  result. `GET /api/v1/sessions/{id}/turns` replays committed results.

Keep the capability secret and retain it on the client to resume a session.
Expired capabilities cannot currently be renewed or recovered. Requests without
a bearer token return 401; a wrong or expired capability cannot access another
session or its request. Identical retries return the same request, while reusing
a key for different input, a stale expected turn or a second outstanding turn
returns 409.

The worker claims queued or expired requests with PostgreSQL row locks, uses a
60-second lease and permits three attempts with bounded backoff. The fake
provider runs outside the transaction with a 30-second timeout. Completion
checks the current lease and session turn and commits the result, event and
state change atomically. The worker continues to report inference health
separately. Generated replies are placeholders, not historical adjudication. Authored sessions may accept validated deterministic state events; issue reactions are labeled as illustrative and qualitative. The interpreter protocol is a seam for a future semantic provider, not an LLM integration.

This capability mechanism is a local development boundary, not complete
classroom authentication. Add TLS, a capability recovery and rotation policy,
rate limits, retention/deletion controls and a production security review
before exposing the API beyond trusted local testing. Real inference, approved source passages, evidence-backed dialogue, Godot, teacher review and classroom deployment remain later work. Deterministic authored events are exercised by the scenario runtime tests.
