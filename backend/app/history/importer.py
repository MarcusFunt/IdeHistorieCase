"""Validation and import support for the pinned Danmarks Breve release."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_VALIDATION_CHECKS = {
    "document_keys_unique",
    "passage_keys_unique",
    "foreign_keys_valid",
    "all_source_offsets_exact",
    "document_sha256",
    "passage_sha256",
}


@dataclass(frozen=True)
class ReleaseContract:
    version: str
    source_archive_sha256: str
    checksums_sha256: str
    document_count: int
    passage_count: int
    source_word_count: int
    edition_count: int = 0


@dataclass(frozen=True)
class ValidatedRelease:
    root: Path
    version: str
    release_sha256: str
    source_archive_sha256: str
    checksums_sha256: str
    document_count: int
    passage_count: int
    source_word_count: int
    edition_count: int
    metadata_anomaly_count: int


EXPECTED_V3_1 = ReleaseContract(
    version="3.1.0",
    source_archive_sha256="c63fdd4299403b92d685e10becfe880e0606aaccfd50dbad180673039fc8ecf3",
    checksums_sha256="322d394c1a16e1e7bd6cfe21444e20a0f9ee131411b6ff7eff7ba05ec88dcc0e",
    document_count=13_516,
    passage_count=78_053,
    source_word_count=7_402_564,
    edition_count=102,
)


class ReleaseValidationError(ValueError):
    """Raised when the supplied release differs from its pinned contract."""


class ImportConflictError(RuntimeError):
    """Raised when an existing release disagrees with the pinned release identity."""


@dataclass(frozen=True)
class ImportResult:
    status: str
    release_sha256: str
    document_count: int
    passage_count: int
    ingest_run_id: UUID
    metadata_anomaly_count: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseValidationError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"{label} must contain a JSON object")
    return value


def _jsonl(path: Path, label: str) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        source = path.open("r", encoding="utf-8")
    except OSError as error:
        raise ReleaseValidationError(f"cannot open {label}: {error}") from error
    with source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReleaseValidationError(
                    f"invalid JSON in {label} at line {line_number}: {error.msg}"
                ) from error
            if not isinstance(row, dict):
                raise ReleaseValidationError(
                    f"{label} line {line_number} must contain a JSON object"
                )
            yield line_number, row


def _safe_manifest_member(root: Path, relative_name: str) -> Path:
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseValidationError(f"unsafe SHA256SUMS path: {relative_name}")
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ReleaseValidationError(
            f"missing SHA256SUMS member {relative_name}: {error}"
        ) from error
    if not resolved.is_relative_to(root.resolve()):
        raise ReleaseValidationError(f"SHA256SUMS member escapes release directory: {relative_name}")
    if not resolved.is_file():
        raise ReleaseValidationError(f"SHA256SUMS member is not a file: {relative_name}")
    return resolved


def _verify_sha256s(root: Path, contract: ReleaseContract) -> str:
    sums_path = root / "SHA256SUMS.txt"
    try:
        sums_bytes = sums_path.read_bytes()
    except OSError as error:
        raise ReleaseValidationError(f"cannot read SHA256SUMS.txt: {error}") from error
    sums_hash = _sha256_bytes(sums_bytes)
    if sums_hash != contract.checksums_sha256:
        raise ReleaseValidationError("SHA256SUMS.txt does not match the pinned release")

    required = {
        "DATA_CARD.md",
        "RELEASE_MANIFEST.json",
        "data/authentic/documents.jsonl",
        "data/authentic/passages.jsonl",
        "data/authentic/source_editions.jsonl",
        "data/authentic/qa_report.json",
        "data/authentic/validation_report.json",
    }
    found: set[str] = set()
    seen: set[str] = set()
    try:
        rows = sums_bytes.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ReleaseValidationError("SHA256SUMS.txt must be ASCII") from error
    for line_number, row in enumerate(rows, start=1):
        parts = row.split(None, 1)
        if len(parts) != 2 or not SHA256_RE.fullmatch(parts[0]):
            raise ReleaseValidationError(f"malformed SHA256SUMS.txt line {line_number}")
        expected, relative_name = parts
        relative_name = relative_name.strip().lstrip("*")
        if relative_name in seen:
            raise ReleaseValidationError(f"duplicate SHA256SUMS path: {relative_name}")
        seen.add(relative_name)
        path = _safe_manifest_member(root, relative_name)
        if _sha256_file(path) != expected:
            raise ReleaseValidationError(f"checksum mismatch for {relative_name}")
        found.add(relative_name)
    missing = required - found
    if missing:
        raise ReleaseValidationError(
            "SHA256SUMS.txt is missing required files: " + ", ".join(sorted(missing))
        )
    return sums_hash


def _required_text(row: dict[str, Any], key: str, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseValidationError(f"{label}.{key} must be a non-empty string")
    return value


def _optional_year(value: Any, label: str) -> None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 9999
    ):
        raise ReleaseValidationError(f"{label}.year must be an integer or null")


def _validate_rows(root: Path, contract: ReleaseContract) -> tuple[int, int, int, int]:
    data_root = root / "data" / "authentic"
    documents_path = data_root / "documents.jsonl"
    passages_path = data_root / "passages.jsonl"
    editions_path = data_root / "source_editions.jsonl"
    doc_texts: dict[str, str] = {}
    document_editions: set[str] = set()
    document_count = source_words = 0

    for line_number, row in _jsonl(documents_path, "documents.jsonl"):
        label = f"documents.jsonl line {line_number}"
        document_id = _required_text(row, "id", label)
        if document_id in doc_texts:
            raise ReleaseValidationError(f"duplicate document ID: {document_id}")
        original_text = row.get("original_text")
        if not isinstance(original_text, str):
            raise ReleaseValidationError(f"{label}.original_text must be a string")
        text_hash = _required_text(row, "text_sha256", label)
        if not SHA256_RE.fullmatch(text_hash) or _sha256_bytes(original_text.encode("utf-8")) != text_hash:
            raise ReleaseValidationError(f"document text SHA-256 mismatch for {document_id}")
        _required_text(row, "selected_variant", label)
        _required_text(row, "source_url", label)
        document_editions.add(_required_text(row, "source_xml_filename", label))
        _required_text(row, "language", label)
        _required_text(row, "rights", label)
        if not isinstance(row.get("metadata"), dict) or not isinstance(row.get("provenance"), dict):
            raise ReleaseValidationError(f"{label} metadata and provenance must be JSON objects")
        for field in (
            "language_verified", "primary_source", "factual_accuracy_checked",
            "ocr_accuracy_checked", "source_xml_member_present",
        ):
            if field in row and not isinstance(row[field], bool):
                raise ReleaseValidationError(f"{label}.{field} must be a boolean")
        _optional_year(row.get("year"), label)
        word_count = row.get("word_count")
        if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count < 0:
            raise ReleaseValidationError(f"{label}.word_count must be a non-negative integer")
        doc_texts[document_id] = original_text
        source_words += word_count
        document_count += 1

    passage_ids: set[str] = set()
    passage_count = 0
    for line_number, row in _jsonl(passages_path, "passages.jsonl"):
        label = f"passages.jsonl line {line_number}"
        passage_id = _required_text(row, "id", label)
        if passage_id in passage_ids:
            raise ReleaseValidationError(f"duplicate passage ID: {passage_id}")
        passage_ids.add(passage_id)
        document_id = _required_text(row, "document_id", label)
        if document_id not in doc_texts:
            raise ReleaseValidationError(f"passage {passage_id} has no parent document")
        contents = row.get("contents")
        if not isinstance(contents, str):
            raise ReleaseValidationError(f"{label}.contents must be a string")
        start, end = row.get("char_start"), row.get("char_end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(doc_texts[document_id])
        ):
            raise ReleaseValidationError(f"passage {passage_id} has invalid source offsets")
        if doc_texts[document_id][start:end] != contents:
            raise ReleaseValidationError(f"passage {passage_id} source span does not match its parent document")
        text_hash = _required_text(row, "text_sha256", label)
        if not SHA256_RE.fullmatch(text_hash) or _sha256_bytes(contents.encode("utf-8")) != text_hash:
            raise ReleaseValidationError(f"passage text SHA-256 mismatch for {passage_id}")
        _required_text(row, "source_url", label)
        _required_text(row, "language", label)
        for field in (
            "language_verified", "primary_source", "source_span_verified",
            "historical_truth_verified",
        ):
            if field in row and not isinstance(row[field], bool):
                raise ReleaseValidationError(f"{label}.{field} must be a boolean")
        word_count = row.get("word_count", 0)
        if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count < 0:
            raise ReleaseValidationError(f"{label}.word_count must be a non-negative integer")
        _optional_year(row.get("year"), label)
        passage_count += 1

    edition_count = 0
    edition_ids: set[str] = set()
    for line_number, row in _jsonl(editions_path, "source_editions.jsonl"):
        label = f"source_editions.jsonl line {line_number}"
        edition_id = _required_text(row, "edition_xml_filename", label)
        if edition_id in edition_ids:
            raise ReleaseValidationError(f"duplicate source edition: {edition_id}")
        _required_text(row, "source_url", label)
        for field in ("letters", "words", "year_min", "year_max"):
            value = row.get(field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ReleaseValidationError(f"{label}.{field} must be an integer or null")
        edition_ids.add(edition_id)
        edition_count += 1

    if document_count != contract.document_count:
        raise ReleaseValidationError(
            f"document count mismatch: expected {contract.document_count}, found {document_count}"
        )
    if passage_count != contract.passage_count:
        raise ReleaseValidationError(
            f"passage count mismatch: expected {contract.passage_count}, found {passage_count}"
        )
    if source_words != contract.source_word_count:
        raise ReleaseValidationError(
            f"source word count mismatch: expected {contract.source_word_count}, found {source_words}"
        )
    if contract.edition_count and edition_count != contract.edition_count:
        raise ReleaseValidationError(
            f"source edition count mismatch: expected {contract.edition_count}, found {edition_count}"
        )
    if document_editions - edition_ids:
        missing = sorted(document_editions - edition_ids)
        raise ReleaseValidationError(
            "documents refer to missing source editions: " + ", ".join(missing[:5])
        )
    return document_count, passage_count, source_words, edition_count


def validate_release(
    release_dir: str | Path,
    source_archive: str | Path,
    *,
    contract: ReleaseContract = EXPECTED_V3_1,
) -> ValidatedRelease:
    """Verify the pinned release and every document/passage before database writes."""
    root = Path(release_dir).resolve()
    archive = Path(source_archive).resolve()
    if not root.is_dir():
        raise ReleaseValidationError(f"release directory does not exist: {root}")
    if not archive.is_file():
        raise ReleaseValidationError(f"source archive does not exist: {archive}")

    checksums_sha256 = _verify_sha256s(root, contract)
    manifest = _json(root / "RELEASE_MANIFEST.json", "RELEASE_MANIFEST.json")
    if manifest.get("version") != contract.version:
        raise ReleaseValidationError(
            f"release version mismatch: expected {contract.version}, found {manifest.get('version')}"
        )
    source = manifest.get("source_archive")
    observed = manifest.get("observed")
    if not isinstance(source, dict) or not isinstance(observed, dict):
        raise ReleaseValidationError("release manifest is missing source_archive or observed counts")
    if source.get("sha256") != contract.source_archive_sha256:
        raise ReleaseValidationError("release manifest source archive SHA-256 does not match the pinned release")
    if _sha256_file(archive) != contract.source_archive_sha256:
        raise ReleaseValidationError("source archive SHA-256 does not match the pinned release")
    if archive.stat().st_size != source.get("bytes"):
        raise ReleaseValidationError("source archive byte count does not match the release manifest")

    data_root = root / "data" / "authentic"
    validation = _json(data_root / "validation_report.json", "validation_report.json")
    if validation.get("status") != "PASS":
        raise ReleaseValidationError("validation report is not PASS")
    checks = validation.get("checks")
    if not isinstance(checks, list) or not all(isinstance(item, str) for item in checks):
        raise ReleaseValidationError("validation report checks must be a list of names")
    if not REQUIRED_VALIDATION_CHECKS.issubset(set(checks)):
        raise ReleaseValidationError("validation report does not include all required integrity checks")
    qa = _json(data_root / "qa_report.json", "qa_report.json")
    counts = qa.get("counts")
    if not isinstance(counts, dict):
        raise ReleaseValidationError("QA report is missing counts")
    quality = qa.get("quality")
    if not isinstance(quality, dict):
        raise ReleaseValidationError("QA report is missing quality summary")
    metadata_anomalies = quality.get("metadata_anomalies_flagged")
    if (
        not isinstance(metadata_anomalies, int)
        or isinstance(metadata_anomalies, bool)
        or metadata_anomalies < 0
    ):
        raise ReleaseValidationError("QA report metadata anomaly count must be a non-negative integer")

    expected_counts = {
        "letters": contract.document_count,
        "passages": contract.passage_count,
        "source_words": contract.source_word_count,
        "tei_editions": contract.edition_count,
    }
    observed_counts = {
        "letters": observed.get("letters"),
        "passages": observed.get("passages"),
        "source_words": observed.get("source_words"),
        "tei_editions": observed.get("tei_editions"),
    }
    if observed_counts != expected_counts:
        raise ReleaseValidationError(f"release manifest counts do not match the pinned contract: {observed_counts}")
    if (
        validation.get("documents") != contract.document_count
        or validation.get("passages") != contract.passage_count
        or counts.get("documents") != contract.document_count
        or counts.get("passages") != contract.passage_count
        or counts.get("source_words") != contract.source_word_count
        or counts.get("unique_xml_editions_referenced") != contract.edition_count
    ):
        raise ReleaseValidationError("validation/QA report counts do not match the pinned contract")

    documents, passages, source_words, editions = _validate_rows(root, contract)
    release_digest = hashlib.sha256(
        f"danmarks-breve-authentic:{contract.version}\0{contract.source_archive_sha256}\0{checksums_sha256}".encode(
            "ascii"
        )
    ).hexdigest()
    return ValidatedRelease(
        root=root,
        version=contract.version,
        release_sha256=release_digest,
        source_archive_sha256=contract.source_archive_sha256,
        checksums_sha256=checksums_sha256,
        document_count=documents,
        passage_count=passages,
        source_word_count=source_words,
        edition_count=editions,
        metadata_anomaly_count=metadata_anomalies,
    )


DOCUMENT_COLUMNS = """
release_sha256, id, original_text, normalized_text_alternative, selected_variant,
source_url, source_xml_filename, year, language, language_verified, content_sha256,
metadata, provenance, rights, rights_uri, primary_source, document_type, institution,
collection, factual_accuracy_checked, ocr_accuracy_checked, source_xml_member_present,
source_letter_fingerprint, source_record_row, word_count
"""
PASSAGE_COLUMNS = """
release_sha256, id, document_id, contents, char_start, char_end, content_sha256,
source_url, year, language, language_verified, license, primary_source,
source_span_verified, historical_truth_verified, source_type, source_variant,
word_count, sender, recipient, source_dataset
"""
EDITION_COLUMNS = """
release_sha256, edition_xml_filename, source_url, pub_title, pub_author, pub_date,
letters, words, year_min, year_max, source_record
"""

DOCUMENT_STAGE_INSERT = text(
    """INSERT INTO history_documents_stage
    (release_sha256, id, original_text, normalized_text_alternative, selected_variant,
     source_url, source_xml_filename, year, language, language_verified, content_sha256,
     metadata, provenance, rights, rights_uri, primary_source, document_type, institution,
     collection, factual_accuracy_checked, ocr_accuracy_checked, source_xml_member_present,
     source_letter_fingerprint, source_record_row, word_count)
    VALUES (:release_sha256, :id, :original_text, :normalized_text_alternative, :selected_variant,
     :source_url, :source_xml_filename, :year, :language, :language_verified, :content_sha256,
     CAST(:metadata AS jsonb), CAST(:provenance AS jsonb), :rights, :rights_uri,
     :primary_source, :document_type, :institution, :collection, :factual_accuracy_checked,
     :ocr_accuracy_checked, :source_xml_member_present, :source_letter_fingerprint,
     :source_record_row, :word_count)"""
)
PASSAGE_STAGE_INSERT = text(
    """INSERT INTO history_passages_stage
    (release_sha256, id, document_id, contents, char_start, char_end, content_sha256,
     source_url, year, language, language_verified, license, primary_source,
     source_span_verified, historical_truth_verified, source_type, source_variant,
     word_count, sender, recipient, source_dataset)
    VALUES (:release_sha256, :id, :document_id, :contents, :char_start, :char_end,
     :content_sha256, :source_url, :year, :language, :language_verified, :license,
     :primary_source, :source_span_verified, :historical_truth_verified, :source_type,
     :source_variant, :word_count, :sender, :recipient, :source_dataset)"""
)
EDITION_STAGE_INSERT = text(
    """INSERT INTO history_editions_stage
    (release_sha256, edition_xml_filename, source_url, pub_title, pub_author, pub_date,
     letters, words, year_min, year_max, source_record)
    VALUES (:release_sha256, :edition_xml_filename, :source_url, :pub_title, :pub_author,
     :pub_date, :letters, :words, :year_min, :year_max, CAST(:source_record AS jsonb))"""
)


def _document_values(row: dict[str, Any], release_sha256: str) -> dict[str, Any]:
    return {
        "release_sha256": release_sha256,
        "id": row["id"],
        "original_text": row["original_text"],
        "normalized_text_alternative": row.get("normalized_text_alternative"),
        "selected_variant": row["selected_variant"],
        "source_url": row["source_url"],
        "source_xml_filename": row["source_xml_filename"],
        "year": row.get("year"),
        "language": row["language"],
        "language_verified": bool(row.get("language_verified", False)),
        "content_sha256": row["text_sha256"],
        "metadata": json.dumps(row["metadata"], ensure_ascii=False),
        "provenance": json.dumps(row["provenance"], ensure_ascii=False),
        "rights": row["rights"],
        "rights_uri": row.get("rights_uri"),
        "primary_source": bool(row.get("primary_source", True)),
        "document_type": row.get("document_type", "historical_letter_printed_edition_OCR"),
        "institution": row.get("institution", "Det Kgl. Bibliotek"),
        "collection": row.get("collection", "Danmarks Breve"),
        "factual_accuracy_checked": bool(row.get("factual_accuracy_checked", False)),
        "ocr_accuracy_checked": bool(row.get("ocr_accuracy_checked", False)),
        "source_xml_member_present": bool(row.get("source_xml_member_present", True)),
        "source_letter_fingerprint": row.get("source_letter_fingerprint", row["text_sha256"]),
        "source_record_row": int(row.get("source_record_row", 0)),
        "word_count": row["word_count"],
    }


def _passage_values(row: dict[str, Any], release_sha256: str) -> dict[str, Any]:
    return {
        "release_sha256": release_sha256,
        "id": row["id"],
        "document_id": row["document_id"],
        "contents": row["contents"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "content_sha256": row["text_sha256"],
        "source_url": row["source_url"],
        "year": row.get("year"),
        "language": row["language"],
        "language_verified": bool(row.get("language_verified", False)),
        "license": row.get("license", ""),
        "primary_source": bool(row.get("primary_source", True)),
        "source_span_verified": bool(row.get("source_span_verified", False)),
        "historical_truth_verified": bool(row.get("historical_truth_verified", False)),
        "source_type": row.get("source_type", "historical_letter_ocr"),
        "source_variant": row.get("source_variant", "raw_text"),
        "word_count": row.get("word_count", 0),
        "sender": row.get("sender"),
        "recipient": row.get("recipient"),
        "source_dataset": row.get("source_dataset"),
    }


def _edition_values(row: dict[str, Any], release_sha256: str) -> dict[str, Any]:
    return {
        "release_sha256": release_sha256,
        "edition_xml_filename": row["edition_xml_filename"],
        "source_url": row["source_url"],
        "pub_title": row.get("pubTitle"),
        "pub_author": row.get("pubAuthor"),
        "pub_date": row.get("pubDate"),
        "letters": row.get("letters"),
        "words": row.get("words"),
        "year_min": row.get("year_min"),
        "year_max": row.get("year_max"),
        "source_record": json.dumps(row, ensure_ascii=False),
    }


async def _record_failed_run(
    engine: AsyncEngine,
    run_id: UUID,
    contract: ReleaseContract,
    summary: str,
    *,
    run_exists: bool,
) -> None:
    async with engine.begin() as connection:
        if run_exists:
            await connection.execute(
                text(
                    """UPDATE history_authentic.ingest_runs
                    SET status='failed', error_summary=:summary, finished_at=now()
                    WHERE id=:id"""
                ),
                {"id": run_id, "summary": summary[:2000]},
            )
        else:
            await connection.execute(
                text(
                    """INSERT INTO history_authentic.ingest_runs
                    (id, version, source_archive_sha256, status, error_summary, finished_at)
                    VALUES (:id, :version, :source_sha, 'failed', :summary, now())"""
                ),
                {
                    "id": run_id,
                    "version": contract.version,
                    "source_sha": contract.source_archive_sha256,
                    "summary": summary[:2000],
                },
            )


async def _insert_batches(connection, statement, rows, batch_size: int = 500) -> int:
    batch: list[dict[str, Any]] = []
    count = 0
    for row in rows:
        batch.append(row)
        if len(batch) == batch_size:
            await connection.execute(statement, batch)
            count += len(batch)
            batch.clear()
    if batch:
        await connection.execute(statement, batch)
        count += len(batch)
    return count


async def _stage_release(connection, validated: ValidatedRelease) -> None:
    await connection.exec_driver_sql(
        "CREATE TEMP TABLE history_documents_stage "
        "(LIKE history_authentic.documents INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"
    )
    await connection.exec_driver_sql(
        "CREATE TEMP TABLE history_passages_stage "
        "(LIKE history_authentic.passages INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING GENERATED) ON COMMIT DROP"
    )
    await connection.exec_driver_sql(
        "CREATE TEMP TABLE history_editions_stage "
        "(LIKE history_authentic.source_editions INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"
    )

    data_root = validated.root / "data" / "authentic"
    edition_rows = (
        _edition_values(row, validated.release_sha256)
        for _, row in _jsonl(data_root / "source_editions.jsonl", "source_editions.jsonl")
    )
    documents = (
        _document_values(row, validated.release_sha256)
        for _, row in _jsonl(data_root / "documents.jsonl", "documents.jsonl")
    )
    passages = (
        _passage_values(row, validated.release_sha256)
        for _, row in _jsonl(data_root / "passages.jsonl", "passages.jsonl")
    )
    counts = (
        await _insert_batches(connection, EDITION_STAGE_INSERT, edition_rows),
        await _insert_batches(connection, DOCUMENT_STAGE_INSERT, documents),
        await _insert_batches(connection, PASSAGE_STAGE_INSERT, passages),
    )
    if counts != (validated.edition_count, validated.document_count, validated.passage_count):
        raise ImportConflictError("staged row counts differ from the validated release")

    orphan_count = (
        await connection.execute(
            text(
                """SELECT count(*) FROM history_passages_stage p
                   LEFT JOIN history_documents_stage d
                     ON d.release_sha256=p.release_sha256 AND d.id=p.document_id
                   WHERE d.id IS NULL"""
            )
        )
    ).scalar_one()
    if orphan_count:
        raise ImportConflictError(f"staging contains {orphan_count} orphan passages")
    missing_edition_count = (
        await connection.execute(
            text(
                """SELECT count(*) FROM history_documents_stage d
                   LEFT JOIN history_editions_stage e
                     ON e.release_sha256=d.release_sha256
                    AND e.edition_xml_filename=d.source_xml_filename
                   WHERE e.edition_xml_filename IS NULL"""
            )
        )
    ).scalar_one()
    if missing_edition_count:
        raise ImportConflictError(f"staging contains {missing_edition_count} documents without an edition")
    bad_span_count = (
        await connection.execute(
            text(
                """SELECT count(*) FROM history_passages_stage p
                   JOIN history_documents_stage d
                     ON d.release_sha256=p.release_sha256 AND d.id=p.document_id
                   WHERE substring(d.original_text from p.char_start + 1
                                   for p.char_end - p.char_start) <> p.contents"""
            )
        )
    ).scalar_one()
    if bad_span_count:
        raise ImportConflictError(f"staging contains {bad_span_count} invalid passage spans")


async def _assert_existing_rows_match(connection, release_sha256: str) -> None:
    tables = (
        ("documents", "history_documents_stage", DOCUMENT_COLUMNS),
        ("passages", "history_passages_stage", PASSAGE_COLUMNS),
        ("source_editions", "history_editions_stage", EDITION_COLUMNS),
    )
    for table_name, stage_name, column_list in tables:
        columns = ", ".join(column.strip() for column in column_list.split(","))
        mismatches = (
            await connection.execute(
                text(
                    f"""SELECT count(*) FROM (
                       (SELECT {columns} FROM {stage_name}
                        EXCEPT ALL
                        SELECT {columns} FROM history_authentic.{table_name}
                        WHERE release_sha256=:release)
                       UNION ALL
                       (SELECT {columns} FROM history_authentic.{table_name}
                        WHERE release_sha256=:release
                        EXCEPT ALL
                        SELECT {columns} FROM {stage_name})
                    ) AS row_differences"""
                ),
                {"release": release_sha256},
            )
        ).scalar_one()
        if mismatches:
            raise ImportConflictError(
                f"existing corpus release contains {mismatches} conflicting {table_name} row values"
            )


def _release_metadata(root: Path) -> dict[str, Any]:
    data_root = root / "data" / "authentic"
    return {
        "release_manifest": _json(root / "RELEASE_MANIFEST.json", "RELEASE_MANIFEST.json"),
        "qa_report": _json(data_root / "qa_report.json", "qa_report.json"),
        "validation_report": _json(data_root / "validation_report.json", "validation_report.json"),
    }


async def import_release(
    engine: AsyncEngine,
    release_dir: str | Path,
    source_archive: str | Path,
    *,
    contract: ReleaseContract = EXPECTED_V3_1,
) -> ImportResult:
    """Validate, stage, and atomically publish an immutable corpus release."""
    run_id = uuid4()
    try:
        validated = validate_release(release_dir, source_archive, contract=contract)
    except ReleaseValidationError as error:
        try:
            await _record_failed_run(engine, run_id, contract, str(error), run_exists=False)
        except Exception:
            pass
        raise

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO history_authentic.ingest_runs
                    (id, release_sha256, version, source_archive_sha256, status)
                    VALUES (:id, :release, :version, :source_sha, 'running')"""
                ),
                {
                    "id": run_id,
                    "release": validated.release_sha256,
                    "version": validated.version,
                    "source_sha": validated.source_archive_sha256,
                },
            )

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:release, 0))"),
                {"release": validated.release_sha256},
            )
            await _stage_release(connection, validated)
            existing = (
                await connection.execute(
                    text(
                        """SELECT status, version, source_archive_sha256, checksums_sha256,
                                  document_count, passage_count, source_word_count, edition_count,
                                  manifest
                           FROM history_authentic.corpus_releases WHERE release_sha256=:release"""
                    ),
                    {"release": validated.release_sha256},
                )
            ).mappings().one_or_none()
            if existing is not None:
                expected = {
                    "status": "ready",
                    "version": validated.version,
                    "source_archive_sha256": validated.source_archive_sha256,
                    "checksums_sha256": validated.checksums_sha256,
                    "document_count": validated.document_count,
                    "passage_count": validated.passage_count,
                    "source_word_count": validated.source_word_count,
                    "edition_count": validated.edition_count,
                    "manifest": _release_metadata(validated.root),
                }
                if dict(existing) != expected:
                    raise ImportConflictError(
                        "an existing corpus release has metadata that conflicts with the pinned input"
                    )
                counts = (
                    await connection.execute(
                        text(
                            """SELECT
                              (SELECT count(*) FROM history_authentic.documents WHERE release_sha256=:release) AS documents,
                              (SELECT count(*) FROM history_authentic.passages WHERE release_sha256=:release) AS passages,
                              (SELECT count(*) FROM history_authentic.source_editions WHERE release_sha256=:release) AS editions"""
                        ),
                        {"release": validated.release_sha256},
                    )
                ).mappings().one()
                if dict(counts) != {
                    "documents": validated.document_count,
                    "passages": validated.passage_count,
                    "editions": validated.edition_count,
                }:
                    raise ImportConflictError("an existing corpus release is incomplete")
                await _assert_existing_rows_match(connection, validated.release_sha256)
                await connection.execute(
                    text(
                        """UPDATE history_authentic.ingest_runs
                           SET status='succeeded', outcome='unchanged', release_sha256=:release,
                               document_count=:documents, passage_count=:passages, finished_at=now()
                           WHERE id=:id"""
                    ),
                    {
                        "id": run_id,
                        "release": validated.release_sha256,
                        "documents": validated.document_count,
                        "passages": validated.passage_count,
                    },
                )
                return ImportResult(
                    "unchanged",
                    validated.release_sha256,
                    validated.document_count,
                    validated.passage_count,
                    run_id,
                    validated.metadata_anomaly_count,
                )

            manifest = _release_metadata(validated.root)
            source_bytes = manifest["release_manifest"]["source_archive"]["bytes"]
            await connection.execute(
                text(
                    """INSERT INTO history_authentic.corpus_releases
                    (release_sha256, version, source_archive_sha256, checksums_sha256,
                     source_archive_bytes, document_count, passage_count, source_word_count,
                     edition_count, manifest, status)
                    VALUES (:release, :version, :source_sha, :checksums_sha, :source_bytes,
                            :documents, :passages, :source_words, :editions,
                            CAST(:manifest AS jsonb), 'staging')"""
                ),
                {
                    "release": validated.release_sha256,
                    "version": validated.version,
                    "source_sha": validated.source_archive_sha256,
                    "checksums_sha": validated.checksums_sha256,
                    "source_bytes": source_bytes,
                    "documents": validated.document_count,
                    "passages": validated.passage_count,
                    "source_words": validated.source_word_count,
                    "editions": validated.edition_count,
                    "manifest": json.dumps(manifest, ensure_ascii=False),
                },
            )
            await connection.exec_driver_sql(
                f"INSERT INTO history_authentic.source_editions ({EDITION_COLUMNS}) "
                "SELECT " + EDITION_COLUMNS + " FROM history_editions_stage"
            )
            await connection.exec_driver_sql(
                f"INSERT INTO history_authentic.documents ({DOCUMENT_COLUMNS}) "
                "SELECT " + DOCUMENT_COLUMNS + " FROM history_documents_stage"
            )
            await connection.exec_driver_sql(
                f"INSERT INTO history_authentic.passages ({PASSAGE_COLUMNS}) "
                "SELECT " + PASSAGE_COLUMNS + " FROM history_passages_stage"
            )
            await connection.execute(
                text(
                    "UPDATE history_authentic.corpus_releases SET status='ready' WHERE release_sha256=:release"
                ),
                {"release": validated.release_sha256},
            )
            await connection.execute(
                text(
                    """UPDATE history_authentic.ingest_runs
                       SET status='succeeded', outcome='imported', release_sha256=:release,
                           document_count=:documents, passage_count=:passages, finished_at=now()
                       WHERE id=:id"""
                ),
                {
                    "id": run_id,
                    "release": validated.release_sha256,
                    "documents": validated.document_count,
                    "passages": validated.passage_count,
                },
            )
        return ImportResult(
            "imported",
            validated.release_sha256,
            validated.document_count,
            validated.passage_count,
            run_id,
            validated.metadata_anomaly_count,
        )
    except Exception as error:
        try:
            await _record_failed_run(engine, run_id, contract, str(error), run_exists=True)
        except Exception:
            pass
        raise
