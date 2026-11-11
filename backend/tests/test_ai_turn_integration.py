"""V2 model turns validate, commit, and reload through the durable API."""
import asyncio
from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from app.ai.llm import StructuredCompletion, StructuredOutputError
from app.core.settings import Settings
from app.db.connection import session_factory
from app.db.models import (
    CommittedTurn,
    GameEvent,
    GameSession,
    HistoricalPassage,
    HistoricalPassageReview,
    ScenarioVersion,
    TurnRequest,
)
from app.game import turn_worker
from app.game.turn_worker import claim_next, complete_claim, fail_claim, process_next
from app.rag.policy import EvidencePassage, PassageApproval
from app.rag.repository import record_review
from app.rag.retrieval import RAG_NO_EVIDENCE, RagSearchResult, SearchCandidate
from app.runtime.orchestrator import TurnGeneration, TurnGenerationFailure, TurnOrchestrator
from app.runtime.proposals import (
    HistoricalClaimRecordedEvent,
    TurnProposal,
)


class ScriptedGenerator:
    model_alias = "test-narrator"

    def __init__(self, outputs, *, delay=0):
        self.outputs = list(outputs)
        self.delay = delay
        self.calls = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if kwargs.get("schema_name") == "ddu_turn_agent_response" and "tool_calls" not in output:
            output = {"schema_version": 1, "tool_calls": [], "proposal": output}
        return StructuredCompletion(
            output=output,
            requested_model=self.model_alias,
            served_model=self.model_alias,
            model_revision="test-revision",
            provider_request_id=f"request-{len(self.calls)}",
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=20,
            raw_output_sha256="a" * 64,
            finish_reason="stop",
        )


class NoEvidenceRetriever:
    async def search(self, query, context, *, top_k):
        return RagSearchResult(status=RAG_NO_EVIDENCE, passages=[])


class ToolSearchRetriever:
    def __init__(self, player_action, candidate):
        self.player_action = player_action
        self.candidate = candidate
        self.calls = []

    async def search(self, query, context, *, top_k):
        self.calls.append(query)
        if query == self.player_action:
            return RagSearchResult(status=RAG_NO_EVIDENCE, passages=[])
        return RagSearchResult(status="ok", passages=[self.candidate])


def proposal(role, objective):
    return {
        "schema_version": 1,
        "language": "da",
        "narration": "Du gennemgår materialet og beder om lønregnskaberne.",
        "dialogue": [
            {"actor_id": role.actor_id, "text": "Jeg finder de relevante mapper."}
        ],
        "outcome_summary": "Undersøgelsen fortsætter med nye spørgsmål.",
        "state_operations": [
            {"type": "record_decision", "summary": "Undersøg lønregnskaberne."},
            {
                "type": "open_thread",
                "thread_id": "wage_records",
                "summary": "Find lønregnskaberne.",
            },
            {
                "type": "record_simulation_event",
                "summary": "En simuleret undersøgelse går i gang.",
                "actor_ids": [role.actor_id],
            },
        ],
        "historical_claims": [],
        "author_context_references": [],
        "player_claims": [],
        "learning_observations": [
            {"objective_id": objective.id, "observation": "Spilleren efterspørger belæg."}
        ],
        "scene_status": "continue",
        "next_player_prompt": "Hvad vil du undersøge bagefter?",
    }


def quick_settings():
    return Settings(
        db_host="localhost",
        db_name="turn_test",
        db_user="turn_test",
        db_password="turn_test",
        turn_lease_seconds=2,
        turn_lease_renew_interval_seconds=0.2,
        turn_context_timeout_seconds=2,
        turn_inference_timeout_seconds=2,
        turn_total_timeout_seconds=20,
        llm_enable_thinking=True,
    )


@pytest.mark.asyncio
async def test_v2_turn_projects_committed_events_and_survives_reload(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    assert response.status_code == 202, response.text
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])]
    )
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )

    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.status_code == 200
    result = status.json()["result"]
    assert status.json()["status"] == "completed"
    assert result["turn_number"] == 1
    assert result["reply"] == "Du gennemgår materialet og beder om lønregnskaberne."
    assert result["provider"]["model_revision"] == "test-revision"
    assert generator.calls[0]["enable_thinking"] is True
    assert generator.calls[0]["max_tokens"] == 8192
    assert generator.calls[0]["schema_name"] == "ddu_turn_agent_response"

    resumed = await turn_harness.client.get(
        f"/api/v1/sessions/{session['id']}", headers=headers
    )
    assert resumed.json()["turn_number"] == 1
    assert resumed.json()["state"]["open_threads"][0]["id"] == "wage_records"
    assert resumed.json()["state"]["decisions"][0]["player_action"] == "Undersøg regnskaberne."
    history = await turn_harness.client.get(
        f"/api/v1/sessions/{session['id']}/turns", headers=headers
    )
    assert history.json()["turns"] == [result]

    async with session_factory() as db:
        committed = (
            await db.execute(
                select(CommittedTurn).where(CommittedTurn.request_id == UUID(status.json()["request_id"]))
            )
        ).scalar_one()
        logged = (
            await db.execute(
                select(GameEvent)
                .where(GameEvent.committed_turn_id == committed.id)
                .order_by(GameEvent.event_index)
            )
        ).scalars().all()
        stored_session = await db.get(GameSession, UUID(session["id"]))
    assert logged[0].event_type == "player_argument"
    assert [event.event_type for event in logged[1:]] == [
        "learning_observation_recorded",
        "decision_recorded",
        "thread_opened",
        "simulation_event_recorded",
    ]
    assert stored_session.state == result["state"]


@pytest.mark.parametrize("truncated_before_tool_plan", [False, True])
@pytest.mark.asyncio
async def test_turn_model_can_run_multiple_approved_evidence_searches(
    turn_harness, truncated_before_tool_plan
):
    from app.db.models import HistoricalPassage, HistoricalPassageReview

    session = await turn_harness.create_ready_session()
    player_action = "Undersøg den officielle brandrapport."
    submitted = await turn_harness.submit_turn(session, player_action)
    async with session_factory() as db:
        passage = (
            await db.execute(
                select(HistoricalPassage).where(
                    HistoricalPassage.scenario_slug == turn_harness.slug
                )
            )
        ).scalar_one()
        review = (
            await db.execute(
                select(HistoricalPassageReview).where(
                    HistoricalPassageReview.passage_id == passage.id
                )
            )
        ).scalar_one()
    candidate = SearchCandidate(
        passage=EvidencePassage(
            id=passage.id,
            scenario_version_id=passage.scenario_version_id,
            scenario_slug=passage.scenario_slug,
            scenario_version=passage.scenario_version,
            passage_key=passage.passage_key,
            source_identifier=passage.source_identifier,
            source_title=passage.source_title,
            source_url=passage.source_url,
            source_anchor=passage.source_anchor,
            text=passage.passage_text,
            passage_sha256=passage.passage_sha256,
            document_date=passage.document_date,
            in_world_available_from=passage.in_world_available_from,
            postgame_only=passage.postgame_only,
            actor_ids=tuple(passage.actor_ids),
            visibility_scope=passage.visibility_scope,
        ),
        approval=PassageApproval(
            review_status=review.review_status,
            rights_status=review.rights_status,
            revision=review.revision,
        ),
        lexical_score=0.9,
    )
    retrieval = ToolSearchRetriever(player_action, candidate)
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    objective = turn_harness.definition.learning_objectives[0]
    final_proposal = {
        **proposal(role, objective),
        "historical_claims": [
            {
                "truth_type": "historical_evidence",
                "claim": "Den godkendte passage omtaler fabriksinspektion.",
                "evidence_ids": [str(passage.id)],
            }
        ],
    }
    outputs = []
    if truncated_before_tool_plan:
        outputs.append(
            StructuredOutputError("truncated_output", raw_output='{"schema_version":1,')
        )
    outputs.append(
        {
            "schema_version": 1,
            "tool_calls": [
                {"name": "search_approved_evidence", "query": "brandinspektion og udgange"},
                {"name": "search_approved_evidence", "query": "loftbygninger"},
            ],
            "proposal": None,
        }
    )
    if truncated_before_tool_plan:
        outputs.append(
            {
                "schema_version": 1,
                "tool_calls": [
                    {"name": "search_approved_evidence", "query": "ekstra forespørgsel et"},
                    {"name": "search_approved_evidence", "query": "ekstra forespørgsel to"},
                ],
                "proposal": None,
            }
        )
    outputs.append({"schema_version": 1, "tool_calls": [], "proposal": final_proposal})
    generator = ScriptedGenerator(outputs)
    settings = quick_settings()
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=retrieval,
        settings=settings,
    )

    assert await process_next(orchestrator=orchestrator, settings=settings)
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(submitted.headers["location"], headers=headers)
    assert status.json()["status"] == "completed", status.text
    result = status.json()["result"]
    assert len(retrieval.calls) == 3  # the initial query plus two parallel tool searches
    assert result["provider"]["model_call_count"] == 2 + int(truncated_before_tool_plan)
    assert len(generator.calls) == 2 + 2 * int(truncated_before_tool_plan)
    assert len(result["provider"]["tool_calls"]) == 2
    assert result["provider"]["eligible_evidence"][0]["passage_id"] == str(passage.id)
    tool_attempt = next(
        item
        for item in status.json()["attempt_metadata"]
        if item["outcome"] == "evidence_tool_calls_completed"
    )
    assert tool_attempt["tool_calls_count"] == 2
    assert len(tool_attempt["tool_calls"]) == 2
    assert all(item["result_passage_ids"] == [str(passage.id)] for item in tool_attempt["tool_calls"])
    assert len({item["query_sha256"] for item in tool_attempt["tool_calls"]}) == 2
    assert generator.calls[0]["user_prompt"].find("search_approved_evidence") >= 0
    tool_plan_call = int(truncated_before_tool_plan)
    final_call = tool_plan_call + 1 + int(truncated_before_tool_plan)
    assert generator.calls[tool_plan_call]["enable_thinking"] is (
        False if truncated_before_tool_plan else True
    )
    assert generator.calls[tool_plan_call]["user_prompt"].find("search_approved_evidence") >= 0
    assert generator.calls[final_call]["enable_thinking"] is False
    assert str(passage.id) in generator.calls[final_call]["user_prompt"]
    if truncated_before_tool_plan:
        assert generator.calls[2]["enable_thinking"] is False
        assert '"tool_calls_remaining":0' in generator.calls[final_call]["user_prompt"]
        assert "evidence_tool_budget_exceeded" in generator.calls[final_call]["user_prompt"]
        assert any(
            "evidence_tool_budget_exceeded" in item.get("validation_codes", [])
            for item in status.json()["attempt_metadata"]
        )
        assert any(
            item.get("final_correction") is True
            for item in status.json()["attempt_metadata"]
        )


@pytest.mark.asyncio
async def test_search_recovery_allows_a_second_bounded_final_correction(turn_harness):
    session = await turn_harness.create_ready_session()
    player_action = "Brug query='rapport' i din vurdering."
    submitted = await turn_harness.submit_turn(session, player_action)
    async with session_factory() as db:
        passage = (
            await db.execute(
                select(HistoricalPassage).where(
                    HistoricalPassage.scenario_slug == turn_harness.slug
                )
            )
        ).scalar_one()
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    objective = turn_harness.definition.learning_objectives[0]
    missing_citation = proposal(role, objective)
    cited_output = {
        **proposal(role, objective),
        "historical_claims": [
            {
                "truth_type": "historical_evidence",
                "claim": "Den godkendte rapport omtaler fabriksforhold.",
                "evidence_ids": [str(passage.id)],
            }
        ],
    }
    invalid_envelope = {
        "schema_version": 1,
        "tool_calls": [],
        "proposal": missing_citation,
        "unexpected": "invalid envelope",
    }
    generator = ScriptedGenerator(
        [
            StructuredOutputError("truncated_output", raw_output='{"schema_version":1,'),
            invalid_envelope,
            missing_citation,
            invalid_envelope,
            invalid_envelope,
            cited_output,
        ]
    )
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=ToolSearchRetriever(
            player_action,
            SearchCandidate(
                passage=EvidencePassage(
                    id=passage.id,
                    scenario_version_id=passage.scenario_version_id,
                    scenario_slug=passage.scenario_slug,
                    scenario_version=passage.scenario_version,
                    passage_key=passage.passage_key,
                    source_identifier=passage.source_identifier,
                    source_title=passage.source_title,
                    source_url=passage.source_url,
                    source_anchor=passage.source_anchor,
                    text=passage.passage_text,
                    passage_sha256=passage.passage_sha256,
                    document_date=passage.document_date,
                    in_world_available_from=passage.in_world_available_from,
                    postgame_only=passage.postgame_only,
                    actor_ids=tuple(passage.actor_ids),
                    visibility_scope=passage.visibility_scope,
                ),
                approval=PassageApproval(review_status="approved", rights_status="approved", revision=1),
                lexical_score=0.9,
            ),
        ),
        settings=quick_settings(),
    )

    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(submitted.headers["location"], headers=headers)
    assert status.json()["status"] == "completed", status.text
    result = status.json()["result"]
    assert result["state"]["historical_claims"][0]["evidence_ids"] == [str(passage.id)]
    assert len(generator.calls) == 6
    assert '"tool_calls_remaining":0' in generator.calls[2]["user_prompt"]
    assert '"tool_calls_remaining":0' in generator.calls[4]["user_prompt"]
    assert len(result["provider"]["tool_calls"]) == 1
    assert result["provider"]["tool_calls"][0]["query_sha256"] == sha256(
        b"rapport"
    ).hexdigest()
    assert status.json()["attempt_metadata"][2]["outcome"] == "evidence_tool_calls_completed"
    assert "requested_evidence_search_missing" in status.json()["attempt_metadata"][2][
        "validation_codes"
    ]
    assert "turn_agent_response_invalid" in generator.calls[-1]["user_prompt"]
    assert str(passage.id) in generator.calls[-1]["user_prompt"]
    assert generator.calls[-1]["enable_thinking"] is False
    assert status.json()["attempt_metadata"][2]["final_correction"] is True
    assert status.json()["attempt_metadata"][3].get("final_correction") is not True
    assert status.json()["attempt_metadata"][4]["final_correction"] is True
    assert status.json()["attempt_metadata"][-1]["final_correction"] is True
    assert [item["outcome"] for item in status.json()["attempt_metadata"]] == [
        "structured_output_rejected",
        "validation_rejected",
        "evidence_tool_calls_completed",
        "validation_rejected",
        "validation_rejected",
        "accepted",
    ]
    assert any(
        "turn_agent_response_invalid" in item.get("validation_codes", [])
        for item in status.json()["attempt_metadata"]
    )


@pytest.mark.asyncio
async def test_next_turn_prompt_includes_the_previous_visible_exchange(turn_harness):
    session = await turn_harness.create_ready_session()
    headers = {"Authorization": f"Bearer {session['token']}"}
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    first = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    assert first.status_code == 202, first.text
    first_generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])]
    )
    assert await process_next(
        orchestrator=TurnOrchestrator(
            generator=first_generator,
            retrieval_service=NoEvidenceRetriever(),
            settings=quick_settings(),
        ),
        settings=quick_settings(),
    )
    first_status = await turn_harness.client.get(first.headers["location"], headers=headers)
    assert first_status.json()["status"] == "completed", first_status.text
    first_result = first_status.json()["result"]

    second = await turn_harness.submit_turn(session, "Ja.", expected_turn=1)
    assert second.status_code == 202, second.text
    second_output = {
        **proposal(role, turn_harness.definition.learning_objectives[0]),
        "narration": first_result["reply"],
        "dialogue": [],
        "state_operations": [],
        "learning_observations": [],
    }
    corrected_output = {
        **second_output,
        "narration": "Jeg noterer dit nye svar og undersøger næste spor.",
    }
    second_generator = ScriptedGenerator([second_output, corrected_output])
    assert await process_next(
        orchestrator=TurnOrchestrator(
            generator=second_generator,
            retrieval_service=NoEvidenceRetriever(),
            settings=quick_settings(),
        ),
        settings=quick_settings(),
    )
    second_prompt = second_generator.calls[0]["user_prompt"]
    assert first_result["reply"] in second_prompt
    assert first_result["dialogue"][0]["text"] in second_prompt
    assert first_result["next_player_prompt"] in second_prompt
    assert "Ja." in second_prompt
    assert len(second_generator.calls) == 2
    assert second_generator.calls[1]["enable_thinking"] is False
    assert "repeated_previous_response" in second_generator.calls[1]["user_prompt"]


@pytest.mark.asyncio
async def test_large_committed_projection_is_bounded_for_the_next_prompt(turn_harness):
    session = await turn_harness.create_ready_session()
    async with session_factory() as db, db.begin():
        game = await db.get(GameSession, UUID(session["id"]))
        state = dict(game.state)
        state["turn_number"] = 24
        state["decisions"] = [
            {
                "summary": f"Tidligere beslutning {index}",
                "player_action": "a" * 2000,
                "truth_type": "player_decision",
            }
            for index in range(24)
        ]
        game.turn_number = 24
        game.state = state
    submitted = await turn_harness.submit_turn(
        session, "Undersøg næste skridt.", expected_turn=24
    )
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    output = {
        **proposal(role, turn_harness.definition.learning_objectives[0]),
        "state_operations": [],
        "learning_observations": [],
    }
    generator = ScriptedGenerator([output])
    assert await process_next(
        orchestrator=TurnOrchestrator(
            generator=generator,
            retrieval_service=NoEvidenceRetriever(),
            settings=quick_settings(),
        ),
        settings=quick_settings(),
    )
    status = await turn_harness.client.get(
        submitted.headers["location"],
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert status.json()["status"] == "completed"
    assert len(generator.calls[0]["user_prompt"]) < 48_000


@pytest.mark.asyncio
async def test_delayed_provider_outlives_initial_lease_but_only_commits_once(turn_harness):
    settings = quick_settings().model_copy(
        update={
            "turn_lease_seconds": 1,
            "turn_lease_renew_interval_seconds": 0.1,
            "turn_total_timeout_seconds": 20,
        }
    )
    session = await turn_harness.create_ready_session()
    turn = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])], delay=1.4
    )
    orchestrator = TurnOrchestrator(
        generator=generator, retrieval_service=NoEvidenceRetriever(), settings=settings
    )

    assert await process_next(orchestrator=orchestrator, settings=settings)
    request = await turn_harness.client.get(
        turn.headers["location"],
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert request.json()["status"] == "completed"
    async with session_factory() as db:
        assert len(
            (
                await db.execute(
                    select(CommittedTurn).where(
                        CommittedTurn.session_id == UUID(session["id"])
                    )
                )
            ).scalars().all()
        ) == 1


@pytest.mark.asyncio
async def test_parent_cancellation_stops_generation_and_leaves_claim_reclaimable(
    turn_harness, monkeypatch
):
    settings = quick_settings()
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    generation_started = asyncio.Event()
    release_generation = asyncio.Event()
    commit_called = asyncio.Event()

    class BlockingOrchestrator:
        async def generate(self, claim, *, authorize_repair, record_attempt):
            generation_started.set()
            await release_generation.wait()
            return object()

    async def record_commit(claim, generation):
        commit_called.set()
        return True

    monkeypatch.setattr(turn_worker, "complete_claim", record_commit)
    task = asyncio.create_task(
        process_next(orchestrator=BlockingOrchestrator(), settings=settings)
    )
    await asyncio.wait_for(generation_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release_generation.set()
    await asyncio.sleep(0.05)
    assert not commit_called.is_set()

    async with session_factory() as db, db.begin():
        await db.execute(
            update(TurnRequest)
            .where(TurnRequest.id == UUID(response.json()["request_id"]))
            .values(leased_until=func.clock_timestamp() - timedelta(seconds=1))
        )
    reclaimed = await claim_next(settings)
    assert reclaimed is not None
    assert reclaimed.request_id == UUID(response.json()["request_id"])
    await fail_claim(reclaimed, "TEST_CANCELLATION", retryable=False)


@pytest.mark.asyncio
async def test_revoked_citation_is_revalidated_before_turn_commit(turn_harness):
    session = await turn_harness.create_ready_session()
    await turn_harness.submit_turn(session, "Undersøg dokumentet.")
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    text = "Arbejdsplanen blev drøftet den første maj."
    async with session_factory() as db:
        scenario = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == turn_harness.slug,
                    ScenarioVersion.version == turn_harness.definition.version,
                )
            )
        ).scalar_one()
    passage = HistoricalPassage(
        id=uuid4(),
        scenario_version_id=scenario.id,
        scenario_slug=turn_harness.slug,
        scenario_version=turn_harness.definition.version,
        passage_key="turn-commit-review-race",
        source_identifier="R01",
        source_title="Test record",
        source_url="https://example.test/record",
        source_anchor="folio 1",
        rights_basis="Controlled integration fixture",
        passage_text=text,
        passage_sha256=sha256(text.encode("utf-8")).hexdigest(),
        document_date=turn_harness.definition.chronology.opening_date,
        in_world_available_from=turn_harness.definition.chronology.opening_date,
        postgame_only=False,
        actor_ids=[role.actor_id],
        visibility_scope="actor_limited",
        corpus_sha256="a" * 64,
    )
    async with session_factory() as db, db.begin():
        db.add(passage)
        db.add(
            HistoricalPassageReview(
                id=uuid4(),
                passage_id=passage.id,
                revision=1,
                review_status="approved",
                rights_status="approved",
                reviewer="integration-fixture",
            )
        )

    evidence_event = HistoricalClaimRecordedEvent(
        event_type="historical_claim_recorded",
        truth_type="historical_evidence",
        claim="Dokumentet beskriver arbejdsplanen.",
        evidence_ids=[str(passage.id)],
        world_date=turn_harness.definition.chronology.opening_date.isoformat(),
        actor_id=role.actor_id,
    )
    turn_proposal = TurnProposal.model_validate(
        {
            **proposal(role, turn_harness.definition.learning_objectives[0]),
            "historical_claims": [
                {
                    "truth_type": "historical_evidence",
                    "claim": "Dokumentet beskriver arbejdsplanen.",
                    "evidence_ids": [str(passage.id)],
                }
            ],
        }
    )
    generation = TurnGeneration(
        proposal=turn_proposal,
        events=(evidence_event,),
        provenance={
            "scenario": {
                "release_id": str(scenario.id),
                "slug": scenario.slug,
                "version": scenario.version,
            },
            "eligible_evidence": [
                {
                    "passage_id": str(passage.id),
                    "passage_sha256": passage.passage_sha256,
                    "review_revision": 1,
                }
            ]
        },
        scenario_version=turn_harness.definition.version,
    )

    claim = await claim_next(quick_settings())
    assert claim is not None
    async with session_factory() as db, db.begin():
        await record_review(
            db,
            passage.id,
            reviewer="integration-fixture",
            decision="revoked",
            rights_status="pending",
        )
    with pytest.raises(TurnGenerationFailure, match="EVIDENCE_UNAVAILABLE") as failure:
        await complete_claim(claim, generation)
    assert failure.value.stage == "commit"
    async with session_factory() as db:
        stored = await db.get(GameSession, UUID(session["id"]))
        committed = (
            await db.execute(
                select(CommittedTurn).where(
                    CommittedTurn.session_id == UUID(session["id"])
                )
            )
        ).scalars().all()
    assert stored.turn_number == 0
    assert stored.state == session["state"]
    assert committed == []


@pytest.mark.asyncio
async def test_invalid_output_uses_one_durable_repair_and_two_bounded_corrections(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg regnskaberne.")
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    invalid = {
        **proposal(role, turn_harness.definition.learning_objectives[0]),
        "extra": True,
        "private_model_text": "must never be stored",
    }
    generator = ScriptedGenerator([invalid, invalid, invalid, invalid])
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )

    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "failed", status.text
    assert status.json()["failure_code"] == "MODEL_OUTPUT_INVALID"
    attempts = status.json()["attempt_metadata"]
    assert len(attempts) == 4
    assert all(item["outcome"] == "validation_rejected" for item in attempts)
    assert all(item["output_sha256"] == "a" * 64 for item in attempts)
    assert attempts[-1]["final_correction"] is True
    assert "must never be stored" not in str(attempts)
    assert len(generator.calls) == 4
    assert generator.calls[0]["enable_thinking"] is True
    assert generator.calls[1]["enable_thinking"] is False
    assert generator.calls[2]["enable_thinking"] is False
    assert generator.calls[3]["enable_thinking"] is False
    async with session_factory() as db:
        request = await db.get(TurnRequest, UUID(status.json()["request_id"]))
        stored = await db.get(GameSession, UUID(session["id"]))
        assert request.repair_used is True
        assert request.failure_code == "MODEL_OUTPUT_INVALID"
        assert stored.turn_number == 0
        assert stored.state == session["state"]


@pytest.mark.asyncio
async def test_truncated_repair_gets_one_bounded_final_correction(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg situationen.")
    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    objective = turn_harness.definition.learning_objectives[0]
    generator = ScriptedGenerator(
        [
            StructuredOutputError("schema_invalid", raw_output="{}"),
            StructuredOutputError("truncated_output", raw_output='{"schema_version":1,'),
            proposal(role, objective),
        ]
    )
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )

    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "completed", status.text
    assert len(generator.calls) == 3
    assert generator.calls[1]["enable_thinking"] is False
    assert generator.calls[2]["enable_thinking"] is False
    assert "truncated_output" in generator.calls[2]["user_prompt"]
    assert status.json()["attempt_metadata"][-1]["final_correction"] is True
    assert [item["enable_thinking"] for item in status.json()["attempt_metadata"]] == [
        True,
        False,
        False,
    ]


@pytest.mark.asyncio
async def test_missing_real_orchestrator_fails_retryably_without_a_canned_reply(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg situationen.")
    assert response.status_code == 202

    assert await process_next(settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    status = await turn_harness.client.get(response.headers["location"], headers=headers)
    assert status.json()["status"] == "queued"
    assert status.json()["failure_code"] == "INFERENCE_UNAVAILABLE"
    assert status.json()["result"] is None
    async with session_factory() as db:
        stored = await db.get(GameSession, UUID(session["id"]))
        committed = (
            await db.execute(
                select(CommittedTurn).where(
                    CommittedTurn.session_id == UUID(session["id"])
                )
            )
        ).scalars().all()
    assert stored.turn_number == 0
    assert committed == []


@pytest.mark.asyncio
async def test_unreachable_real_provider_returns_retryable_unavailable(turn_harness):
    session = await turn_harness.create_ready_session()
    response = await turn_harness.submit_turn(session, "Undersøg situationen.")
    assert response.status_code == 202

    class UnreachableGenerator:
        async def generate_structured(self, **kwargs):
            raise httpx.ConnectError("unreachable")

    orchestrator = TurnOrchestrator(
        generator=UnreachableGenerator(),
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    status = await turn_harness.client.get(
        response.headers["location"],
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    assert status.json()["status"] == "queued"
    assert status.json()["failure_code"] == "INFERENCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_transient_failure_allows_one_same_request_retry_cycle(turn_harness):
    session = await turn_harness.create_ready_session()
    submitted = await turn_harness.submit_turn(session, "Undersøg situationen.")
    request_id = UUID(submitted.json()["request_id"])
    headers = {"Authorization": f"Bearer {session['token']}"}

    class UnavailableGenerator:
        async def generate_structured(self, **kwargs):
            raise httpx.ConnectError("unreachable")

    orchestrator = TurnOrchestrator(
        generator=UnavailableGenerator(),
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    for index in range(3):
        processed = await process_next(orchestrator=orchestrator, settings=quick_settings())
        assert processed, f"no request claimed on attempt {index + 1}"
        async with session_factory() as db, db.begin():
            await db.execute(
                update(TurnRequest)
                .where(TurnRequest.id == request_id)
                .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
            )
    status = await turn_harness.client.get(submitted.headers["location"], headers=headers)
    assert status.json()["status"] == "retryable_failure"
    assert status.json()["failure"] == {
        "code": "INFERENCE_UNAVAILABLE",
        "stage": "inference",
        "retryable": True,
        "message": "Turn inference is temporarily unavailable.",
    }

    retry_url = f"/api/v1/turn-requests/{request_id}/retry"
    retry = await turn_harness.client.post(retry_url, headers=headers)
    assert retry.status_code == 202, retry.text
    assert retry.json()["request_id"] == str(request_id)
    assert retry.json()["status"] == "queued"
    repeated_retry = await turn_harness.client.post(retry_url, headers=headers)
    assert repeated_retry.status_code == 202
    assert repeated_retry.json()["request_id"] == str(request_id)

    for _ in range(3):
        assert await process_next(orchestrator=orchestrator, settings=quick_settings())
        async with session_factory() as db, db.begin():
            await db.execute(
                update(TurnRequest)
                .where(TurnRequest.id == request_id)
                .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
            )
    exhausted = await turn_harness.client.get(submitted.headers["location"], headers=headers)
    assert exhausted.json()["status"] == "failed"
    assert exhausted.json()["failure"]["retryable"] is False
    denied = await turn_harness.client.post(retry_url, headers=headers)
    assert denied.status_code == 409


@pytest.mark.asyncio
async def test_duplicate_submission_and_provider_retry_create_one_turn(turn_harness):
    session = await turn_harness.create_ready_session()
    client_turn_id = UUID(int=17)
    first = await turn_harness.submit_turn(
        session,
        "Sammenlign lønnen med arbejdstiden.",
        client_turn_id=client_turn_id,
    )
    repeated = await turn_harness.submit_turn(
        session,
        "Sammenlign lønnen med arbejdstiden.",
        client_turn_id=client_turn_id,
    )
    assert first.status_code == repeated.status_code == 202
    assert first.json()["request_id"] == repeated.json()["request_id"]

    class BrokenGenerator:
        async def generate_structured(self, **kwargs):
            raise RuntimeError("provider is temporarily unavailable")

    orchestrator = TurnOrchestrator(
        generator=BrokenGenerator(),
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    headers = {"Authorization": f"Bearer {session['token']}"}
    failed_attempt = await turn_harness.client.get(first.headers["location"], headers=headers)
    assert failed_attempt.json()["status"] == "queued"
    assert failed_attempt.json()["failure_code"] == "PROVIDER_FAILURE"

    async with session_factory() as db, db.begin():
        await db.execute(
            update(TurnRequest)
            .where(TurnRequest.id == UUID(first.json()["request_id"]))
            .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
        )

    role = next(role for role in turn_harness.definition.roles if role.id == session["role_id"])
    generator = ScriptedGenerator(
        [proposal(role, turn_harness.definition.learning_objectives[0])]
    )
    orchestrator = TurnOrchestrator(
        generator=generator,
        retrieval_service=NoEvidenceRetriever(),
        settings=quick_settings(),
    )
    assert await process_next(orchestrator=orchestrator, settings=quick_settings())
    completed = await turn_harness.client.get(first.headers["location"], headers=headers)
    assert completed.json()["status"] == "completed"
    async with session_factory() as db:
        committed = (
            await db.execute(
                select(CommittedTurn).where(
                    CommittedTurn.session_id == UUID(session["id"])
                )
            )
        ).scalars().all()
        assert len(committed) == 1
