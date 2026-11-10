"""OpenAI-compatible structured-output client for the external inference service."""
import json
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
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

MAX_STRUCTURED_RESPONSE_CHARS = 32_000
MAX_REPAIR_INPUT_CHARS = 12_000


class StructuredOutputError(ValueError):
    """The provider returned truncated, malformed, or non-object structured output."""

    def __init__(self, code: str, *, raw_output: str | None = None):
        messages = {
            "empty_output": "Expected non-empty JSON text in the model response",
            "truncated_output": "Completion was truncated by the token limit",
            "incomplete_output": "Completion did not finish successfully",
            "invalid_json": "Model response was not valid JSON",
            "output_not_object": "Expected a JSON object in the model response",
            "output_too_large": "Structured response exceeded the configured size limit",
        }
        super().__init__(messages.get(code, code))
        self.code = code
        self.raw_output_sha256 = (
            sha256(raw_output.encode("utf-8")).hexdigest() if raw_output is not None else None
        )
        self.raw_output = raw_output[:MAX_REPAIR_INPUT_CHARS] if raw_output is not None else None


@dataclass(frozen=True)
class StructuredCompletion:
    output: dict
    requested_model: str
    served_model: str | None
    model_revision: str | None
    provider_request_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: int
    raw_output_sha256: str
    finish_reason: str


def _optional_token_count(usage: object, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        response_schema: dict,
        max_tokens: int,
        temperature: float = 0,
        enable_thinking: bool | None = None,
    ) -> StructuredCompletion:
        """Request one JSON-schema-constrained completion and retain safe metadata."""
        if not system_prompt.strip() or not user_prompt.strip():
            raise ValueError("structured prompts must be nonblank")
        if not schema_name.strip() or not 1 <= max_tokens <= 8192:
            raise ValueError("schema_name and a bounded max_tokens value are required")
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if enable_thinking is not None and type(enable_thinking) is not bool:
            raise ValueError("enable_thinking must be a boolean or None")
        body = {
            "model": self.model_alias,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": response_schema,
                },
            },
        }
        # This option is template-specific; omit it unless the operator explicitly configures it.
        resolved_thinking = (
            self.enable_thinking if enable_thinking is None else enable_thinking
        )
        if resolved_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": resolved_thinking}
        started = perf_counter()
        response = await self.client.post(f"{self.base_url}/v1/chat/completions", json=body)
        response.raise_for_status()
        document = response.json()
        choices = document.get("choices") if isinstance(document, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Completion response did not contain a choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        message = choice.get("message")
        raw = message.get("content") if isinstance(message, dict) else None
        raw_text = raw if isinstance(raw, str) else None
        if finish_reason == "length":
            raise StructuredOutputError("truncated_output", raw_output=raw_text)
        if finish_reason != "stop":
            raise StructuredOutputError("incomplete_output", raw_output=raw_text)
        if not isinstance(raw, str) or not raw.strip():
            raise StructuredOutputError("empty_output")
        if len(raw) > MAX_STRUCTURED_RESPONSE_CHARS:
            raise StructuredOutputError("output_too_large", raw_output=raw)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StructuredOutputError("invalid_json", raw_output=raw) from error
        if not isinstance(parsed, dict):
            raise StructuredOutputError("output_not_object", raw_output=raw)
        usage = document.get("usage")
        revision = document.get("model_revision") or document.get("system_fingerprint")
        served_model = document.get("model")
        request_id = document.get("id") or response.headers.get("x-request-id")
        return StructuredCompletion(
            output=parsed,
            requested_model=self.model_alias,
            served_model=served_model if isinstance(served_model, str) else None,
            model_revision=revision if isinstance(revision, str) and revision else None,
            provider_request_id=request_id if isinstance(request_id, str) else None,
            prompt_tokens=_optional_token_count(usage, "prompt_tokens"),
            completion_tokens=_optional_token_count(usage, "completion_tokens"),
            duration_ms=max(0, int((perf_counter() - started) * 1000)),
            raw_output_sha256=sha256(raw.encode("utf-8")).hexdigest(),
            finish_reason=finish_reason,
        )

    async def generate_danish_probe(self) -> ProbeReply:
        """Retain the M1 setup probe on the generalized structured adapter."""
        result = await self.generate_structured(
            system_prompt=(
                "Du spiller en diplomat ved fredsforhandlingerne i Versailles i 1919. "
                "Svar på dansk med en enkelt kort replik. "
                "Returner kun JSON, der opfylder det specificerede skema."
            ),
            user_prompt=(
                "Hvordan kan Frankrig få sikkerhed uden at ødelægge "
                "muligheden for økonomisk genopbygning?"
            ),
            schema_name="ddu_m1_probe",
            response_schema=PROBE_SCHEMA,
            max_tokens=180,
            temperature=0,
        )
        return ProbeReply.model_validate(result.output)
