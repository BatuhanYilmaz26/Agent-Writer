import json
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentwriter.providers import (
    FixtureGateway,
    FixtureResearchGateway,
    OpenRouterGateway,
    ProviderError,
)
from agentwriter.providers.treg import TregResearchGateway


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    count: int = Field(ge=1, le=8)


def completion(**changes):
    payload = {
        "id": "gen-test",
        "model": "vendor/actual-free-model",
        "provider": "TestProvider",
        "choices": [
            {"finish_reason": "stop", "message": {"content": '{"text":"hello","count":2}'}}
        ],
    }
    payload.update(changes)
    return payload


@pytest.mark.asyncio
async def test_openrouter_uses_strict_schema_fixed_origin_and_no_hidden_fallback():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json=completion(usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": "0"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = OpenRouterGateway(client, "test-secret")
        result = await gateway.generate(Answer, [{"role": "user", "content": "Write"}], "run:plan")
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-secret"
    body = json.loads(request.content)
    assert body["provider"] == {
        "require_parameters": True,
        "data_collection": "deny",
        "allow_fallbacks": False,
    }
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["stream"] is False and "models" not in body
    assert result.data == Answer(text="hello", count=2)
    assert result.resolved_model == "vendor/actual-free-model"
    assert result.cost_usd == Decimal("0") and result.usage["prompt_tokens"] == 10


@pytest.mark.asyncio
async def test_absent_usage_and_price_remain_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as client:
        result = await OpenRouterGateway(client, "test-key").generate(
            Answer, [{"role": "user", "content": "Write"}], "call"
        )
    assert result.usage is None and result.cost_usd is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "provider_authentication_failed", False),
        (402, "provider_budget_exhausted", False),
        (400, "provider_request_rejected", False),
        (429, "provider_rate_limited", True),
        (503, "provider_unavailable", True),
        (302, "provider_unavailable", False),
    ],
)
async def test_error_classification_is_safe_and_does_not_retry(status, code, retryable):
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Retry-After": "31", "Location": "https://attacker.example"},
            text="private upstream error test-key",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, "test-key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )
    assert error.value.code == code and error.value.retryable is retryable
    assert error.value.retry_after == 31
    assert "test-key" not in str(error.value)
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (completion(choices=[]), "provider_schema_invalid"),
        (
            completion(choices=[{"finish_reason": "length", "message": {"content": "{}"}}]),
            "provider_incomplete_output",
        ),
        (
            completion(
                choices=[{"finish_reason": "stop", "message": {"content": "{}", "refusal": "no"}}]
            ),
            "provider_refusal",
        ),
        (
            completion(choices=[{"finish_reason": "stop", "message": {"content": ""}}]),
            "provider_schema_invalid",
        ),
        (
            completion(
                choices=[
                    {"finish_reason": "stop", "message": {"content": '{"text":"x","count":99}'}}
                ]
            ),
            "provider_schema_invalid",
        ),
        (completion(error={"message": "secret"}), "provider_invalid_response"),
    ],
)
async def test_invalid_or_truncated_output_is_never_accepted(payload, code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ProviderError, match=code):
            await OpenRouterGateway(client, "test-key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )


@pytest.mark.asyncio
async def test_transport_timeout_has_uncertain_remote_outcome():
    def respond(_):
        raise httpx.ReadTimeout("private failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, "key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )
    assert error.value.code == "provider_timeout" and error.value.outcome_unknown


@pytest.mark.asyncio
async def test_oversize_response_is_bounded():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1_048_577))
    ) as client:
        with pytest.raises(ProviderError, match="provider_response_too_large"):
            await OpenRouterGateway(client, "key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )


def test_free_only_rejects_paid_model_and_untrusted_origin():
    client = httpx.AsyncClient()
    with pytest.raises(ValueError, match="Free-only"):
        OpenRouterGateway(client, "key", model="vendor/paid")
    with pytest.raises(ValueError, match="official"):
        OpenRouterGateway(client, "key", base_url="https://example.com/api/v1")
    with pytest.raises(ValueError, match="allowlists"):
        OpenRouterGateway(client, "key", model="vendor/paid", policy="budgeted")


@pytest.mark.asyncio
async def test_budgeted_transport_requires_trusted_worker_reservation():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as client:
        gateway = OpenRouterGateway(
            client,
            "key",
            model="vendor/paid",
            policy="budgeted",
            allowed_models=("vendor/paid",),
            allowed_providers=("vendor",),
        )
        with pytest.raises(ProviderError, match="budget_reservation_required"):
            await gateway.generate(Answer, [{"role": "user", "content": "Write"}], "call")


@pytest.mark.asyncio
async def test_fixture_payloads_are_validated_and_explicitly_labeled():
    gateway = FixtureGateway({"Answer": {"text": "Synthetic fixture", "count": 2}})
    result = await gateway.generate(Answer, [], "fixture:plan")
    assert result.fixture and result.provider == "fixture" and result.cost_usd == 0
    assert gateway.calls == ["fixture:plan"]
    with pytest.raises(ProviderError, match="fixture_response_not_registered"):
        await FixtureGateway().generate(Answer, [], "fixture:missing")


@pytest.mark.asyncio
async def test_fixture_research_is_deterministic_and_empty_corpus_stays_empty():
    assert await FixtureResearchGateway().search("topic") == []
    gateway = FixtureResearchGateway(
        {"https://example.com/article": {"title": "Testing", "text": "Synthetic local evidence."}}
    )
    result = await gateway.extract("https://example.com/article")
    assert result == await gateway.extract("https://example.com/article")
    assert result["fixture"] is True and "Synthetic fixture" in result["source"]["title"]
    assert len(result["source"]["content_hash"]) == 64
    with pytest.raises(ProviderError, match="research_provider_not_qualified"):
        await TregResearchGateway().search("topic")
