"""Immutable, separately versioned authentic historical-source corpus."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base

SCHEMA = "history_authentic"


class CorpusRelease(Base):
    __tablename__ = "corpus_releases"
    __table_args__ = (
        CheckConstraint("status IN ('staging', 'ready')"),
        CheckConstraint("document_count >= 0 AND passage_count >= 0 AND source_word_count >= 0"),
        {"schema": SCHEMA},
    )

    release_sha256: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_archive_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    checksums_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_archive_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passage_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_word_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    edition_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'staging'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')"),
        CheckConstraint("outcome IS NULL OR outcome IN ('imported', 'unchanged')"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    release_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_archive_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16))
    document_count: Mapped[int | None] = mapped_column(Integer)
    passage_count: Mapped[int | None] = mapped_column(Integer)
    error_summary: Mapped[str | None] = mapped_column(String(2000))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceEdition(Base):
    __tablename__ = "source_editions"
    __table_args__ = (
        PrimaryKeyConstraint("release_sha256", "edition_xml_filename"),
        ForeignKeyConstraint(
            ["release_sha256"], [f"{SCHEMA}.corpus_releases.release_sha256"], ondelete="RESTRICT"
        ),
        {"schema": SCHEMA},
    )

    release_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    edition_xml_filename: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    pub_title: Mapped[str | None] = mapped_column(Text)
    pub_author: Mapped[str | None] = mapped_column(Text)
    pub_date: Mapped[str | None] = mapped_column(Text)
    letters: Mapped[int | None] = mapped_column(Integer)
    words: Mapped[int | None] = mapped_column(BigInteger)
    year_min: Mapped[int | None] = mapped_column(Integer)
    year_max: Mapped[int | None] = mapped_column(Integer)
    source_record: Mapped[dict] = mapped_column(JSONB, nullable=False)


class HistoryDocument(Base):
    __tablename__ = "documents"
    __table_args__ = (
        PrimaryKeyConstraint("release_sha256", "id"),
        ForeignKeyConstraint(
            ["release_sha256"], [f"{SCHEMA}.corpus_releases.release_sha256"], ondelete="RESTRICT"
        ),
        CheckConstraint("year IS NULL OR year BETWEEN 1 AND 9999"),
        CheckConstraint("word_count >= 0"),
        Index("ix_history_documents_year", "release_sha256", "year"),
        {"schema": SCHEMA},
    )

    release_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    id: Mapped[str] = mapped_column(Text, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text_alternative: Mapped[str | None] = mapped_column(Text)
    selected_variant: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_xml_filename: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    language_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rights: Mapped[str] = mapped_column(Text, nullable=False)
    rights_uri: Mapped[str | None] = mapped_column(Text)
    primary_source: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    institution: Mapped[str] = mapped_column(Text, nullable=False)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    factual_accuracy_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ocr_accuracy_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source_xml_member_present: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    source_letter_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_row: Mapped[int] = mapped_column(Integer, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)


class HistoryPassage(Base):
    __tablename__ = "passages"
    __table_args__ = (
        PrimaryKeyConstraint("release_sha256", "id"),
        ForeignKeyConstraint(
            ["release_sha256", "document_id"],
            [f"{SCHEMA}.documents.release_sha256", f"{SCHEMA}.documents.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("char_start >= 0 AND char_end > char_start"),
        CheckConstraint("year IS NULL OR year BETWEEN 1 AND 9999"),
        CheckConstraint("word_count >= 0"),
        Index("ix_history_passages_document", "release_sha256", "document_id"),
        Index("ix_history_passages_year", "release_sha256", "year"),
        Index("ix_history_passages_lexemes", "lexemes", postgresql_using="gin"),
        {"schema": SCHEMA},
    )

    release_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str] = mapped_column(Text, nullable=False)
    contents: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    language_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    license: Mapped[str] = mapped_column(Text, nullable=False)
    primary_source: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    source_span_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    historical_truth_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_variant: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sender: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str | None] = mapped_column(Text)
    source_dataset: Mapped[str | None] = mapped_column(Text)
    lexemes: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple'::regconfig, COALESCE(contents, ''::text))", persisted=True),
    )
