# M2 development: durable fake turns

M2 starts a minimal backend flow using PostgreSQL and a deterministic fake
provider. It runs while the independent inference server is stopped. It is
not yet the playable Versailles scenario or a classroom deployment.

## Start and exercise

Follow the backend-only setup in the [README](../README.md) and [backend runbook](M1_RUNBOOK.md). Creating `ddu_inference_bus` lets Compose resolve its declared external network, but no inference service is required for this fake flow. Configure the backend environment and start only the backend Compose project. After migration and startup:

```sh
python scripts/m2_fake_smoke.py --base-url http://127.0.0.1:8000
```

Use the configured `API_HOST_PORT` if it differs from 8000. The script creates
one synthetic session and turn, retries the submission, waits for completion,
and checks resume and replay. It prints a result without printing the token.
It leaves the synthetic session in the configured database.

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
separately. Generated replies are placeholders, not historical adjudication.

This capability mechanism is a local development boundary, not complete
classroom authentication. Add TLS, a capability recovery and rotation policy,
rate limits, retention/deletion controls and a production security review
before exposing the API beyond trusted local testing. Real inference, authored
game rules, sources, Godot and full scenario play remain later work.
