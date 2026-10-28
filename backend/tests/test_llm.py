import json

import httpx
import pytest
from pydantic import ValidationError

from app.ai.llm import LlamaClient


@pytest.mark.asyncio
async def test_health_success_and_loading():
    def handler(request):
        if request.url.path != "/health":
            raise AssertionError("Unexpected URL")
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        llm = LlamaClient("http://llm:8080", "ddu-narrator", http)
        assert await llm.is_ready()


@pytest.mark.asyncio
async def test_health_loading_returns_false():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"status": "loading"}))
    ) as http:
        assert not await LlamaClient("http://llm:8080", "ddu-narrator", http).is_ready()


@pytest.mark.asyncio
async def test_structured_probe_validates_output_schema():
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = __import__("json").loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"language":"da","reply":"Fred gennem gensidige garantier."}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()
        assert result.language == "da"
        assert "garantier" in result.reply


@pytest.mark.asyncio
async def test_probe_returns_json_when_model_supports_thinking_mode():
    def handler(request):
        body = json.loads(request.content)
        if body.get("chat_template_kwargs", {}).get("enable_thinking") is not False:
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
            )
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {
                "content": '{"language":"da","reply":"Fred gennem gensidige garantier."}'
            }}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()
        assert result.language == "da"
        assert result.reply == "Fred gennem gensidige garantier."


@pytest.mark.asyncio
async def test_structured_probe_rejects_truncated_completion_even_with_valid_json():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"finish_reason": "length", "message": {
                    "content": '{"language":"da","reply":"Hej"}'
                }}]},
            )
        )
    ) as http:
        with pytest.raises(ValueError, match="truncat"):
            await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()


@pytest.mark.asyncio
async def test_structured_probe_propagates_http_errors():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()


@pytest.mark.asyncio
async def test_structured_probe_propagates_timeouts():
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        with pytest.raises(httpx.TimeoutException):
            await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()


@pytest.mark.asyncio
async def test_structured_probe_rejects_invalid_json():
    response = {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        with pytest.raises(ValueError):
            await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()


@pytest.mark.asyncio
async def test_structured_probe_rejects_unexpected_fields():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": '{"language":"da","reply":"Hej","sql":"DROP TABLE"}'}}]},
            )
        )
    ) as http:
        with pytest.raises(ValidationError):
            await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()
