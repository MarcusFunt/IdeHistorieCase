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

## Local checks recorded on 2026-10-29

The checks in this section ran in a local checkout. They do not establish that GitHub Actions passed.

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

## CASES CI remediation — 2026-10-29

The failed [Backend CI run 40868575862](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40868575862) stopped at the byte-exact Versailles bundle pin test. Its Linux checkout computed `040503bf00d0466164ef8893d57b39230109edd394d9711a5f1de91cbecb0282`, while the committed pin was `9cd8ac95cd39d2ffe258ae20509cecf5d14b92b0405c2e26377a213751f028b1`. The digest includes the manifest, version-matched YAML sidecars and recursive `sources/**/*.yaml` files as raw bytes, sorted by relative path. Git stores these YAML files as LF, while this Windows checkout used CRLF because `core.autocrlf=true`; that caused the pin to be generated from different bytes. A fresh LF checkout reproduced the CI digest. No scenario text or immutable version content changed; the pin now matches the reviewed committed bytes, and `.gitattributes` fixes YAML/YML checkout endings to LF.

Added the read-only `verify-bundle-pins` command after `validate-all` in CI. It requires a pin for every bundle-bearing manifest, validates the digest shape, detects mismatches and orphan pins, reports read errors, and never writes pins. Regression tests cover manifest, sidecar, source, and line-ending changes, plus missing, malformed, orphaned, and unreadable pins.

The complete [Backend CI run 40892572079](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40892572079) passed on remediation code commit `f1d63c74fee052dccc4cb63db22fea8c6dfa1c07` in PR [#7](https://github.com/MarcusFunt/IdeHistorieCase/pull/7):

- Scenario validation: 2 manifests; bundle verification: 1 pin.
- Unit/contract tests: 95 passed, 37 database-gated skipped, 1 deselected.
- Alembic upgraded a fresh CI database through migration 0004. Database suites passed: integration 1, M2 integration 15, migration preservation 1, scenario runtime 10, scenario repository 7, scenario CLI 2, and scenario API 2 (38 total).
- Compose validation and no-inference Docker boot passed. `/healthz` and `/readyz` succeeded, status reported `worker=running` and `inference=degraded`, and the fake-turn smoke completed submission, idempotency, processing, resume and replay.
- Ruff passed for backend code, tests, and `scripts/m2_fake_smoke.py`.

Warnings observed on that run: checkout/setup-python used Node.js 20 and emitted the GitHub deprecation notice; pytest-asyncio emitted the unset fixture-loop-scope warning; Node also emitted a `punycode` deprecation, which needs separate investigation after the action upgrade. The failure-only log step was skipped on this passing run; its behavior before Compose setup is addressed in the follow-up CI changes.

PR #7 was merged to `main` as commit `312346e8510b3f85528157979ae57cd57957a70c`. The [push-triggered Backend CI run 40893804099](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40893804099) on that main commit completed successfully.

## CI reliability follow-up — 2026-10-29

PR [#8](https://github.com/MarcusFunt/IdeHistorieCase/pull/8), stacked on PR #7, introduced current Node 24 GitHub Actions releases, a pinned `uv` install, an explicit pytest fixture loop scope, clearer failure-only Compose logs, and manual workflow dispatch. Its full [Backend CI run 40893533431](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40893533431) passed for PR head `39348393030ba497ce2b9e656e7bab5c9b7d67d0` (the runner tested merge ref `844fdcd3fbbe436d009fa2473d2fe8cc902cf108`) with uv 0.13.5. A follow-up review found that bare `uv run` could refresh a stale lock. PR [#9](https://github.com/MarcusFunt/IdeHistorieCase/pull/9) adds `--locked` to every invocation. The final [Backend CI run 40894623285](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40894623285) passed on PR #9 head `691d990fe5258178504d8f8b28b9bf79b5aba14f`, using `uv sync --frozen` and `uv run --locked` for the committed lockfile.

- Scenario validation: 2 manifests; bundle verification: 1 pin.
- Ruff passed. Unit/contract tests: 95 passed, 37 database-gated skipped, 1 deselected.
- Alembic upgraded the fresh PostgreSQL service through migration 0004. Database suites passed: integration 1, M2 integration 15, migration preservation 1, scenario runtime 10, scenario repository 7, scenario CLI 2, and scenario API 2 (38 total).
- Compose configuration, no-inference Docker build/boot, `/healthz`, `/readyz`, status (`worker=running`, `inference=degraded`), and fake-turn smoke all passed.
- The Node.js 20 action deprecation, `punycode` deprecation, and pytest-asyncio fixture-scope warnings seen in the first run were absent. Remaining unrelated output includes the Docker image build's root-pip and UID-range warnings and PostgreSQL's disposable-service trust-authentication notice. The readiness polling loop also saw one startup connection reset before the service became ready; all health and smoke checks then passed.
- The failure-only log step was skipped because the run passed. Its guard prints `Compose not started; no container logs available` unless a Compose boot was attempted and the env file exists; boot-attempt failures still collect `compose ps` and logs.

The workflow keeps the existing single `tests` job/check name because the repository's required-check configuration was not established; splitting it could silently change merge requirements. A `codex/**` push trigger was deferred because pull-request events already run CI for review branches. Scenario content and its immutable version were unchanged by either remediation PR.

PR #9 was merged to `main` as `225ebe753eb8102834d9e5e07dbabd94041e181f`. The [push-triggered Backend CI run 40895434667](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40895434667) on that merged commit completed successfully across every workflow step, including frozen/locked dependency use, the PostgreSQL suites, and the no-inference Compose smoke.
