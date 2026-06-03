"""compute_pnl invariants — the P&L consistency contract.

These are pure function tests. No DB, no psycopg2 needed.
"""
import os
os.environ.setdefault("TRADE_LEDGER_AUDIT_LOG", "/tmp/trade_ledger_test_audit.jsonl")
os.environ.setdefault("TRADE_LEDGER_FALLBACK_LOG", "/tmp/trade_ledger_test_fallback.jsonl")

from trade_ledger import compute_pnl


def _sign(x):
    return (x > 0) - (x < 0)


def test_mark_override_rejected_on_sign_mismatch():
    """A caller-supplied mark-based pnl_dollars that disagrees in sign with the
    fill-derived value is rejected. Regression test for the production case:
    a bullish call winner (entry 0.39 → exit 0.97) where the broker's
    unrealized mark P&L of -51 was passed alongside a fill-derived +58 winner.
    Storing both would have left pnl_dollars=-51 sitting next to pnl_pct=+148%.
    """
    r = compute_pnl(entry_price=0.39, exit_price=0.97, qty=1,
                    asset_class="options", direction="bullish",
                    supplied_dollars=-51.0, supplied_pnl_source="broker")
    assert r["rejected_supplied"] == -51.0
    assert r["pnl_dollars"] == 58.0
    assert r["pnl_source"] == "computed"
    assert _sign(r["pnl_dollars"]) == _sign(r["pnl_pct"]) == 1


def test_bearish_put_is_long_premium_not_flipped():
    """A bearish PUT held long is still LONG premium — a winner must NOT flip to a loss."""
    r = compute_pnl(entry_price=1.00, exit_price=2.50, qty=1,
                    asset_class="options", direction="bearish")
    assert r["pnl_dollars"] == 150.0
    assert r["pnl_pct"] == 150.0


def test_equity_short_sign_applies():
    """Direction sign DOES apply to assets you can be short (equity)."""
    r = compute_pnl(entry_price=100.0, exit_price=90.0, qty=10,
                    asset_class="equity", direction="short")
    assert r["pnl_dollars"] == 100.0
    assert r["pnl_pct"] == 10.0


def test_honored_broker_figure_when_sign_agrees():
    """A broker realized figure that agrees in sign is honored; pct stays consistent."""
    r = compute_pnl(entry_price=0.39, exit_price=0.97, qty=1,
                    asset_class="options", direction="bullish",
                    supplied_dollars=55.0, supplied_pnl_source="broker")
    assert r["rejected_supplied"] is None
    assert r["pnl_dollars"] == 55.0
    assert r["pnl_source"] == "broker"
    assert _sign(r["pnl_dollars"]) == _sign(r["pnl_pct"]) == 1


def test_dollars_and_pct_never_disagree_in_sign():
    """The core invariant, across many cases."""
    cases = [
        dict(entry_price=0.39, exit_price=0.97, qty=1, asset_class="options", direction="bullish"),
        dict(entry_price=1.50, exit_price=0.20, qty=2, asset_class="options", direction="bullish"),
        dict(entry_price=1.00, exit_price=2.50, qty=1, asset_class="options", direction="bearish"),
        dict(entry_price=100.0, exit_price=90.0, qty=10, asset_class="equity", direction="short"),
        dict(entry_price=100.0, exit_price=110.0, qty=5, asset_class="equity", direction="long"),
    ]
    for c in cases:
        r = compute_pnl(**c)
        if r["pnl_pct"] is not None:
            assert _sign(r["pnl_dollars"]) == _sign(r["pnl_pct"]), c


def test_zero_entry_price_is_safe():
    """entry_price=0 must not crash (pct undefined)."""
    r = compute_pnl(entry_price=0.0, exit_price=0.50, qty=1,
                    asset_class="options", direction="bullish")
    assert r["pnl_pct"] is None
    assert r["pnl_dollars"] == 50.0


def test_options_multiplier_applied():
    """Options pnl_dollars accounts for the 100x contract multiplier."""
    r = compute_pnl(entry_price=1.00, exit_price=2.00, qty=1,
                    asset_class="options", direction="bullish")
    assert r["pnl_dollars"] == 100.0


def test_equity_long_no_multiplier():
    """Equity does not apply the 100x multiplier."""
    r = compute_pnl(entry_price=10.0, exit_price=11.0, qty=10,
                    asset_class="equity", direction="long")
    assert r["pnl_dollars"] == 10.0
    assert r["pnl_pct"] == 10.0
