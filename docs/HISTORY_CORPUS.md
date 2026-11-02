# Danmarks Breve authentic corpus

The backend can import the Royal Danish Library's `Danmarks Breve` v3.1.0 release as an independently versioned, immutable source library. The release supports historical source analysis; it is not a fact oracle and does not establish evidence eligibility for any CASES scenario.

Its tables are added by Alembic revision `0006_history_authentic`, after the B3 `0005_historical_rag` revision. This additive schema leaves the M2 and RAG tables intact. Versailles scenario evidence remains governed by its own approval and rights workflow; importing this library does not approve or expose its passages to gameplay.

> **Production downgrade warning:** `0006_history_authentic.downgrade()` executes `DROP SCHEMA history_authentic CASCADE`. It removes all five history tables and any imported releases, documents, and passages. Do not use this downgrade as routine production recovery. The current live Cases database was upgraded to 0006 on 2026-11-02 and has no imported release data yet; that empty state is not a guarantee about future deployments. A downgrade or restore for a data-bearing deployment requires a fresh, independently verified full-database backup and a separately reviewed recovery/cutover plan. A downgrade/re-upgrade rehearsal on a disposable clone does not authorize doing it on production. Prefer a forward fix. See [the backend runbook](M1_RUNBOOK.md#current-host-compatibility-and-recovery) for the live source/database compatibility boundary.

The importer pins the supplied source archive by SHA-256:

```text
c63fdd4299403b92d685e10becfe880e0606aaccfd50dbad180673039fc8ecf3  public_domain_Danmarks_Breve.zip
```

The authenticated release is `latest/authentic_corpus_v3_1/`. Its SHA256SUMS file is pinned to `322d394c1a16e1e7bd6cfe21444e20a0f9ee131411b6ff7eff7ba05ec88dcc0e`. It contains 13,516 documents, 78,053 passages, 7,402,564 document words and 102 source editions. Both the release tree and original ZIP must remain outside Git. Do not edit OCR text in place; a changed artifact fails the checksum gate.

## Offline import

Extract the supplied complete bundle outside the repository and mount that directory read-only to the backend host as `HISTORY_CORPUS_DIR`. It must contain:

```text
latest/authentic_corpus_v3_1/
source_archive/public_domain_Danmarks_Breve.zip
```

After applying database migrations, run the importer explicitly:

```powershell
$env:DB_HOST = 'postgres'
$env:HISTORY_CORPUS_DIR = 'D:\data\danish_history_complete_bundle_v3_1'
uv run --project backend alembic upgrade head
uv run --project backend python scripts/import_history_corpus.py
```

The command validates archive and per-file checksums, release reports, JSONL counts and hashes, all source spans and edition references before any write. It loads into transaction-local staging tables, checks referential integrity and promotes a complete release in one transaction. A repeated import verifies stored rows against the same bytes and records an `unchanged` ingest run. A validation or database failure records an ingest diagnostic without exposing a partial release.

The schema is isolated under `history_authentic`; it does not change game-session or game-turn tables. The release row stores the pinned manifest and the source QA and validation reports; the importer prints the flagged metadata anomaly count. Ready releases and their rows are guarded against updates and deletion. Passage search text is indexed with PostgreSQL's `simple` configuration. Raw source metadata, including printed/raw dates, attribution, rights, provenance, variant, source URLs and checksums, remains attached to each release.

The publisher's Public Domain Mark 1.0 applies to the published archive. Preserve attribution to Det Kgl. Bibliotek and retain the release hash when citing imported rows. `source_span_verified` means the excerpt matches its stored source text; it does not mean a historical claim has been reviewed or is ready to cite in gameplay.
