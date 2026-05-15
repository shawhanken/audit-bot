from __future__ import annotations

from app.budget import BudgetLedger
from app.schema import AuditResponse
from app.storage import HistoryStore


def test_history_store_records_and_bumps(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    store.record(repo="o/n", signature="abc", finding=AuditResponse(request_id="r1"))
    state = store.lookup("o/n", "abc")
    assert state and state.occurrences == 1
    store.bump(repo="o/n", signature="abc")
    state = store.lookup("o/n", "abc")
    assert state and state.occurrences == 2


def test_budget_records_and_caps(tmp_path):
    led = BudgetLedger(str(tmp_path / "b.db"), default_monthly_cap_usd=1.0)
    led.set_cap("o/n", 1.0)
    s = led.state("o/n")
    assert s.cap_usd == 1.0 and s.spent_usd == 0
    led.record(
        repo="o/n", request_id="r1", pr_number=1,
        tokens_input=10, tokens_output=5, tokens_cached_read=0,
        cost_usd=0.85, duration_ms=1000,
    )
    s = led.state("o/n")
    assert s.spent_usd == 0.85 and s.soft_breach is True and s.hard_breach is False
    led.record(
        repo="o/n", request_id="r2", pr_number=2,
        tokens_input=10, tokens_output=5, tokens_cached_read=0,
        cost_usd=0.20, duration_ms=1000,
    )
    s = led.state("o/n")
    assert s.hard_breach is True
