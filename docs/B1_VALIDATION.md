# Scenario validation and delivery record

This file records checks that ran in this checkout. It separates completed B1 work, the deterministic B2 slice now exercised, and later research/runtime gates.

## Authored content

- scenarios/versailles-1919/2.5.yaml: schema version 1, ten reachable phases, twelve dilemmas, ten historical/affected actor entries, and one explicitly fictional playable British adviser.
- scenarios/_fixtures/example-second-scenario.yaml: independent synthetic manifest accepted by the same loader and CLI.
- scenarios/versailles-1919/sources/catalog.yaml: R01–R24 plus the separately scoped R11A appendix record, with institution/date/link/status/rights/reviewer and restrictions.
- sources/claims.yaml: H-001–H-018 with claim scope and source anchors.
- sources/history_goldens.yaml: V25-H01–H12 policy fixtures; these are not model-quality results.
- 2.5_behaviors.yaml: validated issue/decision allow-lists. Ordered objection pairing is explicitly illustrative, not a claim about historical reactions.
- 2.5_evidence.yaml: source date/role access and fail-closed citation readiness. All entries remain citation-ineligible until approved passages exist.

## Implemented

B1 supports strict Pydantic scenario definitions, graph/reference validation, defensive YAML parsing, canonical manifest hashes, byte-exact package hashes, immutable database imports, explicit activation, scenario discovery/details, and session-pinned scenario versions and roles. Migration 0003 preserves the seeded M2 row and protects authored snapshots. Migration 0004 supports multiple ordered effects for one committed turn.

The runtime now has a bounded typed proposal seam and applies authorized metric, flag, phase, and issue-decision effects atomically. Eight deterministic API session paths now visit different issue phases and record decisions, skipped issues and outcomes. They cover compromise, security priority, reconstruction, collective security, self-determination, deadlock, counterfactual labeling, and recovery after a worker lease expires. The recovery case also retries the original request key and checks that only one committed turn results. The deterministic reaction is qualitative and does not assert that any historical actor made that response.

## Verification run on 2026-10-29

- scenario validate-all scenarios: 2 manifests validated.
- Focused schema, loader, engine, research-asset and temporal-gate tests: 48 passed.
- Environment-independent backend suite: 86 passed, 37 PostgreSQL tests skipped, 1 DB-only test deselected.
- PostgreSQL checks on a disposable container: populated-M2 migration preservation 1 passed; M2 integration 15 passed; worker/integration heartbeat 1 passed; scenario runtime including eight full sessions 10 passed; repository 7 passed; CLI database 2 passed; API 2 passed.
- ruff check backend scripts/m2_fake_smoke.py: passed.
- Compose configuration, image build, migrations and boot: passed. /healthz and /readyz succeeded, worker reported running, and scripts/m2_fake_smoke.py completed a durable fake turn. The independent inference service was available in this environment.
- The test-only PostgreSQL instance and Compose stack were shut down after verification.

## Research status and remaining gates

The source inventory contains stable research links and article/record anchors, but no primary passage corpus has been ingested or rights-cleared for Danish educational deployment. The Library of Congress R13 page and UK Archives R14 page were not retrievable in this run; R17 remains a research lead. Human source review is still required.

The evidence helper enforces date and actor eligibility, rejects unknown scope labels, and conservatively gates aggregate Council records R07/R08 at their latest represented dates until a passage-level policy exists; the citation gate stays closed. It is not a retrieval system: there are no passage/provenance tables, embeddings, hybrid search, or 60-query retrieval benchmark. There is no real inference-backed dialogue provider or formative assessment. The eight scripts exercise deterministic events and API replay; the recovery path expires a persisted worker lease and verifies replay against the same committed request. It does not restart a live process or replace diverse model-turn evaluation, teacher review, classroom pilot, or load/backup-restore work.
