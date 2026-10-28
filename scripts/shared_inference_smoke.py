#!/usr/bin/env python3
"""Exercise one shared llama.cpp server using two independent synthetic workloads."""
import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import error, request

CASES = {
    "history": {
        "field": "language",
        "value": "da",
        "prompt": "Du er diplomat i Versailles i 1919. Giv én kort replik på dansk om fred.",
    },
    "medical": {
        "field": "speaker",
        "value": "examiner",
        "prompt": (
            "This is a fictional examination exercise. As the examiner, ask the candidate "
            "to introduce themself in one short sentence. No real patient is involved."
        ),
    },
}


class InvalidResponse(ValueError):
    """The completion did not satisfy the smoke-test contract."""


def validate_response(document, case):
    try:
        choice = document["choices"][0]
        if choice["finish_reason"] != "stop":
            raise InvalidResponse("completion did not finish")
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise InvalidResponse("content is not text")
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise InvalidResponse("invalid completion envelope or JSON") from exc

    expected = CASES[case]
    if (
        not isinstance(result, dict)
        or set(result) != {expected["field"], "reply"}
        or result[expected["field"]] != expected["value"]
        or not isinstance(result["reply"], str)
        or not result["reply"].strip()
        or len(result["reply"]) > 600
    ):
        raise InvalidResponse("response does not match the workload schema")
    return result


def run_case(base_url, model, case, timeout, disable_thinking=False, max_tokens=180):
    started = time.monotonic()
    result = {"case": case, "model": model, "ok": False}
    spec = CASES[case]
    schema = {
        "type": "object",
        "properties": {
            spec["field"]: {"type": "string", "enum": [spec["value"]]},
            "reply": {"type": "string", "minLength": 1, "maxLength": 600},
        },
        "required": [spec["field"], "reply"],
        "additionalProperties": False,
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": spec["prompt"]}],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": f"{case}_probe", "strict": True, "schema": schema},
        },
    }
    if disable_thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    completed = queue.Queue(maxsize=1)

    def send():
        try:
            http_request = request.Request(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(http_request, timeout=timeout) as response:
                if response.status != 200:
                    completed.put(f"HTTP {response.status}")
                    return
                document = json.load(response)
            validate_response(document, case)
            completed.put(None)
        except error.HTTPError as exc:
            completed.put(f"HTTP {exc.code}")
        except (TimeoutError, socket.timeout):
            completed.put("timeout")
        except error.URLError as exc:
            completed.put(
                "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout))
                else "connection error"
            )
        except (InvalidResponse, ValueError, UnicodeError):
            completed.put("invalid response")
        except OSError:
            completed.put("connection error")

    # DNS resolution is not covered by urllib's socket timeout. A daemon thread
    # bounds the caller's total wait, including DNS, without logging the response.
    threading.Thread(target=send, daemon=True).start()
    try:
        failure = completed.get(timeout=timeout)
    except queue.Empty:
        failure = "timeout"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if failure is None:
        result["ok"] = True
    else:
        result["error"] = failure
    return result


def positive_number(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not 0 < number <= 600:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 600")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", "http://ddu-llm:8080"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL_ALIAS", "ddu-narrator"))
    parser.add_argument("--timeout", type=positive_number, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=180)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--concurrent", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_tokens <= 4096:
        parser.error("--max-tokens must be between 1 and 4096")
    started = time.monotonic()

    def run(case):
        return run_case(
            args.base_url, args.model, case, args.timeout, args.disable_thinking, args.max_tokens
        )

    if args.concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, CASES))
    else:
        results = [run(case) for case in CASES]
    print(json.dumps({"results": results, "wall_seconds": round(time.monotonic() - started, 3)}))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
