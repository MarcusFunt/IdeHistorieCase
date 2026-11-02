"""Add an immutable, independently versioned Danmarks Breve corpus."""
from alembic import op

revision = "0006_history_authentic"
down_revision = "0005_historical_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA history_authentic")
    op.execute(
        """
        CREATE TABLE history_authentic.corpus_releases (
            release_sha256 char(64) PRIMARY KEY CHECK (release_sha256 ~ '^[0-9a-f]{64}$'),
            version varchar(32) NOT NULL,
            source_archive_sha256 char(64) NOT NULL CHECK (source_archive_sha256 ~ '^[0-9a-f]{64}$'),
            checksums_sha256 char(64) NOT NULL CHECK (checksums_sha256 ~ '^[0-9a-f]{64}$'),
            source_archive_bytes bigint NOT NULL CHECK (source_archive_bytes > 0),
            document_count integer NOT NULL CHECK (document_count >= 0),
            passage_count integer NOT NULL CHECK (passage_count >= 0),
            source_word_count bigint NOT NULL CHECK (source_word_count >= 0),
            edition_count integer NOT NULL CHECK (edition_count >= 0),
            manifest jsonb NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'staging' CHECK (status IN ('staging', 'ready')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE history_authentic.ingest_runs (
            id uuid PRIMARY KEY,
            release_sha256 char(64),
            version varchar(32) NOT NULL,
            source_archive_sha256 char(64) NOT NULL,
            status varchar(16) NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            outcome varchar(16) CHECK (outcome IS NULL OR outcome IN ('imported', 'unchanged')),
            document_count integer,
            passage_count integer,
            error_summary varchar(2000),
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE history_authentic.source_editions (
            release_sha256 char(64) NOT NULL REFERENCES history_authentic.corpus_releases(release_sha256) ON DELETE RESTRICT,
            edition_xml_filename text NOT NULL,
            source_url text NOT NULL,
            pub_title text,
            pub_author text,
            pub_date text,
            letters integer CHECK (letters IS NULL OR letters >= 0),
            words bigint CHECK (words IS NULL OR words >= 0),
            year_min integer,
            year_max integer,
            source_record jsonb NOT NULL,
            PRIMARY KEY (release_sha256, edition_xml_filename)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE history_authentic.documents (
            release_sha256 char(64) NOT NULL REFERENCES history_authentic.corpus_releases(release_sha256) ON DELETE RESTRICT,
            id text NOT NULL,
            original_text text NOT NULL,
            normalized_text_alternative text,
            selected_variant text NOT NULL,
            source_url text NOT NULL,
            source_xml_filename text NOT NULL,
            year integer CHECK (year IS NULL OR year BETWEEN 1 AND 9999),
            language text NOT NULL,
            language_verified boolean NOT NULL DEFAULT false,
            content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            metadata jsonb NOT NULL,
            provenance jsonb NOT NULL,
            rights text NOT NULL,
            rights_uri text,
            primary_source boolean NOT NULL DEFAULT true,
            document_type text NOT NULL,
            institution text NOT NULL,
            collection text NOT NULL,
            factual_accuracy_checked boolean NOT NULL DEFAULT false,
            ocr_accuracy_checked boolean NOT NULL DEFAULT false,
            source_xml_member_present boolean NOT NULL DEFAULT true,
            source_letter_fingerprint text NOT NULL,
            source_record_row integer NOT NULL DEFAULT 0,
            word_count integer NOT NULL CHECK (word_count >= 0),
            PRIMARY KEY (release_sha256, id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_history_documents_year ON history_authentic.documents (release_sha256, year)"
    )
    op.execute(
        """
        CREATE TABLE history_authentic.passages (
            release_sha256 char(64) NOT NULL,
            id text NOT NULL,
            document_id text NOT NULL,
            contents text NOT NULL,
            char_start integer NOT NULL CHECK (char_start >= 0),
            char_end integer NOT NULL CHECK (char_end > char_start),
            content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            source_url text NOT NULL,
            year integer CHECK (year IS NULL OR year BETWEEN 1 AND 9999),
            language text NOT NULL,
            language_verified boolean NOT NULL DEFAULT false,
            license text NOT NULL,
            primary_source boolean NOT NULL DEFAULT true,
            source_span_verified boolean NOT NULL DEFAULT false,
            historical_truth_verified boolean NOT NULL DEFAULT false,
            source_type text NOT NULL,
            source_variant text NOT NULL,
            word_count integer NOT NULL CHECK (word_count >= 0),
            sender text,
            recipient text,
            source_dataset text,
            lexemes tsvector GENERATED ALWAYS AS (
                to_tsvector('simple'::regconfig, coalesce(contents, ''::text))
            ) STORED,
            PRIMARY KEY (release_sha256, id),
            FOREIGN KEY (release_sha256, document_id)
                REFERENCES history_authentic.documents(release_sha256, id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_history_passages_document ON history_authentic.passages (release_sha256, document_id)"
    )
    op.execute(
        "CREATE INDEX ix_history_passages_year ON history_authentic.passages (release_sha256, year)"
    )
    op.execute(
        "CREATE INDEX ix_history_passages_lexemes ON history_authentic.passages USING gin (lexemes)"
    )
    op.execute(
        """
        CREATE FUNCTION history_authentic.guard_release_state() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'staging' THEN
                    RAISE EXCEPTION 'corpus releases must be created in staging state';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' OR OLD.status = 'ready' THEN
                RAISE EXCEPTION 'ready corpus releases are immutable';
            END IF;
            IF OLD.status <> 'staging' OR NEW.status <> 'ready'
               OR (to_jsonb(OLD) - 'status') IS DISTINCT FROM (to_jsonb(NEW) - 'status') THEN
                RAISE EXCEPTION 'corpus release may only transition from staging to ready';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_corpus_release_state_guard
        BEFORE INSERT OR UPDATE OR DELETE ON history_authentic.corpus_releases
        FOR EACH ROW EXECUTE FUNCTION history_authentic.guard_release_state()
        """
    )
    op.execute(
        """
        CREATE FUNCTION history_authentic.guard_ready_rows() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF EXISTS (
                    SELECT 1 FROM history_authentic.corpus_releases
                    WHERE release_sha256 = NEW.release_sha256 AND status = 'ready'
                ) THEN
                    RAISE EXCEPTION 'rows in ready corpus releases are immutable';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                IF EXISTS (
                    SELECT 1 FROM history_authentic.corpus_releases
                    WHERE release_sha256 = OLD.release_sha256 AND status = 'ready'
                ) THEN
                    RAISE EXCEPTION 'rows in ready corpus releases are immutable';
                END IF;
                RETURN OLD;
            ELSE
                IF EXISTS (
                    SELECT 1 FROM history_authentic.corpus_releases
                    WHERE release_sha256 IN (OLD.release_sha256, NEW.release_sha256)
                      AND status = 'ready'
                ) THEN
                    RAISE EXCEPTION 'rows in ready corpus releases are immutable';
                END IF;
                RETURN NEW;
            END IF;
        END
        $$
        """
    )
    for table_name in ("source_editions", "documents", "passages"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_ready_guard
            BEFORE INSERT OR UPDATE OR DELETE ON history_authentic.{table_name}
            FOR EACH ROW EXECUTE FUNCTION history_authentic.guard_ready_rows()
            """
        )


def downgrade() -> None:
    op.execute("DROP SCHEMA history_authentic CASCADE")
