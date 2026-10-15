import logging

import httpx

from aria.llm.providers.openai_compatible import _log_http_error, thinking_payload, OpenAICompatibleProvider


def test_thinking_payload_modes():
    assert thinking_payload("off") == {"thinking": {"type": "disabled"}}
    assert thinking_payload("HIGH") == {"reasoning_effort": "high"}
    assert thinking_payload("max") == {"reasoning_effort": "max"}
    assert thinking_payload("") == {}
    assert thinking_payload("bogus") == {}


def test_provider_keeps_extra_payload():
    p = OpenAICompatibleProvider("x", "standard_reasoning", "http://h/v1", "m", api_key="k",
                                 extra_payload=thinking_payload("off"))
    assert p.extra_payload == {"thinking": {"type": "disabled"}}


def test_429_body_logged_whole_with_quota_ids(caplog):
    body = ('{"error":{"code":429,"details":[{"violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},'
            '{"retryDelay":"65604s"}],"pad":"' + "x" * 600 + '"}}')
    resp = httpx.Response(429, text=body)
    with caplog.at_level(logging.WARNING):
        _log_http_error("gemini", "m", resp)
    msg = caplog.text
    assert "retryDelay" in msg and "65604s" in msg
    assert "quota_ids=generaterequestsperdayperprojectpermodel-freetier" in msg


def test_budget_status_zero_limits_disabled():
    from aria.llm.budget import compute_budget_status
    st = compute_budget_status(0, 0)
    assert st["daily_pct"] == 0 and st["weekly_pct"] == 0
