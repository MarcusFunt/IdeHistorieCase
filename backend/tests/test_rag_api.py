"""Session evidence route keeps the existing capability and scenario pin contract."""
import hashlib
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.connection import engine, session_factory
from app.db.models import GameSession, RagScenarioPolicy, ScenarioVersion
from app.main import app
from app.rag.corpus import load_corpus
from app.rag.policy import EvidencePassage, PassageApproval
from app.rag.retrieval import RAG_NO_EVIDENCE, RagSearchResult, SearchCandidate
from app.rag.routes import get_rag_db, get_rag_search_service
from app.scenarios.loader import load_scenario
from app.scenarios.repository import activate_scenario_version, import_scenario

pytestmark = pytest.mark.skipif(os.getenv("DDU_TEST_DB") != "1", reason="Postgres not configured")
ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "scenarios" / "versailles-1919" / "2.5.yaml"
CORPUS = ROOT / "scenarios" / "versailles-1919" / "rag" / "corpus.yaml"


class FakeSearchService:
    def __init__(self):
        self.calls = []
        self.result = None

    async def search(self, query, context, *, top_k=5):
        self.calls.append((query, context, top_k))
        if callable(self.result):
            return self.result(query, context)
        return self.result or RagSearchResult(RAG_NO_EVIDENCE, [])


@pytest_asyncio.fixture(autouse=True)
async def dispose_engine_between_test_event_loops():
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def rag_client():
    session_ids: list[UUID] = []
    fake = FakeSearchService()
    app.dependency_overrides[get_rag_search_service] = lambda: fake
    async with session_factory() as db, db.begin():
        await import_scenario(db, load_scenario(MANIFEST))
        await activate_scenario_version(db, "versailles-1919", "2.5")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.created_sessions = session_ids
            yield client, fake
            if session_ids:
                async with session_factory() as cleanup_db, cleanup_db.begin():
                    await cleanup_db.execute(
                        delete(GameSession).where(GameSession.id.in_(session_ids))
                    )
            async with session_factory() as activation_db, activation_db.begin():
                await activate_scenario_version(activation_db, "versailles-1919", "1")
    finally:
        app.dependency_overrides.pop(get_rag_db, None)
        app.dependency_overrides.pop(get_rag_search_service, None)


@asynccontextmanager
async def policy_session():
    loaded_corpus = load_corpus(CORPUS)
    async with session_factory() as db:
        transaction = await db.begin()
        try:
            scenario = (
                await db.execute(
                    select(ScenarioVersion).where(
                        ScenarioVersion.slug == "versailles-1919",
                        ScenarioVersion.version == "2.5",
                    )
                )
            ).scalar_one()
            policy = (
                await db.execute(
                    select(RagScenarioPolicy).where(
                        RagScenarioPolicy.scenario_version_id == scenario.id
                    )
                )
            ).scalar_one_or_none()
            if policy is None:
                db.add(
                    RagScenarioPolicy(
                        id=UUID("eb8bd4f9-d1c7-48a0-820a-69c0f24d6c43"),
                        scenario_version_id=scenario.id,
                        scenario_slug=scenario.slug,
                        scenario_version=scenario.version,
                        phase_world_dates={
                            phase: value.isoformat()
                            for phase, value in loaded_corpus.corpus.phase_world_dates.items()
                        },
                        corpus_sha256=loaded_corpus.corpus_sha256,
                    )
                )
                await db.flush()
            else:
                assert policy.corpus_sha256 == loaded_corpus.corpus_sha256
            app.dependency_overrides[get_rag_db] = lambda: db
            yield scenario
        finally:
            await transaction.rollback()
            app.dependency_overrides.pop(get_rag_db, None)


@pytest.mark.asyncio
async def test_session_search_auth_pins_scenario_actor_phase_and_abstention(rag_client):
    client, fake = rag_client
    created = await client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    assert created.status_code == 201, created.text
    session = created.json()
    client.created_sessions.append(UUID(session["id"]))

    # Activating a newer version cannot move a running session off its pin.
    async with session_factory() as db, db.begin():
        await activate_scenario_version(db, "versailles-1919", "1")

    url = f"/api/v1/sessions/{session['id']}/evidence/search"
    unauthorized = await client.post(url, json={"query": "open covenants"})
    assert unauthorized.status_code == 401
    assert fake.calls == []

    async with policy_session():
        response = await client.post(
            url,
            json={"query": "open covenants", "top_k": 3},
            headers={"Authorization": f"Bearer {session['token']}"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": RAG_NO_EVIDENCE, "passages": []}
    assert len(fake.calls) == 1
    query, context, top_k = fake.calls[0]
    assert query == "open covenants" and top_k == 3
    assert context.scenario_slug == "versailles-1919"
    assert context.scenario_version == "2.5"
    assert context.actor_id == "britain"
    assert context.world_date.isoformat() == "1919-03-20"


@pytest.mark.asyncio
async def test_session_search_rejects_invalid_body_without_changing_m2_endpoints(rag_client):
    client, fake = rag_client
    created = await client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    session = created.json()
    client.created_sessions.append(UUID(session["id"]))
    headers = {"Authorization": f"Bearer {session['token']}"}
    url = f"/api/v1/sessions/{session['id']}/evidence/search"

    blank = await client.post(url, json={"query": "  "}, headers=headers)
    extra = await client.post(url, json={"query": "valid", "mode": "evaluator"}, headers=headers)
    assert blank.status_code == 422
    assert extra.status_code == 422
    assert fake.calls == []

    resumed = await client.get(
        f"/api/v1/sessions/{session['id']}", headers=headers
    )
    assert resumed.status_code == 200
    assert resumed.json()["scenario_version"] == "2.5"


@pytest.mark.asyncio
async def test_session_search_serializes_only_eligible_revalidated_evidence(rag_client):
    client, fake = rag_client
    created = await client.post("/api/v1/sessions", json={"scenario": "versailles-1919"})
    assert created.status_code == 201
    session = created.json()
    client.created_sessions.append(UUID(session["id"]))

    def candidate(context, *, future=False):
        text = "Open covenants of peace, openly arrived at."
        doc_date = date(1919, 5, 29) if future else date(1918, 1, 8)
        source = "R09" if future else "R04"
        return SearchCandidate(
            passage=EvidencePassage(
                id=uuid4(),
                scenario_version_id=context.scenario_version_id,
                scenario_slug=context.scenario_slug,
                scenario_version=context.scenario_version,
                passage_key="future-passage" if future else "open-covenants",
                source_identifier=source,
                source_title="Future source" if future else "Fourteen Points",
                source_url="https://example.test/source",
                source_anchor="p. 1",
                text=text,
                passage_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                document_date=doc_date,
                in_world_available_from=doc_date,
                postgame_only=False,
                actor_ids=("britain",),
                visibility_scope="actor_limited",
            ),
            approval=PassageApproval("approved", "approved", 1),
            lexical_score=0.2,
        )

    fake.result = lambda _query, context: RagSearchResult(
        "ok", [candidate(context), candidate(context, future=True)]
    )
    headers = {"Authorization": f"Bearer {session['token']}"}
    url = f"/api/v1/sessions/{session['id']}/evidence/search"
    async with policy_session():
        response = await client.post(url, json={"query": "open covenants"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert len(body["passages"]) == 1
    assert body["passages"][0]["source_identifier"] == "R04"
    assert body["passages"][0]["scenario_version"] == "2.5"
    assert body["passages"][0]["review"] == {
        "status": "approved",
        "rights_status": "approved",
        "revision": 1,
    }
