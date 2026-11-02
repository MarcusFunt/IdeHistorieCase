"""Real PostgreSQL full text/vector retrieval and pre/post eligibility filters."""
import hashlib
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select

from app.db.connection import engine, session_factory
from app.db.models import (
    HistoricalPassage,
    HistoricalPassageEmbedding,
    HistoricalPassageReview,
    RagScenarioPolicy,
    ScenarioVersion,
)
from app.rag.policy import EvidenceContext
from app.rag.postgres import PostgresRagRepository
from app.rag.repository import record_review
from app.scenarios.loader import load_scenario
from app.scenarios.repository import import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "scenarios" / "versailles-1919" / "2.5.yaml"
MODEL_ID = "rag-integration-fake"
VECTOR = [1.0] + [0.0] * 1023


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_lexical_dense_scenario_and_review_prefilters_and_revalidation():
    async with session_factory() as db:
        transaction = await db.begin()
        try:
            authored = await import_scenario(db, load_scenario(MANIFEST))
            scenario = authored.scenario
            legacy = (
                await db.execute(
                    select(ScenarioVersion).where(
                        ScenarioVersion.slug == "versailles-1919",
                        ScenarioVersion.version == "1",
                    )
                )
            ).scalar_one()
            corpus_hash = "a" * 64
            db.add(
                RagScenarioPolicy(
                    id=uuid4(),
                    scenario_version_id=scenario.id,
                    scenario_slug=scenario.slug,
                    scenario_version=scenario.version,
                    phase_world_dates={"opening_context": "1919-03-20"},
                    corpus_sha256=corpus_hash,
                )
            )
            await db.flush()
            approved = HistoricalPassage(
                id=uuid4(), scenario_version_id=scenario.id,
                scenario_slug=scenario.slug, scenario_version=scenario.version,
                passage_key="integration-approved", source_identifier="R04",
                source_title="Wilson Fourteen Points", source_url="https://example.test/r04",
                source_anchor="Point I", rights_basis="Test fixture; human review is simulated.",
                passage_text="Open covenants of peace, openly arrived at.",
                passage_sha256=hashlib.sha256(
                    b"Open covenants of peace, openly arrived at."
                ).hexdigest(), document_date=date(1918, 1, 8),
                in_world_available_from=date(1918, 1, 8), postgame_only=False,
                actor_ids=["britain"], visibility_scope="actor_limited", corpus_sha256=corpus_hash,
            )
            rights_pending = HistoricalPassage(
                id=uuid4(), scenario_version_id=scenario.id,
                scenario_slug=scenario.slug, scenario_version=scenario.version,
                passage_key="integration-rights-pending", source_identifier="R04",
                source_title="Wilson Fourteen Points", source_url="https://example.test/r04",
                source_anchor="Point I", rights_basis="Rights check pending.",
                passage_text="Open covenants await a rights decision.",
                passage_sha256=hashlib.sha256(
                    b"Open covenants await a rights decision."
                ).hexdigest(), document_date=date(1918, 1, 8),
                in_world_available_from=date(1918, 1, 8), postgame_only=False,
                actor_ids=["britain"], visibility_scope="actor_limited", corpus_sha256=corpus_hash,
            )
            mis_scoped = HistoricalPassage(
                id=uuid4(), scenario_version_id=scenario.id,
                scenario_slug=scenario.slug, scenario_version=scenario.version,
                passage_key="integration-mis-scoped", source_identifier="R04",
                source_title="Wilson Fourteen Points", source_url="https://example.test/r04",
                source_anchor="Point I", rights_basis="Test fixture.",
                passage_text="Open covenants with an invalid confidential scope.",
                passage_sha256=hashlib.sha256(
                    b"Open covenants with an invalid confidential scope."
                ).hexdigest(), document_date=date(1918, 1, 8),
                in_world_available_from=date(1918, 1, 8), postgame_only=False,
                actor_ids=["britain"], visibility_scope="confidential", corpus_sha256=corpus_hash,
            )
            future = HistoricalPassage(
                id=uuid4(), scenario_version_id=scenario.id,
                scenario_slug=scenario.slug, scenario_version=scenario.version,
                passage_key="integration-future", source_identifier="R09",
                source_title="German observations", source_url="https://example.test/r09",
                source_anchor="May 29, 1919", rights_basis="Rights check pending.",
                passage_text="Open covenants in later German observations.",
                passage_sha256=hashlib.sha256(
                    b"Open covenants in later German observations."
                ).hexdigest(), document_date=date(1919, 5, 29),
                in_world_available_from=date(1919, 5, 29), postgame_only=False,
                actor_ids=["britain"], visibility_scope="actor_limited", corpus_sha256=corpus_hash,
            )
            other_scenario = HistoricalPassage(
                id=uuid4(), scenario_version_id=legacy.id,
                scenario_slug="versailles-1919", scenario_version="1",
                passage_key="integration-other-version", source_identifier="R04",
                source_title="Wilson Fourteen Points", source_url="https://example.test/r04",
                source_anchor="Point I", rights_basis="Test fixture.",
                passage_text="Open covenants from another version.",
                passage_sha256=hashlib.sha256(
                    b"Open covenants from another version."
                ).hexdigest(), document_date=date(1918, 1, 8),
                in_world_available_from=date(1918, 1, 8), postgame_only=False,
                actor_ids=["britain"], visibility_scope="actor_limited", corpus_sha256=corpus_hash,
            )
            db.add_all([approved, rights_pending, mis_scoped, future, other_scenario])
            await db.flush()
            for passage, status, rights_status in (
                (approved, "approved", "approved"),
                (rights_pending, "approved", "pending"),
                (mis_scoped, "approved", "approved"),
                (future, "approved", "approved"),
                (other_scenario, "approved", "approved"),
            ):
                db.add(
                    HistoricalPassageReview(
                        id=uuid4(), passage_id=passage.id, revision=1,
                        review_status=status, rights_status=rights_status,
                        reviewer="integration-fixture",
                    )
                )
                db.add(
                    HistoricalPassageEmbedding(
                        id=uuid4(), passage_id=passage.id,
                        passage_sha256=passage.passage_sha256, model_id=MODEL_ID,
                        embedding=VECTOR,
                    )
                )
            await db.flush()

            policy = yaml.safe_load(
                (ROOT / "scenarios" / "versailles-1919" / "2.5_evidence.yaml").read_text(
                    encoding="utf-8"
                )
            )
            context = EvidenceContext(
                scenario_version_id=scenario.id,
                scenario_slug=scenario.slug,
                scenario_version=scenario.version,
                actor_id="britain",
                world_date=date(1919, 4, 22),
                mode="gameplay",
                evidence_policy=policy,
            )
            repository = PostgresRagRepository(db, embedding_model_id=MODEL_ID)

            assert await repository.has_eligible(context)
            lexical = await repository.lexical_search(context, "open covenants", 10)
            assert [row.passage.id for row in lexical] == [approved.id]
            dense = await repository.dense_search(context, VECTOR, 10)
            assert [row.passage.id for row in dense] == [approved.id]
            assert dense[0].dense_similarity == pytest.approx(1.0)

            assert len(await repository.revalidate(context, [approved.id])) == 1
            await record_review(
                db,
                approved.id,
                reviewer="integration-fixture",
                decision="revoked",
                rights_status="pending",
                notes="Simulated revocation before exposure.",
            )
            assert await repository.revalidate(context, [approved.id]) == []
        finally:
            await transaction.rollback()
