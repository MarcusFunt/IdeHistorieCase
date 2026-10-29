# Scenario authoring (schema v1)

A scenario manifest is one immutable authored release. Put each manifest under scenarios/<slug>/<version>.yaml; keep research catalogs, evidence access rules, and future B2 behavior rules in separate sidecars. The B1 schema rejects unknown fields, so adding runtime or retrieval policy belongs in a versioned sidecar or a later schema version.

## Validate before import

From the repository root:

    uv sync --project backend --extra dev
    uv run --project backend python -m app.scenarios.cli validate scenarios/versailles-1919/2.5.yaml
    uv run --project backend python -m app.scenarios.cli validate-all scenarios/

The command checks field types, IDs, cross-references, the phase graph, and derives the initial runtime state. It prints a SHA-256 identity for the validated definition. It requires no database or model service.

The loader accepts UTF-8 YAML up to 1 MiB. It uses safe parsing, rejects duplicate mapping keys, anchors, aliases, multiple documents, non-mapping roots, unsupported schema versions, and unknown fields. IDs use lowercase tokens matching [a-z][a-z0-9_-]{1,63}; source reference keys use stable uppercase IDs such as R01.

## Required structure

A manifest defines schema_version, stable slug, and opaque content version; public metadata and a historical introduction with an explicit counterfactual disclaimer; actors, playable roles, learning objectives, typed bounded state variables, and one default playable role; a directed phase graph with one start, at least one terminal, reachable phases, and a route from every phase to a terminal; dilemmas that reference existing phases, actors, and learning objectives; and a typed future event allow-list with stable source-reference IDs.

The initial state is derived from the start phase and variable initial values. Do not duplicate it in YAML. Integer metrics and boolean flags are simulated mechanics, not historical measurements.

## Content and evidence

A source_refs entry is a planned document key, not an ingested source or a valid citation. Never present a source link as evidence that a generated claim is supported. Keep unverified statements qualified, keep later records out of earlier phases, and label invented dialogue as fictional. Rights are link-only until reviewed for Denmark and the intended deployment.
