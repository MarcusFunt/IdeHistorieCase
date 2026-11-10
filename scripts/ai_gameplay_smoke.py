"""Run one real v2 opening against an explicitly isolated local app stack."""
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
        timeout = float(os.environ.get("CASES_SMOKE_TIMEOUT_SECONDS", "240"))
    except ValueError as exc:
        raise ValueError("CASES_SMOKE_TIMEOUT_SECONDS must be numeric") from exc
    if not 10 <= timeout <= 900:
        raise ValueError("CASES_SMOKE_TIMEOUT_SECONDS must be between 10 and 900")
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
    }


def main() -> int:
    try:
        result = asyncio.run(run_smoke())
    except (TypeError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
