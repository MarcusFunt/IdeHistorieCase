"""Add version-pinned historical passages, reviews and vector embeddings."""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0005_historical_rag"
down_revision = "0004_multiple_events_per_turn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "rag_scenario_policies",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "scenario_version_id",
            sa.UUID(),
            sa.ForeignKey("scenario_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario_slug", sa.String(80), nullable=False),
        sa.Column("scenario_version", sa.String(32), nullable=False),
        sa.Column("phase_world_dates", postgresql.JSONB(), nullable=False),
        sa.Column("corpus_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scenario_version_id", name="uq_rag_policy_scenario_version"),
        sa.CheckConstraint(
            "corpus_sha256 ~ '^[0-9a-f]{64}$'", name="ck_rag_policy_corpus_sha256"
        ),
        sa.CheckConstraint("jsonb_typeof(phase_world_dates) = 'object'", name="ck_rag_policy_dates_object"),
    )

    op.create_table(
        "historical_passages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "scenario_version_id",
            sa.UUID(),
            sa.ForeignKey("scenario_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario_slug", sa.String(80), nullable=False),
        sa.Column("scenario_version", sa.String(32), nullable=False),
        sa.Column("passage_key", sa.String(96), nullable=False),
        sa.Column("source_identifier", sa.String(32), nullable=False),
        sa.Column("source_title", sa.String(240), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_anchor", sa.String(240), nullable=False),
        sa.Column("rights_basis", sa.Text(), nullable=False),
        sa.Column("passage_text", sa.Text(), nullable=False),
        sa.Column("passage_sha256", sa.String(64), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("in_world_available_from", sa.Date(), nullable=True),
        sa.Column("postgame_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("actor_ids", postgresql.JSONB(), nullable=False),
        sa.Column("visibility_scope", sa.String(24), nullable=False),
        sa.Column("corpus_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "scenario_version_id", "passage_key", name="uq_historical_passage_scenario_key"
        ),
        sa.CheckConstraint("length(passage_text) BETWEEN 1 AND 4000", name="ck_historical_passage_length"),
        sa.CheckConstraint(
            "passage_sha256 ~ '^[0-9a-f]{64}$'", name="ck_historical_passage_sha256"
        ),
        sa.CheckConstraint("corpus_sha256 ~ '^[0-9a-f]{64}$'", name="ck_historical_passage_corpus_sha256"),
        sa.CheckConstraint("jsonb_typeof(actor_ids) = 'array'", name="ck_historical_passage_actor_ids_array"),
        sa.CheckConstraint(
            "(postgame_only AND in_world_available_from IS NULL) OR "
            "(NOT postgame_only AND in_world_available_from IS NOT NULL)",
            name="ck_historical_passage_availability",
        ),
        sa.CheckConstraint(
            "visibility_scope IN ('public', 'actor_limited', 'confidential', 'postgame')",
            name="ck_historical_passage_visibility_scope",
        ),
    )
    op.create_index(
        "ix_historical_passages_scenario_version",
        "historical_passages",
        ["scenario_version_id", "source_identifier"],
    )
    op.create_index(
        "ix_historical_passages_actor_ids",
        "historical_passages",
        ["actor_ids"],
        postgresql_using="gin",
    )
    op.execute(
        "CREATE INDEX historical_passages_search_idx ON historical_passages "
        "USING gin (to_tsvector('simple'::regconfig, passage_text))"
    )

    op.create_table(
        "historical_passage_reviews",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "passage_id",
            sa.UUID(),
            sa.ForeignKey("historical_passages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("rights_status", sa.String(16), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "review_status IN ('approved', 'rejected', 'revoked')",
            name="ck_historical_passage_review_status",
        ),
        sa.CheckConstraint(
            "rights_status IN ('approved', 'pending', 'denied', 'restricted')",
            name="ck_historical_passage_rights_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_historical_passage_review_revision"),
        sa.CheckConstraint("length(reviewer) BETWEEN 1 AND 128", name="ck_historical_passage_reviewer"),
        sa.UniqueConstraint("passage_id", "revision", name="uq_historical_passage_review_revision"),
    )
    op.create_index(
        "ix_historical_passage_reviews_latest",
        "historical_passage_reviews",
        ["passage_id", "revision"],
    )

    op.create_table(
        "historical_passage_embeddings",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "passage_id",
            sa.UUID(),
            sa.ForeignKey("historical_passages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("passage_sha256", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(160), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "passage_id", "passage_sha256", "model_id", name="uq_historical_embedding_revision_model"
        ),
        sa.CheckConstraint(
            "passage_sha256 ~ '^[0-9a-f]{64}$'", name="ck_historical_embedding_passage_sha256"
        ),
        sa.CheckConstraint("length(model_id) BETWEEN 1 AND 160", name="ck_historical_embedding_model_id"),
    )
    op.create_index(
        "historical_embeddings_hnsw_idx",
        "historical_passage_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )

    op.execute(
        """
        CREATE FUNCTION reject_historical_passage_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'historical passages are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_historical_review_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'historical review decisions are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_rag_policy_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'RAG scenario policies are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_historical_embedding_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'historical passage embeddings are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_rag_truncate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'TRUNCATE is not allowed on append-only RAG records';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table, function in (
        ("historical_passages", "reject_historical_passage_mutation"),
        ("historical_passage_reviews", "reject_historical_review_mutation"),
        ("historical_passage_embeddings", "reject_historical_embedding_mutation"),
        ("rag_scenario_policies", "reject_rag_policy_mutation"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_rag_truncate()"
        )


def downgrade() -> None:
    for table in (
        "historical_passage_embeddings",
        "historical_passage_reviews",
        "historical_passages",
        "rag_scenario_policies",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.drop_table("historical_passage_embeddings")
    op.drop_table("historical_passage_reviews")
    op.drop_table("historical_passages")
    op.drop_table("rag_scenario_policies")
    op.execute("DROP FUNCTION reject_historical_embedding_mutation()")
    op.execute("DROP FUNCTION reject_historical_review_mutation()")
    op.execute("DROP FUNCTION reject_historical_passage_mutation()")
    op.execute("DROP FUNCTION reject_rag_policy_mutation()")
    op.execute("DROP FUNCTION reject_rag_truncate()")
    # Leave the shared pgvector extension installed; other application tables may use it.
