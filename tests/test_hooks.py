"""Notification hook registration and best-effort behavior."""
import os
os.environ.setdefault("TRADE_LEDGER_AUDIT_LOG", "/tmp/trade_ledger_test_audit.jsonl")
os.environ.setdefault("TRADE_LEDGER_FALLBACK_LOG", "/tmp/trade_ledger_test_fallback.jsonl")

import pytest
from trade_ledger import open_trade, register_entry_hook
from trade_ledger.hooks import _clear_hooks_for_tests


@pytest.fixture(autouse=True)
def reset_hooks():
    _clear_hooks_for_tests()
    yield
    _clear_hooks_for_tests()


def test_entry_hook_called_when_webhook_passed():
    calls = []

    def hook(**payload):
        calls.append(payload)
        return {"message_id": "msg_123", "posted": True}

    register_entry_hook(hook)
    r = open_trade(method="momentum_breakout", asset_class="options", symbol="SPY",
                   direction="bullish", entry_price=1.0, qty=1, mode="live",
                   accepted=True, dry_run=True, webhook="http://example.com/hook")
    assert len(calls) == 1
    assert calls[0]["trade_id"] == r["trade_id"]
    assert r["entry_message_id"] == "msg_123"
    assert r["posted"] is True


def test_entry_hook_not_called_without_webhook():
    calls = []

    def hook(**payload):
        calls.append(payload)
        return {}

    register_entry_hook(hook)
    open_trade(method="momentum_breakout", asset_class="options", symbol="SPY",
               direction="bullish", entry_price=1.0, qty=1, mode="live",
               accepted=True, dry_run=True)
    assert calls == []


def test_failing_hook_does_not_break_trade():
    """A hook that raises must not prevent the trade row from being returned."""
    def bad_hook(**payload):
        raise RuntimeError("simulated downstream failure")

    register_entry_hook(bad_hook)
    r = open_trade(method="momentum_breakout", asset_class="options", symbol="SPY",
                   direction="bullish", entry_price=1.0, qty=1, mode="live",
                   accepted=True, dry_run=True, webhook="http://example.com/hook")
    assert r["trade_id"] is not None
    assert r["ledger_row"] is not None
    assert "simulated downstream failure" in (r["error"] or "")


def test_hook_skipped_when_message_id_preset():
    """If caller supplies entry_message_id (upstream already posted), hook is NOT fired."""
    calls = []

    def hook(**payload):
        calls.append(payload)
        return {"message_id": "should_not_be_used"}

    register_entry_hook(hook)
    r = open_trade(method="momentum_breakout", asset_class="options", symbol="SPY",
                   direction="bullish", entry_price=1.0, qty=1, mode="live",
                   accepted=True, dry_run=True, webhook="http://example.com/hook",
                   entry_message_id="upstream_msg_42")
    assert calls == []
    assert r["entry_message_id"] == "upstream_msg_42"
    assert r["posted"] is True
