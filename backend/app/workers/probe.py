"""Run inside backend turn-worker: validates a genuine Danish structured completion."""
import asyncio
import json
import sys

import httpx
from pydantic import ValidationError

from app.ai.llm import LlamaClient
from app.core.settings import get_settings


async def main() -> int:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), trust_env=False) as client:
        llm = LlamaClient(settings.llm_base_url, settings.llm_model_alias, client)
        if not await llm.is_ready():
            print("INFERENCE_NOT_READY: /health did not report model ready", file=sys.stderr)
            return 2
        try:
            reply = await llm.generate_danish_probe()
        except (httpx.HTTPError, ValidationError, ValueError, KeyError, IndexError) as error:
            print(f"PROBE_FAILED: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(json.dumps(reply.model_dump(), ensure_ascii=False))
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
