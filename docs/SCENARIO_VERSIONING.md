# Scenario versioning and identity

A published scenario uses a unique (slug, version) pair. Versions are opaque strings: ordering is determined by an explicit active pointer, never by lexicographic comparison. The active version selects content only for new sessions; each session remains bound to its stored scenario-version row.

## Content hash

The loader validates the manifest first, then hashes its canonical JSON representation:

1. ScenarioDefinition.model_dump(mode="json").
2. JSON with sorted keys, compact separators, UTF-8, ensure_ascii=False, and no non-finite numbers.
3. SHA-256 of those bytes.

YAML formatting, source-file path, and mapping-key order do not affect the hash. The slug, content version, and schema version are included because they are part of the validated model.

After import, authored content, its hash, and the derived initial state are immutable. To change a published scenario, author a new content version. Activation is a separate explicit operation; old sessions keep their original version.

The definition hash covers only the B1 manifest. A separate byte-exact bundle hash covers the manifest, its version-matched sibling YAML sidecars, and YAML files under sources/. The bundle hash is stored with an imported version and protected by the same database immutability guard. Validated behavior and evidence-policy sidecars are also stored as private JSONB snapshots for later runtime use; the public API does not return them. Reimporting the same slug/version after any bundled file changes is rejected. The checked-in 2.5_bundle.sha256 pins the current Versailles package; it does not include itself.


## API and publication

- GET /api/v1/scenarios lists versions with schema version, title, summary, default language, playable role IDs, active status, and an explicit authored flag. The M2 seed remains visible as an unauthored stub.
- GET /api/v1/scenarios/{slug} returns schema-aware public display content for the active authored version. V1 responses include phases and dilemmas; v2 responses include chronology, interaction guidance, completion policy, and optional beats. Source references and retrieval sidecars are not returned. An active legacy stub responds with 409; an unknown slug responds with 404.
- POST /api/v1/sessions accepts an optional role_id. Authored runtime-compatible scenarios resolve the active version once and default to its default playable role. Legacy sessions may omit a role. The stored version and role remain pinned when the active pointer changes.
- GET /api/v1/sessions/{id} includes the session's pinned scenario, version, and player_role_id.

## Runtime compatibility and readiness

The current turn worker and session runtime support schema v1 only. Schema-v2 bundles can be structurally validated, imported inactive, inspected from SQL, and reported by the schema-aware catalog endpoint. The activation service rejects them with an explicit unsupported-runtime error, and session creation also rejects a v2 row if an older deployment or direct database operation left it active. A structurally valid manifest is not evidence of runtime support.

Use `validate` for structural validation. Add `--json` for a machine-readable report that identifies checks which require SQL. Use `readiness <slug> --version <version>` (also available as `playable-check`) for database-backed schema validity, publication integrity, runtime compatibility, author-content review, and evidence-readiness results. The command emits JSON and exits nonzero when the release is not fully playable. It only reports readiness; it does not activate a release.

Schema-v1 activation and session behavior remain supported. The v2 Versailles intake is intentionally inactive and its opening briefing remains draft; its readiness report must not describe it as playable or evidence-ready.

Publish using the developer CLI from the repository root:

    uv run --project backend python -m app.scenarios.cli import scenarios/versailles-1919/2.5.yaml
    uv run --project backend python -m app.scenarios.cli activate versailles-1919 --version 2.5
    uv run --project backend python -m app.scenarios.cli list
    uv run --project backend python -m app.scenarios.cli show versailles-1919 --version 2.5

Import creates an inactive version. Activation is explicit. For a read-only import check, add --dry-run.


## Deterministic runtime effects

The fake provider boundary accepts typed candidate events. The application validates metric permissions and ranges, flag targets, phase transitions, and issue decisions against the stored manifest and behavior snapshot before committing them. One turn may commit multiple ordered events; existing v1 turns still commit one player-argument event. The Versailles reaction policy maps decision order to authored objection codes only as an illustrative qualitative game mechanic. It does not claim that the paired concern was a documented response by a historical delegation.
