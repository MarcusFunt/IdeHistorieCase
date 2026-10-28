"""External inference health and one deliberately narrow structured-output probe."""
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProbeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["da"]
    reply: str = Field(min_length=1, max_length=600)

    @field_validator("reply", mode="before")
    @classmethod
    def reject_blank_reply(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("reply must not be blank")
        return value


PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["da"]},
        "reply": {"type": "string", "minLength": 1, "maxLength": 600, "pattern": r"\S"},
    },
    "required": ["language", "reply"],
    "additionalProperties": False,
}


class InferenceClient:
    def __init__(
        self,
        base_url: str,
        model_alias: str,
        client: httpx.AsyncClient,
        *,
        enable_thinking: bool | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_alias = model_alias
        self.client = client
        self.enable_thinking = enable_thinking

    async def is_ready(self) -> bool:
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def serves_expected_model(self) -> bool:
        """Explicit setup check; keep model-list polling out of the health loop."""
        try:
            response = await self.client.get(f"{self.base_url}/v1/models")
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        models = document.get("data") if isinstance(document, dict) else None
        return isinstance(models, list) and any(
            isinstance(model, dict) and model.get("id") == self.model_alias
            for model in models
        )

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
        # This option is template-specific; omit it unless the operator explicitly configures it.
        if self.enable_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        response = await self.client.post(f"{self.base_url}/v1/chat/completions", json=body)
        response.raise_for_status()
        document = response.json()
        choices = document.get("choices") if isinstance(document, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Completion response did not contain a choice")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Completion was truncated by the token limit")
        if choice.get("finish_reason") != "stop":
            raise ValueError("Completion did not finish successfully")
        message = choice.get("message")
        raw = message.get("content") if isinstance(message, dict) else None
        # Some models return a JSON string; accept only an object matching our strict schema.
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Expected non-empty JSON text in the model response")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("Model response was not valid JSON") from error
        return ProbeReply.model_validate(parsed)
