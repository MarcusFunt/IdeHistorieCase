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
            json={"choices": [{"message": {"content": '{"language":"da","reply":"Fred gennem gensidige garantier."}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()
        assert result.language == "da"
        assert "garantier" in result.reply


@pytest.mark.asyncio
async def test_structured_probe_rejects_unexpected_fields():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"language":"da","reply":"Hej","sql":"DROP TABLE"}'}}]},
            )
        )
    ) as http:
        with pytest.raises(ValidationError):
            await LlamaClient("http://llm:8080", "ddu-narrator", http).generate_danish_probe()
