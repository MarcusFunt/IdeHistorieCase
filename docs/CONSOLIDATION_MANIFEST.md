# Consolidation source and preservation manifest

**Target branch:** `codex/cases-consolidation-2026-11-02`

**Baseline:** `origin/main` at `b65faad48db261220db34b64d6da8dd4cdc22905`

**Implementation commit:** `9584649cabb12ee263e3c42e5d25e4999f10989f`

**Review:** [draft PR #11](https://github.com/MarcusFunt/IdeHistorieCase/pull/11), not merged to `main`.

## Source lineage

| Source | Commit | Handling |
|---|---|---|
| `main` | `b65faad48db261220db34b64d6da8dd4cdc22905` | Baseline for the clean consolidation worktree; all M2 and B1 content retained. |
| `feat/cases-b3-historical-rag` | `b2490063914880af363a519db09d3015e1a61f5b` | Merged as `3dfa4db`; RAG API, policy, corpus, tests, migration 0005, and pinned client. |
| `codex/retrieval-evaluation` | `90dd6ca82d6162fe2543055cf4d2a9a298fb9c70` | Merged as `b8456ad`; evaluation module/data/contracts/tests reconciled with B3. |
| `codex/danmarks-breve-pr1` | `63d760721e1b9f90833367fc5d7d99efcdd114ba` | Selectively copied history importer/models/tests/docs/script; migration adapted to 0006. The branch itself was not merged. |
| `codex/cases-history-integration` | dirty at `b249006` | Preserved as quarantined evidence; malformed conflict files were not used as source. Snapshot: `D:\projects\IdeHistorieCase-snapshots\cases-p0-20261102-066f715438384906ac4ea51e7163a3be`. |

The independent RAG config correction aligns Docker Compose fallback model IDs with the settings defaults and tests the fallback identities. Evaluation model clients now validate LITE capability metadata and request limits before using embeddings or rankings.

## Database graph

```text
0001_service_heartbeats
  -> 0002_durable_turns
  -> 0003_scenario_definitions
  -> 0004_multiple_events_per_turn
  -> 0005_historical_rag
  -> 0006_history_authentic (only head)
```

The history migration adds a new `history_authentic` schema. It does not replace or rename M2, scenario, or B3 tables. The full Danmarks Breve ZIP and extracted release are not committed.

After the user explicitly authorized updating the currently running Cases stack, the existing `ddu-backend_pgdata` volume was upgraded in place on 2026-11-02. A fresh pre-upgrade backup was made and a restored clone of that exact backup was upgraded first; details are in [CONSOLIDATION_VALIDATION.md](CONSOLIDATION_VALIDATION.md).

## Protected Versailles content

All assets below remain in the consolidation tree, with the authored bundle pin unchanged at `040503bf00d0466164ef8893d57b39230109edd394d9711a5f1de91cbecb0282`:

```text
scenarios/versailles-1919/2.5.yaml
scenarios/versailles-1919/2.5_behaviors.yaml
scenarios/versailles-1919/2.5_bundle.sha256
scenarios/versailles-1919/2.5_evidence.yaml
scenarios/versailles-1919/sources/catalog.yaml
scenarios/versailles-1919/sources/claims.yaml
scenarios/versailles-1919/sources/history_goldens.yaml
scenarios/versailles-1919/tests/scripted_sessions.yaml
docs/VERSAILLES_RESEARCH.md
backend/tests/test_scenario_engine.py
backend/tests/test_scenario_runtime.py
```

The Danmarks branch's deletions of scenario assets and tests were not imported. Generated evaluation results, environment files, database dumps, model caches, and corpus archives are outside the repository.

## External model pins

| Alias | Role | Identity |
|---|---|---|
| `ddu-narrator` | Optional worker diagnostic | LLM deployment identity comes from Local-AI-API-LITE; not used by the default turn provider. |
| `ddu-embed` | Production B3 and offline dense retrieval | `Qwen/Qwen3-Embedding-0.6B@97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`; 1024 dimensions; query instruction `qwen3-question-to-passages-en-v1`. |
| `ddu-rerank` | Optional B3/evaluation reranker | `Alibaba-NLP/gte-multilingual-reranker-base@8215cf04918ba6f7b6a62bb44238ce2953d8831c+code@40ced75c3017eb27626c9d4ea981bde21a2662f4`; disabled by default in Cases. |

## Evaluation status

The checked-in evaluation manifest remains `draft`: 40 reviewed pilot queries and 0/100 held-out queries. Do not treat the pilot as held-out acceptance or promote generated result artifacts into Git. See [RETRIEVAL_EVALUATION.md](RETRIEVAL_EVALUATION.md).
