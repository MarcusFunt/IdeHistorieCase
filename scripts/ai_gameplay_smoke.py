"""Run one v2 opening and five real actions against an isolated local app stack."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ACTION_SCRIPTS = {
    "versailles-1919": [
        "Fortæl kort, hvordan en britisk rådgiver kan afveje sikkerhed og genopbygning i en kontrafaktisk drøftelse. Opfind ikke citater eller historiske faktapåstande. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Undersøg, hvem drøftelserne berører uden selv at have en tydelig stemme. Forklar især repræsentationsproblemet, og markér mulige konsekvenser som kontrafaktiske. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Sammenlign hensyn til sikkerhed med hensyn til retfærdighed. Hvilke spørgsmål bør en britisk rådgiver undersøge videre, og hvilke omkostninger kan følge af prioriteringen? Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Forklar, hvordan en britisk rådgiver kan få Kinas indsigelser ført til protokols og taget op i en senere runde. Opfind ikke citater eller historiske faktapåstande. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Uventet sidespor: skriv en kort limerick om en blæksprutte på en cykel. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
    ],
    "industrial-workplace": [
        "Kald search_approved_evidence præcis to gange. Brug query='ordinary wood' i det første kald og query='irrespective of the exit facilities' i det andet kald; kopiér begge query-strenge nøjagtigt som skrevet uden oversættelse, omskrivning eller tilføjelser. Brug de returnerede godkendte uddrag til at forklare på dansk præcist, hvad hvert uddrag viser, og angiv deres evidence-id'er. Hvis et kald ikke finder et godkendt uddrag, sig det i stedet for at gætte. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Kald search_approved_evidence præcis to gange med query='ordinary wood' og query='irrespective of the exit facilities', én nøjagtig forespørgsel pr. kald. Udform en inspektionsoversigt, der kan undersøge forholdene på flere fabrikker: angiv tre konkrete forhold, som bør registreres på hvert sted, og forklar kort, hvordan bredere stikprøver kan afgrænse rapportens rækkevidde. Gentag ikke den forrige handlings svar eller formuleringer. Brug de returnerede evidence-id'er, og skeln mellem kildens fund og forslag til ny dataindsamling. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Som undersøger, udarbejd åbne spørgsmål til fremtidige samtaler med arbejdere og arbejdsgiverrepræsentanter. Hvilke forskellige hensyn bør spørgsmålene afdække? Skriv kun forslag til spørgsmål, ikke historiske faktapåstande; sæt historical_claims=[]. Opfind ikke vidneudsagn. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Som undersøger, skitsér hvilke oplysninger du vil indhente fra arbejdere, arbejdsgivere og inspektører for at kunne vurdere en mulig inspektionsplan. Skriv kun om en fremtidig undersøgelsesmetode, ikke om hvad der faktisk skete; sæt historical_claims=[]. Opfind ikke vidneudsagn. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
        "Uventet sidespor: skriv en kort fabel om en kat, der gemmer sig mellem sagsmapperne. Skriv kun fortællende tekst uden dialog, og foreslå ingen tilstandsændringer.",
    ],
}


def _actions_for(scenario: str) -> list[str]:
    try:
        return ACTION_SCRIPTS[scenario]
    except KeyError as exc:
        raise ValueError(
            f"no reviewed five-action smoke script configured for scenario {scenario!r}"
        ) from exc


def _configuration() -> tuple[str, str, str, str | None, float]:
    if os.environ.get("CASES_ISOLATED_SMOKE") != "1":
        raise ValueError("set CASES_ISOLATED_SMOKE=1 only for a disposable app stack")
    database = os.environ.get("CASES_SMOKE_DB_NAME", "")
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", database)
        or not re.search(r"smoke|test", database)
    ):
        raise ValueError("CASES_SMOKE_DB_NAME must name a disposable smoke/test database")

    base_url = os.environ.get("CASES_SMOKE_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("CASES_SMOKE_BASE_URL must point to a loopback app endpoint")

    scenario = os.environ.get("CASES_SMOKE_SCENARIO", "versailles-1919")
    role = os.environ.get("CASES_SMOKE_ROLE") or None
    try:
        timeout = float(os.environ.get("CASES_SMOKE_TIMEOUT_SECONDS", "1200"))
    except ValueError as exc:
        raise ValueError("CASES_SMOKE_TIMEOUT_SECONDS must be numeric") from exc
    if not 10 <= timeout <= 1800:
        raise ValueError("CASES_SMOKE_TIMEOUT_SECONDS must be between 10 and 1800")
    return base_url, database, scenario, role, timeout


async def run_smoke() -> dict:
    base_url, database, scenario, role, timeout_seconds = _configuration()
    session_capability = uuid4().hex + uuid4().hex[:11]
    request_key = str(uuid4())
    start = time.perf_counter()

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds),
        trust_env=False,
    ) as client:
        identity = await client.get("/api/v1/status")
        if identity.status_code != 200:
            raise RuntimeError("isolated API database identity check failed")
        if identity.json().get("database_name") != database:
            raise RuntimeError("API database does not match the isolated smoke database")

        payload = {"scenario": scenario}
        if role is not None:
            payload["role_id"] = role
        created = await client.post(
            "/api/v1/sessions",
            json=payload,
            headers={
                "Idempotency-Key": request_key,
                "X-Session-Capability": session_capability,
            },
        )
        if created.status_code != 201:
            raise RuntimeError(f"session creation failed with HTTP {created.status_code}")
        body = created.json()
        session_id = body.get("id")
        token = body.get("token")
        if not isinstance(session_id, str) or not isinstance(token, str):
            raise TypeError("isolated API did not return the new session capability")
        headers = {"Authorization": f"Bearer {token}"}
        deadline = time.monotonic() + timeout_seconds
        status = None
        reload_verified = False
        resumed_state = None
        while time.monotonic() < deadline:
            response = await client.get(
                f"/api/v1/sessions/{session_id}/initialization", headers=headers
            )
            if response.status_code != 200:
                raise RuntimeError(f"initialization polling failed with HTTP {response.status_code}")
            status = response.json()
            if status.get("status") in {"ready", "failed", "retryable_failure"}:
                break
            delay = status.get("poll_after_ms", 1000)
            delay = delay if isinstance(delay, int) and 100 <= delay <= 10_000 else 1000
            await asyncio.sleep(delay / 1000)
        if isinstance(status, dict) and status.get("status") == "ready":
            reloaded = await client.get(
                f"/api/v1/sessions/{session_id}/initialization", headers=headers
            )
            resumed = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
            if reloaded.status_code != 200 or resumed.status_code != 200:
                raise RuntimeError("opening reload failed")
            reload_body = reloaded.json()
            resumed_body = resumed.json()
            reload_verified = (
                reload_body.get("status") == "ready"
                and reload_body.get("opening") == status.get("opening")
                and reload_body.get("provenance") == status.get("provenance")
                and resumed_body.get("initialization_status") == "ready"
                and isinstance(resumed_body.get("state"), dict)
            )
            if not reload_verified:
                raise RuntimeError("opening or initialized state changed after session reload")
            resumed_state = resumed_body["state"]

            actions = _actions_for(scenario)
            turn_trace = []
            for expected_turn, action in enumerate(actions):
                client_turn_id = str(uuid4())
                turn_payload = {
                    "client_turn_id": client_turn_id,
                    "expected_turn": expected_turn,
                    "text": action,
                    "input_language": "da",
                }
                turn_headers = {
                    **headers,
                    "Idempotency-Key": client_turn_id,
                }
                submitted = await client.post(
                    f"/api/v1/sessions/{session_id}/turns",
                    json=turn_payload,
                    headers=turn_headers,
                )
                if submitted.status_code != 202:
                    raise RuntimeError(
                        f"turn {expected_turn + 1} submission failed with HTTP {submitted.status_code}"
                    )
                repeated = await client.post(
                    f"/api/v1/sessions/{session_id}/turns",
                    json=turn_payload,
                    headers=turn_headers,
                )
                if (
                    repeated.status_code != 202
                    or repeated.json().get("request_id") != submitted.json().get("request_id")
                ):
                    raise RuntimeError("duplicate turn submission created a second request")

                turn_status = None
                request_location = submitted.headers.get("location")
                while time.monotonic() < deadline:
                    polled = await client.get(request_location, headers=headers)
                    if polled.status_code != 200:
                        raise RuntimeError(
                            f"turn polling failed with HTTP {polled.status_code}"
                        )
                    turn_status = polled.json()
                    if turn_status.get("status") in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.5)
                if not isinstance(turn_status, dict) or turn_status.get("status") != "completed":
                    raise RuntimeError(
                        f"turn {expected_turn + 1} did not complete: "
                        f"{turn_status.get('failure_code') if isinstance(turn_status, dict) else 'timeout'}"
                    )
                turn_trace.append(
                    {
                        "turn_number": expected_turn + 1,
                        "player_action": action,
                        "request_id": turn_status.get("request_id"),
                        "result": turn_status.get("result"),
                    }
                )

            resumed = await client.get(
                f"/api/v1/sessions/{session_id}", headers=headers
            )
            history = await client.get(
                f"/api/v1/sessions/{session_id}/turns", headers=headers
            )
            if (
                resumed.status_code != 200
                or history.status_code != 200
                or resumed.json().get("turn_number") != len(actions)
                or len(history.json().get("turns", [])) != len(actions)
                or history.json().get("turns")
                != [item["result"] for item in turn_trace]
                or resumed.json().get("state")
                != turn_trace[-1]["result"].get("state")
            ):
                raise RuntimeError("turn state or history changed after reload")
            resumed_state = resumed.json()["state"]

    if not isinstance(status, dict) or status.get("status") != "ready":
        failure = status.get("failure") if isinstance(status, dict) else None
        return {
            "status": status.get("status") if isinstance(status, dict) else "timeout",
            "failure": failure,
            "scenario": scenario,
            "database": database,
            "session_id": session_id,
            "client_elapsed_ms": int((time.perf_counter() - start) * 1000),
        }

    provenance = status.get("provenance") or {}
    return {
        "status": "ready",
        "scenario": scenario,
        "database": database,
        "session_id": session_id,
        "opening": status.get("opening"),
        "model": {
            "provider": provenance.get("provider"),
            "requested_alias": provenance.get("requested_model"),
            "served_model": provenance.get("served_model"),
            "revision": provenance.get("model_revision"),
        },
        "latency_ms": provenance.get("latency_ms"),
        "client_elapsed_ms": int((time.perf_counter() - start) * 1000),
        "prompt_tokens": provenance.get("prompt_tokens"),
        "completion_tokens": provenance.get("completion_tokens"),
        "repair_used": provenance.get("repair_used"),
        "reload_verified": reload_verified,
        "state_world_date": (resumed_state or {}).get("world_date"),
        "state_turn_number": (resumed_state or {}).get("turn_number"),
        "scenario_content_hash": (provenance.get("scenario") or {}).get("content_hash"),
        "eligible_evidence": provenance.get("eligible_evidence", []),
        "turn_trace": turn_trace,
        "turns_reload_verified": len(turn_trace) == len(_actions_for(scenario)),
        "final_state_matches_last_turn_result": (
            isinstance(resumed_state, dict)
            and resumed_state == turn_trace[-1]["result"].get("state")
        ),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = asyncio.run(run_smoke())
    except (TypeError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
