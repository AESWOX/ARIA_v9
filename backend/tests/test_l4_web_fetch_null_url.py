"""L4 regression: every ``web_fetch`` without url recorded in the live logs (18 hits).

The fixture is task ``5bb37d4c`` from ``%APPDATA%\\ARIA\\logs`` (29-30.10.2026):
the Oracle produced a ``web_fetch`` step without a URL while the *task* objective
did contain ``https://example.com``. The plan validator must reject such a step
before execution, and the executor must fail fast without touching DNS/network.
"""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from aria.core import executor as ex
from aria.core import plan_validator as pv
from aria.tools.handlers import web as webmod
from tests.test_19_4a_web_fetch_url import _stage3_env

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "web_fetch_null_url_5bb37d4c.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
RECORDS = DATA["records"]


def _step(record: dict) -> dict:
    return {
        "step_id": DATA["step_id"],
        "objective": DATA["objective"],
        "role": DATA["role"],
        "tool_ref": DATA["tool_ref"],
        "depends_on": [],
        "covers": [],
        "write_scope": [],
        "_log": f"{record['source']}:{record['line']}",
    }


def test_fixture_has_all_eighteen_log_hits():
    assert len(RECORDS) == 18
    assert {r["task"] for r in RECORDS} == {"36c58df9", "37d83fa6"}


@pytest.mark.parametrize(
    "record",
    RECORDS,
    ids=[f"{r['source']}:{r['line']}" for r in RECORDS],
)
def test_logged_web_fetch_without_url_never_reaches_network(record, tmp_path, monkeypatch):
    step = _step(record)
    step.pop("_log")
    with pytest.raises(ValueError) as ei:
        pv.validate_plan([step])
    assert "web_fetch" in str(ei.value) and "url" in str(ei.value)

    network_calls: list[str] = []

    def _no_network(*args, **kwargs):
        network_calls.append("open")
        raise AssertionError("web_fetch must not touch the network without a url")

    def _no_dns(*args, **kwargs):
        network_calls.append("dns")
        raise AssertionError("web_fetch must not resolve DNS without a url")

    monkeypatch.setattr(webmod, "_open_url", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)

    db, task, plan = _stage3_env([step], tmp_path, monkeypatch)
    calls = asyncio.run(ex._stage3_execute(db, task, plan, router=object()))
    assert calls[0]["status"] == "error", calls[0]
    assert calls[0]["output_json"]["error"].startswith("invalid_arguments (web_fetch)")
    assert network_calls == []
