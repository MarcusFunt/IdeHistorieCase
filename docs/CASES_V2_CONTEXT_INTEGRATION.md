# CASES v2 context builder integration contract

`app.runtime.context.build_session_context` assembles a bounded, read-only prompt
context from the schema-v2 release pinned by a `GameSession`. It does not run the
game worker, call a language model, validate a proposed state transition, or
commit game state. Existing schema-v1 sessions continue through their current
worker path.

## Worker inputs and output

The planned worker supplies:

- The persisted session, whose `scenario_version_id`, `player_role_id`, and
  `state.world_date` identify the pinned release, selected actor, and current
  game state.
- `stage="initialization"` or `stage="turn"`.
- The retrieval query derived from the current state and player input.
- `mode="gameplay"` or `mode="postgame"`.
- A `RagSearchService` backed by the SQL retrieval repository and configured
  embedding client.
- Optional `ContextBudget` limits.

Initialization uses the release opening date. Turn context requires a persisted
ISO `world_date`; it never infers a date from the active scenario or from the
latest release. The builder validates the pinned manifest, validates the
release's evidence policy, verifies the complete SQL content set against the
shared publication-integrity checker, and then builds `SessionPromptContext`.

The public inference adapter is `SessionPromptContext.to_inference_input`,
which returns JSON-compatible `stage`, `generation_mode`, `world_date`,
`current_state`, `context`, and `context_metadata` fields. The downstream
inference client can map those fields into its provider-specific request. The
contract test verifies this output serializes without changing the session
state.

## Evidence access and provenance

Gameplay applies the selected actor's access policy, the passage's document
date, and its in-world availability date. Future historical knowledge and
postgame-only passages stay out of gameplay.

Postgame keeps the selected actor identity and enforces both source-policy and
passage actor allowlists. It may include reviewed ordinary historical sources
even when their document date is later than the game's current date. Public
postgame-only sources with an empty actor allowlist may also be included.
Confidential and restricted sources still require the selected actor to be
authorized by both the scenario policy and the passage. The prompt states that
postgame chronology is retrospective and that its evidence must not be used in
gameplay.

Historical evidence carries the pinned scenario, passage and source identifiers,
title, URL, anchor, text hash, document and availability dates, current review
revision, review status, rights status, and source citation readiness. The
`claim_verification_status` remains `not_assessed`: a source-level citation
policy does not prove that a specific claim is supported. Author notes remain
in the `author_context_not_historical_evidence` category. Their `source_refs`
are displayed as references only and do not verify their claims.

Retrieved passage text is treated as untrusted source data. Instructions inside
a source cannot change system behavior or evidence rules.

## Evidence outcomes and failures

Successful context results expose evidence status through both
`SessionPromptContext.evidence_status` and `SessionPromptContext.metadata`:

| Status | Meaning |
| --- | --- |
| `no_eligible_sources` | No source is allowed for this role, date, and generation mode. |
| `no_relevant_passages` | Sources may be allowed, but retrieval returned no relevant, currently approved passage. |
| `excluded_by_context_budget` | Relevant passages were retrieved, and every one exceeded the available prompt space. |
| `evidence_included` | At least one relevant passage fit. Metadata separately lists any passages omitted by the budget. |

Metadata reports eligible source IDs, retrieved and included passage counts,
omitted passage keys, diagnostics, the character limit, serialized character
count, retrieval top-k, and truncated author-note keys. Evidence passages are
included whole or omitted whole; they are never clipped into fragments. The
builder keeps scenario identity, role, chronology, knowledge constraints, and
evidence rules in a protected core. It fits ranked passages before optional
author notes. Author-note truncation and evidence omission are reported
separately.

Validation and retrieval failures raise `ContextBuildFailure`. Its `status`
and `as_metadata()` values are machine-readable (`validation_failure` or
`retrieval_failure`). The builder performs reads only, so the worker must stop
before inference or state commit on either failure and report the error through
its existing request-failure path. A retrieval failure never becomes an empty
success response that looks like a normal evidence abstention.

## Budget defaults

`ContextBudget()` defaults to 18,000 characters of compact serialized prompt
payload and five retrieved passages. Both limits are configurable per call via
`ContextBudget(max_chars=..., top_k=...)`; `top_k` must be from 1 through 10.
The legacy direct `max_chars` and `top_k` call arguments remain supported.
Budget accounting covers the prompt payload, not the separate diagnostic
metadata returned to the worker.

## Integration boundary

The planned worker should build context before beginning any transaction that
can commit a turn. It supplies the session and current state, query, stage, and
generation mode; it then passes `to_inference_input()` to the inference adapter.
If context validation fails, it must not invoke generation. If retrieval fails,
it must report the failure and leave session state untouched. Any generated
proposal still requires the follow-up runtime work for schema-v2 turn handling,
output validation, authorization, and state-transition checks.

This service makes pinned context available to a future inference pipeline; it
does not make schema-v2 AI gameplay operational by itself.
