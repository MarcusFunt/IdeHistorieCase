import json

import httpx
import pytest
from pydantic import ValidationError

from app.ai.llm import InferenceClient


def completion(content, finish_reason="stop"):
    return {
        "choices": [
            {"finish_reason": finish_reason, "message": {"content": content}}
        ]
    }


@pytest.mark.asyncio
async def test_health_accepts_empty_non_json_http_200_body():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b""))
    ) as http:
        assert await InferenceClient("http://llm:8080", "ddu-narrator", http).is_ready()


@pytest.mark.asyncio
async def test_health_503_returns_false():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="loading"))
    ) as http:
        assert not await InferenceClient("http://llm:8080", "ddu-narrator", http).is_ready()


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("timed out"), httpx.ConnectError("offline")]
)
@pytest.mark.asyncio
async def test_health_network_failures_return_false(failure):
    def handler(request):
        raise failure

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert not await InferenceClient("http://llm:8080", "ddu-narrator", http).is_ready()


@pytest.mark.asyncio
async def test_explicit_model_readiness_checks_the_expected_alias():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "other-model"}, {"id": "ddu-narrator"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = InferenceClient("http://llm:8080", "ddu-narrator", http)
        assert await client.serves_expected_model()


@pytest.mark.asyncio
async def test_explicit_model_readiness_rejects_alias_mismatch():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": "other-model"}]})
        )
    ) as http:
        client = InferenceClient("http://llm:8080", "ddu-narrator", http)
        assert not await client.serves_expected_model()


@pytest.mark.parametrize("enable_thinking", [False, True])
@pytest.mark.asyncio
async def test_structured_probe_applies_the_configured_thinking_mode(enable_thinking):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["chat_template_kwargs"] == {"enable_thinking": enable_thinking}
        assert body["model"] == "ddu-narrator"
        return httpx.Response(
            200,
            json=completion('{"language":"da","reply":"Fred gennem garantier."}'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = InferenceClient(
            "http://llm:8080", "ddu-narrator", http, enable_thinking=enable_thinking
        )
        result = await client.generate_danish_probe()
        assert result.language == "da"
        assert "garantier" in result.reply


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (completion('{"language":"da","reply":"Hej"}', "length"), ValueError, "truncat"),
        (completion('{"language":"da","reply":"Hej"}', "content_filter"), ValueError, "finish successfully"),
        (completion(""), ValueError, "non-empty"),
        (completion(None), ValueError, "non-empty"),
        (completion("not JSON"), ValueError, "JSON"),
        (completion('{"language":"da","reply":"Hej","extra":1}'), ValidationError, None),
        (completion('{"language":"en","reply":"Hello"}'), ValidationError, None),
    ],
)
@pytest.mark.asyncio
async def test_structured_probe_rejects_invalid_or_truncated_output(response, error, message):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        client = InferenceClient("http://llm:8080", "ddu-narrator", http)
        with pytest.raises(error, match=message):
            await client.generate_danish_probe()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), httpx.ConnectError("offline")])
@pytest.mark.asyncio
async def test_structured_probe_propagates_network_failures(failure):
    async def handler(request):
        raise failure

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = InferenceClient("http://llm:8080", "ddu-narrator", http)
        with pytest.raises(httpx.HTTPError):
            await client.generate_danish_probe()


@pytest.mark.asyncio
async def test_structured_probe_propagates_http_errors():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as http:
        client = InferenceClient("http://llm:8080", "ddu-narrator", http)
        with pytest.raises(httpx.HTTPStatusError):
            await client.generate_danish_probe()
