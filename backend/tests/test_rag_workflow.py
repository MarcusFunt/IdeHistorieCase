"""PostgreSQL workflow checks for staged ingestion and append-only review decisions."""
import hashlib
import os
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select

from app.db.connection import engine, session_factory
from app.db.models import HistoricalPassage, HistoricalPassageEmbedding, HistoricalPassageReview
from app.rag.corpus import load_corpus
from app.rag.repository import RagCorpusConflict, ingest_corpus, record_review
from app.scenarios.loader import load_scenario
from app.scenarios.repository import import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "scenarios" / "versailles-1919" / "2.5.yaml"
PHASES = {
    "opening_context": "1919-03-20",
    "security_rhineland": "1919-03-20",
    "reparations_framework": "1919-03-20",
    "territories_minorities": "1919-03-20",
    "league_security": "1919-03-20",
    "shandong_mandates": "1919-04-22",
    "draft_delivery": "1919-05-07",
    "german_objections": "1919-05-29",
    "final_settlement": "1919-06-28",
    "reflection": "1919-06-28",
}


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


def write_corpus(path: Path, passage_text: str = "A checked historical passage.") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "scenario_slug": "versailles-1919",
        "scenario_version": "2.5",
        "phase_world_dates": PHASES,
        "passages": [{
            "key": "workflow-passage",
            "source_identifier": "R04",
            "source_title": "Wilson address",
            "source_url": "https://history.state.gov/historicaldocuments/sample",
            "source_anchor": "Document 4, page 1",
            "passage_text": passage_text,
            "passage_sha256": hashlib.sha256(passage_text.encode("utf-8")).hexdigest(),
            "document_date": "1918-01-08",
            "in_world_available_from": "1918-01-08",
            "postgame_only": False,
            "actor_ids": ["britain"],
            "visibility_scope": "actor_limited",
            "rights_basis": "FRUS public-domain statement; rights approval remains pending.",
        }],
    }
    corpus_path = path / "corpus.yaml"
    corpus_path.write_text(yaml.safe_dump(record), encoding="utf-8")
    return corpus_path


@pytest.mark.asyncio
async def test_ingestion_stages_idempotently_and_reviews_append_new_revisions(tmp_path):
    async with session_factory() as db:
        transaction = await db.begin()
        try:
            await import_scenario(db, load_scenario(MANIFEST))
            loaded = load_corpus(write_corpus(tmp_path))
            vector = [1.0] + [0.0] * 1023

            with pytest.raises(ValueError, match="1024 values"):
                await ingest_corpus(db, loaded, [[1.0]], model_id="bad-embed")

            staged = await ingest_corpus(db, loaded, [vector], model_id="fake-embed-v1")
            assert staged.status == "created"
            assert staged.passages == 1
            passage = (
                await db.execute(select(HistoricalPassage).where(
                    HistoricalPassage.passage_key == "workflow-passage"
                ))
            ).scalar_one()
            assert passage.scenario_version == "2.5"
            assert passage.passage_sha256 == hashlib.sha256(
                b"A checked historical passage."
            ).hexdigest()
            assert (await db.execute(select(HistoricalPassageReview))).scalars().all() == []
            assert (await db.execute(select(HistoricalPassageEmbedding))).scalars().one().model_id == "fake-embed-v1"

            repeated = await ingest_corpus(db, loaded, [vector], model_id="fake-embed-v1")
            assert repeated.status == "unchanged"
            new_model = await ingest_corpus(db, loaded, [vector], model_id="fake-embed-v2")
            assert new_model.status == "embeddings_added"
            assert new_model.embeddings == 1

            first = await record_review(
                db, passage.id, reviewer="local-reviewer", decision="approved",
                rights_status="approved", notes="Exact excerpt and reuse status checked.",
            )
            second = await record_review(
                db, passage.id, reviewer="local-reviewer", decision="revoked",
                rights_status="pending", notes="Review withdrawn for reassessment.",
            )
            assert (first.revision, second.revision) == (1, 2)
            reviews = (await db.execute(select(HistoricalPassageReview).order_by(
                HistoricalPassageReview.revision
            ))).scalars().all()
            assert [row.review_status for row in reviews] == ["approved", "revoked"]

            changed = load_corpus(write_corpus(tmp_path / "changed", "Different passage."))
            with pytest.raises(RagCorpusConflict, match="increment the scenario version"):
                await ingest_corpus(db, changed, [vector], model_id="fake-embed-v1")
        finally:
            await transaction.rollback()
