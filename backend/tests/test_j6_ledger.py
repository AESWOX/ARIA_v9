"""J6: учёт деньги против токенов (без сети/LLM)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from aria.db import models as m
from aria.db.base import session_scope
from aria.jobs import ingest, ledger


def _lead(text="J6 order text") -> str:
    with session_scope() as db:
        return str(ingest.manual(db, text=text, title="J6").id)


def test_entry_report_formulas_and_unknown():
    lead_id = _lead()
    with session_scope() as db:
        entry = ledger.add_entry(
            db, lead_id=uuid.UUID(lead_id), revenue=100, currency="EUR",
            platform_fee=10, fx_to_usd=1.1, llm_cost_usd=2.5, hours=4, order_type="landing",
        )
        report = ledger.entry_report(entry)
    assert report["revenue_usd"] == 99.0
    assert report["profit_usd"] == 96.5
    assert report["profit_per_hour"] == 24.12
    assert report["return_multiple"] == 39.6
    assert report["order_type"] == "landing"

    with session_scope() as db:
        unknown = ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=50, currency="USD", llm_cost_usd=None, hours=0)
        report = ledger.entry_report(unknown)
    assert report["profit_usd"] is None and report["profit_per_hour"] is None and report["return_multiple"] is None


@pytest.mark.parametrize("kwargs", [
    {"revenue": -1, "fx_to_usd": 1},
    {"revenue": 10, "fx_to_usd": 0},
    {"revenue": 10, "platform_fee": 20},
    {"revenue": float("nan")},
    {"revenue": 10, "llm_cost_usd": -1},
    {"revenue": 10, "hours": float("inf")},
])
def test_validation_rejects_bad_numbers(kwargs):
    lead_id = uuid.UUID(_lead())
    with session_scope() as db:
        with pytest.raises(ValueError):
            ledger.add_entry(db, lead_id=lead_id, currency="USD", **kwargs)


def test_missing_lead_rejected():
    with session_scope() as db:
        with pytest.raises(ValueError):
            ledger.add_entry(db, lead_id=uuid.uuid4(), revenue=10, currency="USD")


def test_lead_report_and_month_boundaries():
    lead_id = _lead("J6 month order")
    jan = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)
    feb = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=10, currency="USD", llm_cost_usd=1, hours=2, created_at=jan)
        ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=20, currency="USD", llm_cost_usd=None, hours=1, created_at=jan)
        ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=30, currency="USD", llm_cost_usd=2, created_at=feb)
        report = ledger.monthly_report(db, 2026, 1)
        per_lead = ledger.lead_report(db, uuid.UUID(lead_id))

    assert report["entries"] == 2 and report["revenue_usd"] == 30.0
    assert report["revenue_usd_known"] == 10.0
    assert report["profit_usd"] == 9.0 and report["complete"] is False
    assert report["unknown_cost_entries"] == 1
    assert per_lead["entries"] == 3 and per_lead["complete"] is False


def test_monthly_report_rejects_bad_month():
    with session_scope() as db:
        with pytest.raises(ValueError):
            ledger.monthly_report(db, 2026, 13)
        with pytest.raises(ValueError):
            ledger.monthly_report(db, 0, 1)


def test_null_cost_round_trip():
    lead_id = _lead("J6 null cost")
    with session_scope() as db:
        entry = ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=5, currency="USD", llm_cost_usd=None)
        assert entry.llm_cost_usd is None
        db.flush()
        row = db.execute(select(m.OrderLedger).where(m.OrderLedger.id == entry.id)).scalar_one()
        assert row.llm_cost_usd is None


def test_migration_upgrade_and_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    import sqlalchemy as sa

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["dsn_explicit"] = f"sqlite:///{(tmp_path / 'j6.db').as_posix()}"
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "j4e5f6a7b8c9")
    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    assert "order_ledger" not in sa.inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    assert "order_ledger" in sa.inspect(engine).get_table_names()


def test_ledger_module_has_no_network_or_llm_imports():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(ledger.__file__).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(mod.startswith(("httpx", "requests", "socket", "aria.llm")) for mod in modules)


def test_zero_hours_known_cost_and_empty_currency():
    lead_id = _lead("J6 zero hours")
    with session_scope() as db:
        entry = ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=10, currency="usd", llm_cost_usd=1, hours=0, order_type="landing")
        report = ledger.entry_report(entry)
        assert report["profit_per_hour"] is None and report["currency"] == "USD"
        db.flush()
        persisted = db.get(m.OrderLedger, entry.id)
        assert persisted.order_type == "landing"
        with pytest.raises(ValueError):
            ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=1, currency=" ")


def test_created_at_normalized_to_utc_month():
    from datetime import timedelta

    lead_id = _lead("J6 tz")
    plus3 = datetime(2026, 2, 1, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    with session_scope() as db:
        ledger.add_entry(db, lead_id=uuid.UUID(lead_id), revenue=1, currency="USD", created_at=plus3)
    with session_scope() as db:
        january = ledger.monthly_report(db, 2026, 1)
    assert january["entries"] == 1
