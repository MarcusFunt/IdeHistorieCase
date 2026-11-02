from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.history import importer as history_importer
from app.history.importer import (
    ImportConflictError,
    ReleaseContract,
    ReleaseValidationError,
    import_release,
    validate_release,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _refresh_checksums(root: Path) -> str:
    payload_paths = [
        root / "DATA_CARD.md",
        root / "RELEASE_MANIFEST.json",
        root / "data/authentic/documents.jsonl",
        root / "data/authentic/passages.jsonl",
        root / "data/authentic/source_editions.jsonl",
        root / "data/authentic/qa_report.json",
        root / "data/authentic/validation_report.json",
    ]
    lines = [f"{_sha(path.read_bytes())}  {path.relative_to(root).as_posix()}\n" for path in payload_paths]
    (root / "SHA256SUMS.txt").write_text("".join(lines), encoding="ascii")
    return _sha((root / "SHA256SUMS.txt").read_bytes())


@pytest.fixture
def release_files(tmp_path: Path):
    root = tmp_path / "release"
    data = root / "data" / "authentic"
    source_archive = tmp_path / "public_domain_Danmarks_Breve.zip"
    source_archive.write_bytes(b"small fixture archive " + uuid4().bytes)
    text = "One source-exact letter passage."
    doc = {
        "id": "KBDB_L_fixture",
        "original_text": text,
        "normalized_text_alternative": None,
        "selected_variant": "raw_text",
        "source_url": "https://tekster.kb.dk/text/letters-fixture",
        "source_xml_filename": "fixture.xml",
        "year": 1848,
        "language": "und",
        "language_verified": False,
        "text_sha256": _sha(text.encode()),
        "metadata": {"raw_date": "1848", "sender": "Fixture Author", "recipient": "Fixture Reader"},
        "provenance": {"csv_row": 1},
        "rights": "Public Domain Mark 1.0",
        "factual_accuracy_checked": False,
        "ocr_accuracy_checked": False,
        "primary_source": True,
        "document_type": "historical_letter_printed_edition_OCR",
        "word_count": 5,
    }
    passage = {
        "id": "KBDB_L_fixture_P0000",
        "document_id": doc["id"],
        "contents": text,
        "char_start": 0,
        "char_end": len(text),
        "text_sha256": _sha(text.encode()),
        "source_url": doc["source_url"],
        "year": 1848,
        "language": "und",
        "source_type": "historical_letter_ocr",
        "source_variant": "raw_text",
        "license": "Public Domain Mark 1.0",
        "primary_source": True,
        "source_span_verified": True,
        "historical_truth_verified": False,
        "word_count": 5,
        "sender": "Fixture Author",
        "recipient": "Fixture Reader",
    }
    _write_jsonl(data / "documents.jsonl", [doc])
    _write_jsonl(data / "passages.jsonl", [passage])
    _write_jsonl(data / "source_editions.jsonl", [{
        "edition_xml_filename": "fixture.xml",
        "source_url": "https://tekster.kb.dk/text/letters-fixture",
        "pubTitle": "Fixture Edition",
    }])
    _write_json(data / "validation_report.json", {
        "status": "PASS",
        "documents": 1,
        "passages": 1,
        "checks": [
            "document_keys_unique",
            "passage_keys_unique",
            "foreign_keys_valid",
            "all_source_offsets_exact",
            "document_sha256",
            "passage_sha256",
        ],
    })
    _write_json(data / "qa_report.json", {
        "counts": {
            "documents": 1,
            "passages": 1,
            "source_words": 5,
            "unique_xml_editions_referenced": 1,
        },
        "quality": {"metadata_anomalies_flagged": 0},
    })
    (root / "DATA_CARD.md").write_text("fixture release\n", encoding="utf-8")
    source_sha = _sha(source_archive.read_bytes())
    _write_json(root / "RELEASE_MANIFEST.json", {
        "version": "3.1.0",
        "source_archive": {"bytes": source_archive.stat().st_size, "sha256": source_sha},
        "observed": {"letters": 1, "passages": 1, "source_words": 5, "tei_editions": 1},
    })
    checksums_sha = _refresh_checksums(root)
    contract = ReleaseContract(
        version="3.1.0",
        source_archive_sha256=source_sha,
        checksums_sha256=checksums_sha,
        document_count=1,
        passage_count=1,
        source_word_count=5,
        edition_count=1,
    )
    return root, source_archive, contract


def test_validate_release_checks_pinned_hashes_counts_and_exact_spans(release_files):
    root, source_archive, contract = release_files

    validated = validate_release(root, source_archive, contract=contract)

    assert validated.version == "3.1.0"
    assert validated.document_count == 1
    assert validated.passage_count == 1
    assert validated.source_word_count == 5
    assert validated.release_sha256 == _sha(
        f"danmarks-breve-authentic:{contract.version}\0{contract.source_archive_sha256}\0{contract.checksums_sha256}".encode()
    )


def test_validate_release_rejects_changed_payload_before_import(release_files):
    root, source_archive, contract = release_files
    passages = root / "data" / "authentic" / "passages.jsonl"
    passages.write_text(passages.read_text(encoding="utf-8").replace("passage", "edited"), encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match="checksum mismatch"):
        validate_release(root, source_archive, contract=contract)


def test_validate_release_rejects_bad_source_archive_hash(release_files):
    root, source_archive, contract = release_files
    source_archive.write_bytes(b"tampered archive")

    with pytest.raises(ReleaseValidationError, match="source archive SHA-256"):
        validate_release(root, source_archive, contract=contract)


def test_validate_release_rejects_source_span_mismatch_even_with_updated_checksums(release_files):
    root, source_archive, contract = release_files
    passage_path = root / "data" / "authentic" / "passages.jsonl"
    passage = json.loads(passage_path.read_text(encoding="utf-8"))
    passage["contents"] = "a different quotation"
    passage["text_sha256"] = _sha(passage["contents"].encode())
    _write_jsonl(passage_path, [passage])
    contract = replace(contract, checksums_sha256=_refresh_checksums(root))

    with pytest.raises(ReleaseValidationError, match="source span"):
        validate_release(root, source_archive, contract=contract)


def test_validate_release_rejects_orphan_passage_even_with_updated_checksums(release_files):
    root, source_archive, contract = release_files
    passage_path = root / "data" / "authentic" / "passages.jsonl"
    passage = json.loads(passage_path.read_text(encoding="utf-8"))
    passage["document_id"] = "KBDB_L_missing"
    _write_jsonl(passage_path, [passage])
    contract = replace(contract, checksums_sha256=_refresh_checksums(root))

    with pytest.raises(ReleaseValidationError, match="parent document"):
        validate_release(root, source_archive, contract=contract)


def test_validate_release_rejects_failed_validation_report(release_files):
    root, source_archive, contract = release_files
    report_path = root / "data" / "authentic" / "validation_report.json"
    _write_json(report_path, {"status": "FAIL", "documents": 1, "passages": 1})
    contract = replace(contract, checksums_sha256=_refresh_checksums(root))

    with pytest.raises(ReleaseValidationError, match="validation report"):
        validate_release(root, source_archive, contract=contract)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("HISTORY_TEST_DATABASE_URL"), reason="disposable history test DB not configured"
)
async def test_import_release_is_idempotent_and_persists_exact_rows(release_files, monkeypatch):
    root, source_archive, contract = release_files
    engine = create_async_engine(os.environ["HISTORY_TEST_DATABASE_URL"])
    try:
        first = await import_release(engine, root, source_archive, contract=contract)
        second = await import_release(engine, root, source_archive, contract=contract)
        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        """SELECT
                          (SELECT count(*) FROM history_authentic.documents WHERE release_sha256=:release) AS documents,
                          (SELECT count(*) FROM history_authentic.passages WHERE release_sha256=:release) AS passages,
                          (SELECT count(*) FROM history_authentic.source_editions WHERE release_sha256=:release) AS editions
                        """
                    ),
                    {"release": first.release_sha256},
                )
            ).mappings().one()

        assert first.status == "imported"
        assert second.status == "unchanged"
        assert first.release_sha256 == second.release_sha256
        assert first.metadata_anomaly_count == second.metadata_anomaly_count == 0
        assert dict(counts) == {"documents": 1, "passages": 1, "editions": 1}
        async with engine.begin() as connection:
            stored_anomalies = (
                await connection.execute(
                    text(
                        "SELECT manifest->'qa_report'->'quality'->>'metadata_anomalies_flagged' "
                        "FROM history_authentic.corpus_releases WHERE release_sha256=:release"
                    ),
                    {"release": first.release_sha256},
                )
            ).scalar_one()
        assert stored_anomalies == "0"
        with pytest.raises(DBAPIError, match="rows in ready corpus releases are immutable"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE history_authentic.documents SET rights=rights "
                        "WHERE release_sha256=:release AND id='KBDB_L_fixture'"
                    ),
                    {"release": first.release_sha256},
                )

        stage_release = history_importer._stage_release

        async def conflicting_stage(connection, validated):
            await stage_release(connection, validated)
            await connection.execute(
                text(
                    "UPDATE history_documents_stage SET rights=rights || ' conflict' "
                    "WHERE id='KBDB_L_fixture'"
                )
            )

        monkeypatch.setattr(history_importer, "_stage_release", conflicting_stage)
        with pytest.raises(ImportConflictError, match="conflicting documents row values"):
            await import_release(engine, root, source_archive, contract=contract)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("HISTORY_TEST_DATABASE_URL"), reason="disposable history test DB not configured"
)
async def test_invalid_release_is_logged_without_publishing_corpus_rows(release_files):
    root, source_archive, contract = release_files
    passage_path = root / "data" / "authentic" / "passages.jsonl"
    passage_path.write_text(passage_path.read_text(encoding="utf-8").replace("source-exact", "edited"), encoding="utf-8")
    engine = create_async_engine(os.environ["HISTORY_TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            failed_runs_before = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM history_authentic.ingest_runs "
                        "WHERE status='failed' AND source_archive_sha256=:source_sha"
                    ),
                    {"source_sha": contract.source_archive_sha256},
                )
            ).scalar_one()
        with pytest.raises(ReleaseValidationError, match="checksum mismatch"):
            await import_release(engine, root, source_archive, contract=contract)
        async with engine.connect() as connection:
            published = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM history_authentic.corpus_releases "
                        "WHERE source_archive_sha256=:source_sha"
                    ),
                    {"source_sha": contract.source_archive_sha256},
                )
            ).scalar_one()
            failed_runs = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM history_authentic.ingest_runs "
                        "WHERE status='failed' AND source_archive_sha256=:source_sha"
                    ),
                    {"source_sha": contract.source_archive_sha256},
                )
            ).scalar_one()
        assert published == 0
        assert failed_runs == failed_runs_before + 1
    finally:
        await engine.dispose()
