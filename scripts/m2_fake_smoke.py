#!/usr/bin/env python3
"""Exercise an authenticated durable fake turn without inference."""
import argparse
import json
import sys
import time
from urllib import error, request
from uuid import uuid4


def call(base_url, method, path, body=None, token=None, key=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = request.Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=5) as response:
        return response.status, json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        status, session = call(args.base_url, "POST", "/api/v1/sessions", {
            "scenario": "versailles-1919"
        })
        if status != 201:
            raise ValueError("session creation failed")
        token = session["token"]
        session_path = f"/api/v1/sessions/{session['id']}"
        turn_id = str(uuid4())
        payload = {
            "client_turn_id": turn_id, "expected_turn": 0,
            "text": "Vi søger en holdbar fred.", "input_language": "da",
        }
        status, turn = call(args.base_url, "POST", session_path + "/turns", payload, token, turn_id)
        if status != 202:
            raise ValueError("turn submission failed")
        request_id = turn["request_id"]
        status, retry = call(args.base_url, "POST", session_path + "/turns", payload, token, turn_id)
        if status != 202 or retry["request_id"] != request_id:
            raise ValueError("idempotent retry failed")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            _, turn = call(args.base_url, "GET", f"/api/v1/turn-requests/{request_id}", token=token)
            if turn["status"] == "completed":
                break
            if turn["status"] == "failed":
                raise ValueError("turn processing failed")
            time.sleep(0.5)
        else:
            raise ValueError("turn did not complete within 40 seconds")
        _, resumed = call(args.base_url, "GET", session_path, token=token)
        _, history = call(args.base_url, "GET", session_path + "/turns", token=token)
        if resumed["turn_number"] != 1 or history["turns"] != [turn["result"]]:
            raise ValueError("session resume or replay failed")
        print(json.dumps({"status": "ok", "turn_number": 1, "provider": turn["result"]["provider"]}))
        return 0
    except (KeyError, ValueError, error.URLError, OSError):
        print("M2 fake turn smoke test failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
