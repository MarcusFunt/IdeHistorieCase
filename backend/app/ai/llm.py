"""llama.cpp health and one deliberately narrow structured-output probe."""
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ProbeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["da"]
    reply: str = Field(min_length=1, max_length=600)


PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["da"]},
        "reply": {"type": "string", "minLength": 1, "maxLength": 600},
    },
    "required": ["language", "reply"],
    "additionalProperties": False,
}


class LlamaClient:
    def __init__(self, base_url: str, model_alias: str, client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self.model_alias = model_alias
        self.client = client

    async def is_ready(self) -> bool:
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200 and response.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    async def generate_danish_probe(self) -> ProbeReply:
        """Real inference: strict JSON shape is validated again by Pydantic."""
        body = {
            "model": self.model_alias,
            "stream": False,
            "temperature": 0,
            "max_tokens": 180,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Du spiller en diplomat ved fredsforhandlingerne i Versailles i 1919. "
                        "Svar på dansk med en enkelt kort replik. "
                        "Returner kun JSON, der opfylder det specificerede skema."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Hvordan kan Frankrig få sikkerhed uden at ødelægge "
                        "muligheden for økonomisk genopbygning?"
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ddu_m1_probe",
                    "strict": True,
                    "schema": PROBE_SCHEMA,
                },
            },
        }
        response = await self.client.post(f"{self.base_url}/v1/chat/completions", json=body)
        response.raise_for_status()
        document = response.json()
        raw = document["choices"][0]["message"]["content"]
        # Some models return a JSON string; accept only an object matching our strict schema.
        if not isinstance(raw, str):
            raise ValueError("Expected JSON text in the model response")
        parsed = json.loads(raw)
        return ProbeReply.model_validate(parsed)
