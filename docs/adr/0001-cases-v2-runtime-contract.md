# ADR 0001: CASES v2 runtime contract

- **Status:** Accepted for Goals 2–16; implementation follows the verified source baseline recorded by Goal 0.
- **Date:** 2026-11-09
- **Decision owners:** CASES project

## Context

CASES already stores immutable scenario versions, pins sessions to a version, queues durable turns, commits results and events in PostgreSQL, and filters historical passages by version, date, actor, mode, review, and rights. The current scenario manifest is schema v1 and requires phases, dilemmas, state variables, and fixed event permissions. V2 must support open-ended play without reinterpreting v1 data or weakening its validator.

The v2 implementation plan calls for a canonical, SQL-backed release; durable AI initialization; typed model proposals; an event-authoritative runtime; explicit source trust categories; and a bounded session capability. This ADR freezes those interfaces before implementation.

## Decisions

### 1. Manifest and runtime version routing

- `schema_version` is the manifest-format discriminator. The parser dispatches `1` to the existing strict v1 model and validator and `2` to a separate strict v2 model and validator. Missing, malformed, or unsupported values fail before persistence. V1 validation is not relaxed to accept v2 fields.
- Manifest schema version is distinct from the HTTP API version. Existing `/api/v1` routes remain available for v1 sessions and may serve v2 sessions with an explicit `runtime_version` discriminator and additive lifecycle fields. A breaking HTTP response or authentication change requires a new API version.
- Persist the runtime discriminator (`v1_legacy` or `v2_ai`) with the scenario version and the session. Readers dispatch by the stored discriminator, never by the currently active release. Existing sessions keep their original scenario-version row and v1 event interpretation after activation changes.
- V2 releases are immutable `(slug, version)` identities. Activation affects only new sessions. An identical import is idempotent; changed content at the same identity is rejected atomically.

### 2. Canonical v2 release

- The canonical release is a strict manifest with stable IDs for metadata, historical context, actors, playable roles, learning objectives, chronology, typed author content, source references, interaction guidance, and a generic completion policy. Optional beats are suggestions only.
- V2 does not require a phase graph, fixed dilemma list, scripted decision codes, or a scenario-name-specific runtime branch. Content may guide interaction but cannot select scenario-specific Python gameplay logic.
- PostgreSQL is the runtime source of truth. The immutable canonical manifest is stored with its `ScenarioVersion`. Author content items that need role, date, mode, or text filtering may be stored as immutable child rows in the same release transaction; each row is keyed by a manifest ID and covered by the release hash. Runtime must not read authoring YAML/JSON files.
- Canonical manifest hashing uses the validated JSON form: UTF-8, sorted object keys, compact separators, `ensure_ascii=false`, and no non-finite numbers. A separate bundle hash covers all imported authoring bytes that affect the release. Both hashes include the version identity; reimport cannot replace content in place.
- Import, activation, and evidence approval are separate operations. Publishing scenario notes does not approve a historical source or passage.

### 3. Opening date and date advancement

- Each v2 release declares exactly one `chronology.opening_date` as an ISO calendar date. It must fall within the release's inclusive historical period. All sessions for that release start on that date; the initializer may repeat it but cannot choose or change it.
- The current simulated date starts at the release opening date and is part of the session state projection. Only the validated `advance_world_time` operation can change it.
- Date movement is monotonic. Each operation advances by at least one day and no more than the release's positive `chronology.max_days_per_advance`; the target cannot exceed the release's historical-period end. The validator also checks any release-specific date and knowledge constraints before applying the operation. A rejected date proposal has no state effect.
- Historical passage eligibility is evaluated against the current simulated date and authorized actor at retrieval and again before commit. A later document, later availability date, or postgame passage is never eligible for gameplay.

### 4. Session initialization lifecycle

V2 session creation pins the active release and playable role, creates one durable initialization job per session, and returns the session capability once. The initialization state is:

```text
queued -> running -> ready
                  -> retryable_failure -> queued
                  -> failed
```

- `ready` is written only in the transaction that stores the validated opening, its authoritative initialization event/result, and the initialized state projection. A queued, running, or failed session is not playable.
- A worker lease/fencing token owns each attempt. Duplicate requests, worker restarts, and stale results cannot create a second opening. Inference runs outside database transactions and locks.
- The session remains `uninitialized` while the job is queued, running, retryable, or failed; only a successful atomic commit changes its lifecycle to `ready`. Transient service, context/evidence, timeout, and database failures may be retried within the bounded whole-job attempt budget below. Invalid or unauthorized proposals become terminal after the allowed repair. Failure updates job metadata only; it does not alter story state or turn number.
- V2 creation accepts `Idempotency-Key` and a client-generated 256-bit `X-Session-Capability` together. Store only hashes of both. A matching retry returns the same pinned session and initialization job, omits the capability from the replay response, and rejects reuse with different session inputs.
- Preserve the existing non-null `ScenarioVersion.initial_state` column for compatibility: v2 stores `{}` there as a neutral placeholder, not a playable state. A v2 session projection remains uninitialized until the opening event/result and initial projection commit successfully.
- The client reads lifecycle and failure information from the session/initialization status resource. Retrying reuses the same logical initialization job and cannot create another opening.
- **Selection is distinct from playability for v2.** Activation verifies the immutable SQL manifest and complete content rows, then makes the release selectable for session creation. The runtime playable check remains false until the required runtime is implemented. New sessions stay uninitialized and turn submission is rejected until the opening commits as `ready`.

### 5. Model proposal boundary and validation

- Initialization and turn calls return strict, versioned JSON proposals in Danish. Schemas forbid unknown fields and bound text and list sizes. A turn proposal may contain narrative/dialogue, typed state operations, learning observations, historical-claim references, and a continue/complete signal.
- The model may propose narrative and operations from an application-owned allowlist. It cannot execute SQL, call tools, change the pinned manifest, select inaccessible actors, choose arbitrary evidence, or send an unrestricted state patch.
- Before commit, application code validates the proposal against the pinned release, current projection, player role, current date, operation allowlist, referenced actor/objective/thread IDs, and evidence IDs actually supplied to that inference call. The application applies accepted operations deterministically.
- Historical claims may cite only eligible passage IDs supplied for that request. Author notes, character beliefs, player statements, simulation events, and postgame reflection are separately typed and cannot be promoted to historical evidence by the model.
- A proposal is never authoritative by itself. Invalid output is discarded; only a validated proposal's committed result and accepted events become session history.

### 6. Events, committed results, and state ownership

- The ordered, committed event stream in PostgreSQL is authoritative for decisions, simulation outcomes, claims, actor reactions, objective observations, and completion. It is append-only from the runtime's perspective.
- A validated opening is persisted once as an initialization result/event under the same append-only authority. A committed turn stores the exact player-visible result and ordered accepted events together.
- `GameSession.state` is a rebuildable projection of the event stream, updated in the same transaction as the events and committed result. It is not an independent authority. Replay rebuilds state from committed events and never reruns inference.
- A failure, raw draft, or uncommitted proposal cannot change the event stream, state, opening, or turn number. Narrative summaries and browser state are disposable caches derived from committed records.

### 7. Truth and knowledge categories

| Category | Meaning and authority | Runtime rule |
|---|---|---|
| Historical evidence | Exact passage from a source, with provenance, date, actor visibility, mode, current review decision, and approved rights | Only an eligible passage can support a historical citation. Import or URL presence is not approval. Restricted passages may be eligible for authorized actors under explicit policy; postgame passages may be eligible in postgame mode. |
| Author note | Curated, versioned scenario guidance or interpretation | A reviewed briefing may be shown to roles/dates allowed by its policy; label it as scenario context and do not present it as a cited source passage. |
| Character knowledge | Information available to a particular actor at a particular in-world date | Apply role and chronology restrictions; a character's belief is not automatically a verified fact. |
| Player claim | A statement or assumption made by the player | Preserve as a player claim; never convert it into historical truth without independent eligible evidence. |
| Simulation event | Invented dialogue, reaction, decision, or consequence in this playthrough | Label as simulated/counterfactual and keep it out of documented history. |
| Postgame reflection | Educational comparison derived from committed play and postgame-eligible evidence | May discuss choices and tradeoffs; distinguish documented evidence from interpretation and simulation. It is not a grade or proof of mastery. |

Historical passage eligibility continues to use the existing review and rights records and the current version/date/actor/mode policy. Rejected or revoked reviews, pending or denied rights, blocked or future-dated sources, and otherwise ineligible passages are excluded. Restricted passages require an explicitly authorized scope and actor. Postgame-only passages are excluded from gameplay but may be returned in postgame mode when their review, rights, actor, and mode checks pass. A source catalog record is not itself evidence.

### 8. API status and failure reporting

- Session creation for v2 returns the pinned release identity and `initialization_status`; resume and initialization polling expose `queued`, `running`, `ready`, `retryable_failure`, or `failed`. The session lifecycle is `uninitialized` until the initialization job reaches `ready`; a failed job remains unplayable. The opening is returned only after `ready`.
- Turn submission remains durable and idempotent by client turn ID. The status resource exposes `queued`, `running`, `completed`, `retryable_failure`, or `failed`; the result is visible only after `completed`. A stale turn or conflicting idempotency key returns a conflict and does not mutate state.
- Job failures use a stable machine-readable `failure` object with `code`, `stage`, `retryable`, and a brief safe `message`. They do not expose prompts, rejected model text, stack traces, credentials, or private source text. Expected codes include `SCENARIO_NOT_PLAYABLE`, `INVALID_ROLE`, `INITIALIZATION_FAILED`, `INFERENCE_UNAVAILABLE`, `INFERENCE_TIMEOUT`, `MODEL_OUTPUT_INVALID`, `EVIDENCE_UNAVAILABLE`, and `TURN_CONFLICT`. Missing or non-Bearer credentials return 401 with a Bearer challenge; an unknown session or wrong/expired bearer returns the existing non-disclosing 404.
- Evidence status is reported separately as `available`, `none_eligible`, or `unavailable`, with only the eligible passage identifiers used. `none_eligible` is a normal no-source result: continue without citations and tell the client no evidence is available. `unavailable` is an infrastructure failure: return a retryable failure without committing a proposal. It consumes a whole-job attempt, even when failure occurs before an inference call.
- A failure leaves the session's last committed opening, events, state, and turn number unchanged. A later retry does not create a second opening or turn.

### 9. Model provenance, retry, and repair bounds

Persist provenance with each initialization and committed turn, and retain bounded attempt metadata on failures:

- pinned scenario-version ID, slug/version, manifest content hash, and bundle hash;
- provider/service identifier and model alias; immutable served model revision or digest when reported by Local-AI-API-LITE, otherwise an explicit unavailable value;
- prompt template revision/hash, proposal schema version, context-selection revision, generation settings, and the IDs/hashes plus applicable approval revision of eligible passages supplied;
- provider request/correlation ID when returned, attempt and repair numbers, start/end timestamps, duration, token counts when returned, outcome, and stable failure/validation codes;
- hash of a rejected output for diagnosis, without storing rejected raw proposals or logging prompts, credentials, or private evidence.

Local-AI-API-LITE owns model deployment and lifecycle. The application records only metadata returned or configured at its boundary and never invents a model revision.

Each job gets at most three whole-job attempts in its automatic cycle, counted when a worker claim begins and including context selection, evidence retrieval, inference, validation, and commit failures. Transient errors use bounded exponential backoff capped at 30 seconds. Exhausting the automatic cycle sets `retryable_failure`. One explicit client retry may start one final cycle of at most three whole-job attempts; exhausting that cycle sets terminal `failed`. Thus no pre-inference failure can retry indefinitely, and a retry reuses the same logical job. A job may receive at most one structured repair request across its lifetime, containing bounded validator error codes/paths; the repair does not widen the schema or grant new permissions. A second invalid proposal, or an invalid repaired proposal, is terminal. At most six primary inference calls plus one repair call can occur, and fewer may occur when whole-job failures happen before inference. A failed provider call consumes a whole-job attempt but no repair budget. No unbounded retry/repair loop or fake-provider production fallback is permitted.

### 10. Session capability and deferred identity

- A session capability is an opaque bearer token returned once at creation. Store only its cryptographic hash; never log or include it in model context.
- For idempotent v2 creation, the client generates and retains the bearer before sending it as `X-Session-Capability`; `Idempotency-Key` identifies the create request. Both headers are required together. A replay returns session metadata without repeating the bearer value.
- The capability expires exactly 24 hours after issuance, using UTC timestamps. After expiry, the session remains stored but cannot be resumed with that token; the API returns 404 for an unknown session or wrong/expired bearer, while a missing/non-Bearer credential returns 401. The UI must not promise longer or cross-device recovery.
- There is no account or guest profile in this release. Guest profiles, multi-session progress, capability renewal/recovery, and account-based identity are deferred until classroom use demonstrates the need and a privacy/retention design is approved.

## Rejected alternatives

- A fixed branching tree or mandatory phase/dilemma graph would constrain open-ended play and make the first case define the engine.
- A giant untyped chat prompt would make state, evidence, and retry behavior unverifiable.
- Model-generated SQL, tools, or arbitrary JSON patches would hand application authority to untrusted output.
- Automatic source approval would conflate import with historical and rights review.
- An immediate heavyweight agent framework adds orchestration and tool surface before a measured need exists.
- Randomly chosen session opening dates would make release chronology and replay less reproducible; a release has one canonical opening date.
- Indefinite bearer capabilities or implicit renewal would promise recovery beyond the implemented authorization boundary.

## Consequences

V1 scenario definitions and sessions remain readable under their original semantics. V2 runtime behavior is generic and depends on immutable SQL releases, validated proposals, current evidence eligibility, and event-derived state. The API and UI must expose durable initialization/turn status and actionable failures. Additive schema changes will be required for initialization jobs, runtime discriminators, provenance, and event projection support; implementation must use an isolated database and preserve the existing schema and sessions.

**Baseline:** Goal 0's [audit](../CASES_BASELINE_AUDIT.md) selected this worktree at `db0350dc5287701d1ca59addc35566afe309e24a` (`codex/cases-ai-v2`) from fetched `origin/main`. The audit reports source migration head `0006_history_authentic`, but the live database's current Alembic revision remains unknown because its container was stopped. Its available dump predates `0006` and is not a current recovery point. Goal 2+ database work must use a verified isolated restore and must not target the live volume.

## References

- `CASES_AI_Driven_Multi_Scenario_Implementation_Plan_v2.md`, especially §§ 6–14.
- `docs/SCENARIO_VERSIONING.md` — existing immutable version and hash behavior.
- `docs/SCENARIO_AUTHORING.md` — existing strict v1 manifest behavior.
- `docs/M2_DEVELOPMENT.md` — current session capability and durable turn API.
- `docs/SHARED_INFERENCE.md` — inference service ownership and client boundary.
- `docs/CASES_BASELINE_AUDIT.md` — verified code/worktree baseline and explicit deployment/recovery limitations.
- `backend/app/scenarios/schema.py`, `backend/app/game/routes.py`, `backend/app/db/models.py`, and `backend/app/rag/policy.py` — implementation baseline inspected for this decision.
