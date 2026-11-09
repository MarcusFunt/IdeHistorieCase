"""Pinned, role-aware scenario context and evidence eligibility contracts."""
from datetime import date
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from app.db.models import ScenarioContentItem, ScenarioVersion
from app.rag.policy import EvidenceContext, EvidencePassage, PassageApproval, is_eligible
from app.rag.retrieval import RAG_NO_EVIDENCE, RagSearchService, SearchCandidate
from app.runtime.context import build_session_context
from app.scenarios.schema_v2 import ScenarioDefinitionV2
from app.scenarios.validation import validate_v2_definition

FIXTURE = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry/1.0.yaml"
RELEASE_ID = uuid4()


def definition(source_ids: list[str] | None = None) -> ScenarioDefinitionV2:
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    raw["source_refs"] = source_ids or []
    if not source_ids:
        raw["content_items"][0]["source_refs"] = []
    raw["actors"].append(
        {
            "id": "factory_worker",
            "name": "En arbejder",
            "description": "Et selvstændigt perspektiv, ikke en repræsentant for alle.",
            "motivations": ["sikkerhed"],
            "knowledge_constraints": ["Kender kun egne erfaringer."],
        }
    )
    raw["roles"].append(
        {
            "id": "observer",
            "actor_id": "factory_worker",
            "title": "Observatør",
            "playable": False,
            "introduction": "En rolle, som ikke er valgt i denne session.",
            "knowledge_constraints": [],
        }
    )
    if source_ids:
        raw["content_items"][0]["source_refs"] = [source_ids[0]]
    result = ScenarioDefinitionV2.model_validate(raw)
    validate_v2_definition(result)
    return result


def scenario_row(definition_: ScenarioDefinitionV2, evidence_policy: dict | None = None):
    return ScenarioVersion(
        id=RELEASE_ID,
        slug=definition_.slug,
        version=definition_.version,
        schema_version=2,
        definition=definition_.model_dump(mode="json"),
        initial_state={},
        content_hash="a" * 64,
        bundle_hash="b" * 64,
        evidence_policy=evidence_policy,
    )


def content_rows(definition_: ScenarioDefinitionV2, bodies: dict[str, str]):
    rows = []
    for item in definition_.content_items:
        body = bodies[item.key]
        rows.append(
            ScenarioContentItem(
                id=uuid4(),
                scenario_version_id=RELEASE_ID,
                item_key=item.key,
                kind=item.kind,
                language=definition_.metadata.language,
                body=body,
                authoritative_scope="author_context",
                availability_date=item.availability_date,
                world_valid_from=item.world_valid_from,
                world_valid_to=item.world_valid_to,
                visibility=item.visibility,
                mode=item.mode,
                review_status=item.review_status,
                source_refs=item.source_refs,
                metadata_json={"source_file": item.file},
                sha256=sha256(body.encode("utf-8")).hexdigest(),
            )
        )
    return rows


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeDb:
    def __init__(self, scenario, rows):
        self.scenario = scenario
        self.rows = rows

    async def get(self, model, identity):
        if model is ScenarioVersion and identity == self.scenario.id:
            return self.scenario
        return None

    async def execute(self, _statement):
        return _FakeResult(self.rows)


class _FakeEmbedder:
    def __init__(self):
        self.calls = 0

    async def embed_query(self, _query: str):
        self.calls += 1
        return [0.1, 0.2]


class _FakeRetrievalRepository:
    def __init__(self, candidates: list[SearchCandidate]):
        self.candidates = candidates

    async def has_eligible(self, context: EvidenceContext) -> bool:
        return any(
            is_eligible(candidate.passage, context, candidate.approval, context.evidence_policy)
            for candidate in self.candidates
        )

    async def lexical_search(self, _context: EvidenceContext, _query: str, _limit: int):
        return self.candidates

    async def dense_search(self, _context: EvidenceContext, _vector: list[float], _limit: int):
        return []

    async def revalidate(self, _context: EvidenceContext, candidate_ids: list):
        return [
            candidate
            for candidate in self.candidates
            if candidate.passage.id in candidate_ids
        ]


def evidence_policy() -> dict:
    entries = [
        ("R01", "1888-04-30", ["factory_owner"]),
        ("R02", "1888-06-01", ["factory_owner"]),
        ("R03", "1888-04-20", ["factory_worker"]),
        ("R04", "1888-04-20", ["factory_owner"]),
        ("R05", "postgame", []),
        ("R06", "1888-04-20", ["factory_owner"]),
    ]
    return {
        "citation_ready": True,
        "source_access": [
            {
                "source_id": source_id,
                "in_world_available_from": available,
                "scope": "postgame_only" if available == "postgame" else "period_statement_paraphrase_only",
                "actor_ids": actor_ids,
                "citation_ready": source_id == "R01",
            }
            for source_id, available, actor_ids in entries
        ],
    }


def passage_candidate(
    source_id: str,
    *,
    actor_ids: tuple[str, ...] = ("factory_owner",),
    document_date: date = date(1888, 4, 20),
    available_from: date | None = date(1888, 4, 20),
    postgame_only: bool = False,
    visibility_scope: str = "actor_limited",
    review_status: str = "approved",
    rights_status: str = "approved",
) -> SearchCandidate:
    text = f"Kontrolleret passage for {source_id}."
    passage = EvidencePassage(
        id=uuid4(),
        scenario_version_id=RELEASE_ID,
        scenario_slug="industrial-inquiry",
        scenario_version="1.0",
        passage_key=f"passage_{source_id.lower()}",
        source_identifier=source_id,
        source_title=f"Kilde {source_id}",
        source_url="https://example.org/source",
        source_anchor="afsnit-1",
        text=text,
        passage_sha256=sha256(text.encode("utf-8")).hexdigest(),
        document_date=document_date,
        in_world_available_from=available_from,
        postgame_only=postgame_only,
        actor_ids=actor_ids,
        visibility_scope=visibility_scope,
    )
    return SearchCandidate(
        passage=passage,
        approval=PassageApproval(review_status, rights_status, revision=1),
        lexical_score=0.5,
    )


def prepared_context(
    *,
    source_ids: list[str] | None = None,
    bodies: dict[str, str] | None = None,
    candidates: list[SearchCandidate] | None = None,
    policy: dict | None = None,
    state: dict | None = None,
):
    definition_ = definition(source_ids)
    bodies = bodies or {
        item.key: "Forfatterbriefing, adskilt fra historisk kildebelæg."
        for item in definition_.content_items
    }
    db = _FakeDb(
        scenario_row(definition_, policy),
        content_rows(definition_, bodies),
    )
    session = SimpleNamespace(
        scenario_version_id=RELEASE_ID,
        player_role_id="investigator",
        state=state or {},
    )
    embedder = _FakeEmbedder()
    rag = RagSearchService(_FakeRetrievalRepository(candidates or []), embedder)
    return db, session, rag, embedder


@pytest.mark.asyncio
async def test_initialization_uses_release_opening_and_only_current_role_notes():
    source_ids = ["R01"]
    definition_ = definition(source_ids)
    raw = definition_.model_dump(mode="python")
    raw["content_items"].extend(
        [
            {
                "key": "future_briefing",
                "kind": "author_note",
                "file": "notes/future.md",
                "mode": "gameplay",
                "visibility": ["investigator"],
                "review_status": "author_reviewed",
                "availability_date": "1888-05-04",
                "world_valid_from": "1888-05-04",
                "source_refs": [],
            },
            {
                "key": "observer_briefing",
                "kind": "author_note",
                "file": "notes/observer.md",
                "mode": "gameplay",
                "visibility": ["observer"],
                "review_status": "author_reviewed",
                "source_refs": [],
            },
            {
                "key": "draft_briefing",
                "kind": "author_note",
                "file": "notes/draft.md",
                "mode": "gameplay",
                "visibility": ["investigator"],
                "review_status": "draft",
                "source_refs": [],
            },
            {
                "key": "postgame_briefing",
                "kind": "author_note",
                "file": "notes/postgame.md",
                "mode": "postgame",
                "visibility": ["investigator"],
                "review_status": "author_reviewed",
                "source_refs": [],
            },
        ]
    )
    definition_ = ScenarioDefinitionV2.model_validate(raw)
    validate_v2_definition(definition_)
    bodies = {item.key: item.key for item in definition_.content_items}
    db = _FakeDb(scenario_row(definition_), content_rows(definition_, bodies))
    session = SimpleNamespace(
        scenario_version_id=RELEASE_ID,
        player_role_id="investigator",
        state={"world_date": "1888-05-10"},
    )
    embedder = _FakeEmbedder()
    rag = RagSearchService(_FakeRetrievalRepository([]), embedder)

    context = await build_session_context(
        db,
        session,
        stage="initialization",
        query="",
        retrieval_service=rag,
    )

    assert context.world_date == date(1888, 5, 1)
    assert context.payload["pinned_scenario"]["version"] == "1.0"
    assert context.payload["selected_role"]["id"] == "investigator"
    assert context.payload["chronology"]["max_days_per_advance"] == 7
    assert [note["item_key"] for note in context.payload["author_notes"]] == ["site_briefing"]
    assert context.payload["author_notes"][0]["trust_category"] == "author_context_not_historical_evidence"
    assert context.evidence_status == RAG_NO_EVIDENCE
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_later_turn_uses_persisted_world_date_and_date_valid_notes():
    definition_ = definition()
    raw = definition_.model_dump(mode="python")
    raw["content_items"].append(
        {
            "key": "future_briefing",
            "kind": "author_note",
            "file": "notes/future.md",
            "mode": "gameplay",
            "visibility": ["investigator"],
            "review_status": "author_reviewed",
            "availability_date": "1888-05-04",
            "world_valid_from": "1888-05-04",
            "source_refs": [],
        }
    )
    definition_ = ScenarioDefinitionV2.model_validate(raw)
    bodies = {item.key: item.key for item in definition_.content_items}
    db = _FakeDb(scenario_row(definition_), content_rows(definition_, bodies))
    session = SimpleNamespace(
        scenario_version_id=RELEASE_ID,
        player_role_id="investigator",
        state={"world_date": "1888-05-05"},
    )
    embedder = _FakeEmbedder()
    context = await build_session_context(
        db,
        session,
        stage="turn",
        query="",
        retrieval_service=RagSearchService(_FakeRetrievalRepository([]), embedder),
    )

    assert context.world_date == date(1888, 5, 5)
    assert {item["item_key"] for item in context.payload["author_notes"]} == {
        "site_briefing",
        "future_briefing",
    }


@pytest.mark.asyncio
async def test_retrieval_excludes_future_wrong_actor_unapproved_and_rights_pending_passages():
    policy = evidence_policy()
    ids = [f"R0{i}" for i in range(1, 7)]
    candidates = [
        passage_candidate("R01", document_date=date(1888, 4, 30), available_from=date(1888, 4, 30)),
        passage_candidate("R02", document_date=date(1888, 6, 1), available_from=date(1888, 6, 1)),
        passage_candidate("R03", actor_ids=("factory_worker",)),
        passage_candidate("R04", rights_status="pending"),
        passage_candidate(
            "R05",
            actor_ids=(),
            available_from=None,
            postgame_only=True,
            visibility_scope="postgame",
        ),
        passage_candidate("R06", review_status="rejected"),
    ]
    db, session, rag, _ = prepared_context(
        source_ids=ids,
        candidates=candidates,
        policy=policy,
        state={"world_date": "1888-05-01"},
    )

    context = await build_session_context(
        db,
        session,
        stage="turn",
        query="arbejdsvilkår",
        retrieval_service=rag,
    )

    passages = context.payload["evidence"]["passages"]
    assert context.evidence_status == "available"
    assert [passage["source_identifier"] for passage in passages] == ["R01"]
    assert passages[0]["citation_ready"] is True
    assert passages[0]["trust_category"] == "historical_evidence"


@pytest.mark.asyncio
async def test_postgame_passages_are_available_only_in_postgame_context():
    postgame = passage_candidate(
        "R05",
        actor_ids=(),
        available_from=None,
        postgame_only=True,
        visibility_scope="postgame",
    )
    policy = evidence_policy()
    db, session, rag, _ = prepared_context(
        source_ids=["R05"],
        candidates=[postgame],
        policy=policy,
        state={"world_date": "1888-12-31"},
    )

    historical_reflection = await build_session_context(
        db,
        session,
        stage="turn",
        query="sammenligning",
        mode="postgame",
        retrieval_service=rag,
    )
    in_world = await build_session_context(
        db,
        session,
        stage="turn",
        query="sammenligning",
        mode="gameplay",
        retrieval_service=rag,
    )

    assert historical_reflection.evidence_status == "available"
    assert [item["source_identifier"] for item in historical_reflection.payload["evidence"]["passages"]] == ["R05"]
    assert in_world.evidence_status == RAG_NO_EVIDENCE
    assert in_world.payload["evidence"]["passages"] == []


@pytest.mark.asyncio
async def test_no_evidence_is_explicit_and_does_not_call_the_embedding_service():
    db, session, rag, embedder = prepared_context()
    context = await build_session_context(
        db,
        session,
        stage="initialization",
        query="kilder om sagen",
        retrieval_service=rag,
    )

    assert context.evidence_status == RAG_NO_EVIDENCE
    assert context.payload["evidence"] == {"status": RAG_NO_EVIDENCE, "passages": []}
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_context_budget_is_deterministic_and_keeps_critical_constraints():
    body = "langt forfatterindhold " * 500
    db, session, rag, _ = prepared_context(
        bodies={"site_briefing": body},
    )
    first = await build_session_context(
        db,
        session,
        stage="initialization",
        query="",
        retrieval_service=rag,
        max_chars=5000,
    )
    second = await build_session_context(
        db,
        session,
        stage="initialization",
        query="",
        retrieval_service=rag,
        max_chars=5000,
    )

    assert first.serialized_chars <= 5000
    assert first.payload == second.payload
    assert first.truncated_note_keys == ("site_briefing",)
    assert "kontrafaktisk" in first.payload["historical_context"]["counterfactual_disclaimer"]
    assert first.payload["constraints"]


@pytest.mark.asyncio
async def test_turn_context_rejects_missing_or_out_of_period_persisted_date():
    db, session, rag, _ = prepared_context()
    session.state = {}
    with pytest.raises(ValueError, match="persisted world_date"):
        await build_session_context(
            db,
            session,
            stage="turn",
            query="handling",
            retrieval_service=rag,
        )

    session.state = {"world_date": "1891-01-01"}
    with pytest.raises(ValueError, match="historical period"):
        await build_session_context(
            db,
            session,
            stage="turn",
            query="handling",
            retrieval_service=rag,
        )

    session.state = {"world_date": "1880-01-01"}
    with pytest.raises(ValueError, match="opening date"):
        await build_session_context(
            db,
            session,
            stage="turn",
            query="handling",
            retrieval_service=rag,
        )


@pytest.mark.asyncio
async def test_context_rejects_nonplayable_role_and_tampered_sql_content():
    db, session, rag, _ = prepared_context()
    session.player_role_id = "observer"
    with pytest.raises(ValueError, match="playable role"):
        await build_session_context(
            db,
            session,
            stage="initialization",
            query="",
            retrieval_service=rag,
        )

    session.player_role_id = "investigator"
    db.rows[0].body = "ændret efter import"
    with pytest.raises(ValueError, match="does not match its manifest"):
        await build_session_context(
            db,
            session,
            stage="initialization",
            query="",
            retrieval_service=rag,
        )
