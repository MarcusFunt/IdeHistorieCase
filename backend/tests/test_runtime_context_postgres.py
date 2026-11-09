"""PostgreSQL-backed publication, pinning, evidence, and budgeting contract tests."""
import hashlib
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml

from app.db.connection import engine, session_factory
from app.db.models import (
    GameSession,
    HistoricalPassage,
    HistoricalPassageReview,
    RagScenarioPolicy,
)
from app.rag.postgres import PostgresRagRepository
from app.rag.repository import record_review
from app.rag.retrieval import RagSearchConfig, RagSearchService
from app.runtime.context import (
    EVIDENCE_EXCLUDED_BY_BUDGET,
    EVIDENCE_INCLUDED,
    EVIDENCE_NO_RELEVANT_PASSAGES,
    ContextBudget,
    build_session_context,
)
from app.scenarios.loader import load_scenario
from app.scenarios.repository import activate_scenario_version, import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
ROOT = Path(__file__).parents[2]
SOURCE_RELEASE = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry/1.0.yaml"


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


class FakeEmbedder:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.0] * 1024


def make_release(tmp_path: Path) -> Path:
    target = tmp_path / "industrial-inquiry"
    target.mkdir()
    shutil.copytree(SOURCE_RELEASE.parent / "notes", target / "notes")
    raw = yaml.safe_load(SOURCE_RELEASE.read_text(encoding="utf-8"))
    version = f"pg-{uuid4().hex[:12]}"
    raw["version"] = version
    raw["source_refs"] = ["R01", "R02", "R03", "R04"]
    raw["actors"].append(
        {
            "id": "factory_worker",
            "name": "En arbejder",
            "description": "En enkelt arbejders erfaringer.",
            "motivations": ["sikkerhed"],
            "knowledge_constraints": ["Kender egne erfaringer."],
        }
    )
    raw["roles"].append(
        {
            "id": "worker",
            "actor_id": "factory_worker",
            "title": "Arbejder",
            "playable": True,
            "introduction": "Du beskriver egne erfaringer.",
            "knowledge_constraints": [],
        }
    )
    manifest = target / SOURCE_RELEASE.name
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    evidence = {
        "scenario_slug": "industrial-inquiry",
        "scenario_version": version,
        "policy_version": 1,
        "citation_ready": True,
        "source_access": [
            {
                "source_id": "R01",
                "in_world_available_from": "1888-04-30",
                "scope": "period_statement_paraphrase_only",
                "actor_ids": ["factory_owner"],
                "citation_ready": True,
            },
            {
                "source_id": "R02",
                "in_world_available_from": "postgame",
                "scope": "postgame_only",
                "actor_ids": [],
                "citation_ready": True,
            },
            {
                "source_id": "R03",
                "in_world_available_from": "1888-04-20",
                "scope": "confidential_council_record",
                "actor_ids": ["factory_worker"],
                "citation_ready": False,
            },
            {
                "source_id": "R04",
                "in_world_available_from": "1888-04-20",
                "scope": "period_statement_paraphrase_only",
                "actor_ids": ["factory_owner"],
                "citation_ready": False,
            },
        ],
    }
    manifest.with_name(f"{manifest.stem}_evidence.yaml").write_text(
        yaml.safe_dump(evidence, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return manifest


def make_passage(
    scenario,
    *,
    key,
    source_id,
    text,
    doc_date,
    available_from,
    actors,
    scope,
    corpus_hash,
    postgame_only=False,
):
    return HistoricalPassage(
        id=uuid4(),
        scenario_version_id=scenario.id,
        scenario_slug=scenario.slug,
        scenario_version=scenario.version,
        passage_key=key,
        source_identifier=source_id,
        source_title=f"Test source {source_id}",
        source_url=f"https://example.test/{source_id.lower()}",
        source_anchor="section-1",
        rights_basis="Controlled integration fixture",
        passage_text=text,
        passage_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        document_date=doc_date,
        in_world_available_from=available_from,
        postgame_only=postgame_only,
        actor_ids=actors,
        visibility_scope=scope,
        corpus_sha256=corpus_hash,
    )


def add_approved_review(db, passage):
    db.add(
        HistoricalPassageReview(
            id=uuid4(),
            passage_id=passage.id,
            revision=1,
            review_status="approved",
            rights_status="approved",
            reviewer="context-integration-test",
        )
    )


@pytest.mark.asyncio
async def test_postgres_context_is_pinned_role_scoped_temporal_and_budgeted(tmp_path):
    manifest = make_release(tmp_path)
    async with session_factory() as db:
        transaction = await db.begin()
        try:
            imported = await import_scenario(db, load_scenario(manifest))
            scenario = imported.scenario
            corpus_hash = "d" * 64
            db.add(
                RagScenarioPolicy(
                    id=uuid4(),
                    scenario_version_id=scenario.id,
                    scenario_slug=scenario.slug,
                    scenario_version=scenario.version,
                    phase_world_dates={"investigation": "1888-05-01"},
                    corpus_sha256=corpus_hash,
                )
            )
            future_owner = make_passage(
                scenario,
                key="future-owner-record",
                source_id="R01",
                text="Factory wage evidence from a later report.",
                doc_date=date(1888, 6, 1),
                available_from=date(1888, 4, 30),
                actors=["factory_owner"],
                scope="actor_limited",
                corpus_hash=corpus_hash,
            )
            public_postgame = make_passage(
                scenario,
                key="public-postgame-record",
                source_id="R02",
                text="Retrospective factory wage evidence after the inquiry.",
                doc_date=date(1889, 1, 1),
                available_from=None,
                actors=[],
                scope="postgame",
                postgame_only=True,
                corpus_hash=corpus_hash,
            )
            confidential_worker = make_passage(
                scenario,
                key="confidential-worker-record",
                source_id="R03",
                text="Factory wage evidence in confidential worker testimony.",
                doc_date=date(1888, 4, 20),
                available_from=date(1888, 4, 20),
                actors=["factory_worker"],
                scope="confidential",
                corpus_hash=corpus_hash,
            )
            large_passage = make_passage(
                scenario,
                key="large-budget-record",
                source_id="R04",
                text="oversized archive evidence " * 120,
                doc_date=date(1888, 4, 20),
                available_from=date(1888, 4, 20),
                actors=["factory_owner"],
                scope="actor_limited",
                corpus_hash=corpus_hash,
            )
            passages = [future_owner, public_postgame, confidential_worker, large_passage]
            db.add_all(passages)
            for passage in passages:
                add_approved_review(db, passage)

            owner_session = GameSession(
                id=uuid4(),
                scenario_version_id=scenario.id,
                capability_hash="a" * 64,
                player_role_id="investigator",
                capability_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                turn_number=0,
                state={"world_date": "1888-05-01", "phase": "investigation"},
            )
            worker_session = GameSession(
                id=uuid4(),
                scenario_version_id=scenario.id,
                capability_hash="b" * 64,
                player_role_id="worker",
                capability_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                turn_number=0,
                state={"world_date": "1888-05-01", "phase": "investigation"},
            )
            db.add_all([owner_session, worker_session])
            await db.flush()

            retrieval = RagSearchService(
                PostgresRagRepository(db, embedding_model_id="context-test-model"),
                FakeEmbedder(),
                config=RagSearchConfig(min_lexical_score=0.0),
            )
            owner_init = await build_session_context(
                db,
                owner_session,
                stage="initialization",
                query="factory wage evidence",
                retrieval_service=retrieval,
            )
            owner_turn = await build_session_context(
                db,
                owner_session,
                stage="turn",
                query="factory wage evidence",
                retrieval_service=retrieval,
            )
            owner_postgame = await build_session_context(
                db,
                owner_session,
                stage="turn",
                query="factory wage evidence",
                mode="postgame",
                retrieval_service=retrieval,
            )
            worker_postgame = await build_session_context(
                db,
                worker_session,
                stage="turn",
                query="factory wage evidence",
                mode="postgame",
                retrieval_service=retrieval,
            )
            worker_gameplay = await build_session_context(
                db,
                worker_session,
                stage="turn",
                query="factory wage evidence",
                retrieval_service=retrieval,
            )

            assert owner_init.world_date == date(1888, 5, 1)
            assert owner_init.evidence_status == EVIDENCE_NO_RELEVANT_PASSAGES
            assert owner_turn.evidence_status == EVIDENCE_NO_RELEVANT_PASSAGES
            assert {
                item["source_identifier"] for item in owner_postgame.payload["evidence"]["passages"]
            } == {"R01", "R02"}
            assert {
                item["source_identifier"] for item in worker_postgame.payload["evidence"]["passages"]
            } == {"R02", "R03"}
            assert [
                item["source_identifier"] for item in worker_gameplay.payload["evidence"]["passages"]
            ] == ["R03"]

            await record_review(
                db,
                confidential_worker.id,
                reviewer="context-integration-test",
                decision="revoked",
                rights_status="denied",
                notes="Revocation must take effect immediately.",
            )
            worker_after_revocation = await build_session_context(
                db,
                worker_session,
                stage="turn",
                query="factory wage evidence",
                mode="postgame",
                retrieval_service=retrieval,
            )
            assert [
                item["source_identifier"]
                for item in worker_after_revocation.payload["evidence"]["passages"]
            ] == ["R02"]

            base_context = await build_session_context(
                db,
                owner_session,
                stage="turn",
                query="no matching passage phrase",
                retrieval_service=retrieval,
            )
            bounded = await build_session_context(
                db,
                owner_session,
                stage="turn",
                query="oversized archive evidence",
                retrieval_service=retrieval,
                budget=ContextBudget(max_chars=base_context.serialized_chars + 100),
            )
            assert bounded.evidence_status == EVIDENCE_EXCLUDED_BY_BUDGET
            assert bounded.omitted_evidence_count == 1
            assert bounded.omitted_evidence_keys == ("large-budget-record",)
            assert bounded.payload["evidence"]["passages"] == []

            legacy = await import_scenario(
                db,
                load_scenario(ROOT / "scenarios" / "versailles-1919" / "2.5.yaml"),
            )
            await activate_scenario_version(db, legacy.scenario.slug, legacy.scenario.version)
            still_pinned = await build_session_context(
                db,
                owner_session,
                stage="turn",
                query="factory wage evidence",
                mode="postgame",
                retrieval_service=retrieval,
            )
            assert still_pinned.payload["pinned_scenario"]["slug"] == "industrial-inquiry"
            assert still_pinned.payload["pinned_scenario"]["version"] == scenario.version
            assert still_pinned.evidence_status == EVIDENCE_INCLUDED
        finally:
            await transaction.rollback()
