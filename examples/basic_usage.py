"""basic_usage.py — minimal trade-ledger walkthrough.

Two modes:

  1) **Without Postgres** (the default): demonstrates the pure helpers
     (compute_pnl, broker_acceptance, generate_trade_id) and the dry-run
     path of open_trade. No DB connection needed.

  2) **With Postgres** (set DATABASE_URL): also exercises close_trade and
     find_open_trade against a real `trades` table.

Setup for mode (2):
    pip install "trade-ledger[postgres]"
    psql $DATABASE_URL -f migrations/001_create_trades.sql
    export DATABASE_URL="postgresql://user:pass@host:5432/db"
    python examples/basic_usage.py
"""
import os

from trade_ledger import (
    open_trade,
    close_trade,
    find_open_trade,
    compute_pnl,
    broker_acceptance,
    register_entry_hook,
    register_exit_hook,
)

HAS_DB = bool(os.environ.get("DATABASE_URL"))


# ── 1) Register a simple notifier hook (optional) ─────────────────────────────
def print_entry(*, trade_id, method, symbol, direction, entry, **_):
    print(f"  [entry hook] {trade_id}: {method} {symbol} {direction} @ {entry}")
    return {"message_id": f"console_{trade_id}", "posted": True}


def print_exit(*, trade_id, symbol, exit_price, reason, pnl_pct, **_):
    print(f"  [exit hook]  {trade_id}: {symbol} → {exit_price} ({reason}, {pnl_pct:+.2f}%)")
    return {"message_id": f"console_exit_{trade_id}", "posted": True}


register_entry_hook(print_entry)
register_exit_hook(print_exit)


# ── 2) Pure-helper demos (no DB needed) ───────────────────────────────────────
print("== Pure helpers (no DB) ==")

# compute_pnl: single source of truth for P&L
pnl = compute_pnl(entry_price=1.25, exit_price=2.10, qty=2,
                  asset_class="options", direction="bullish")
print(f"compute_pnl({1.25} → {2.10}, qty=2, options bullish) "
      f"= ${pnl['pnl_dollars']:.2f} ({pnl['pnl_pct']:+.2f}%)")

# compute_pnl will reject a mark-based broker number that disagrees in sign
bad = compute_pnl(entry_price=0.39, exit_price=0.97, qty=1,
                  asset_class="options", direction="bullish",
                  supplied_dollars=-51.0, supplied_pnl_source="broker")
print(f"compute_pnl rejects mark-based override: rejected_supplied={bad['rejected_supplied']}, "
      f"using fill-derived ${bad['pnl_dollars']:.2f}")

# broker_acceptance: a 200-OK rejected order is NOT a live position
broker_response = [({}, {"id": "ord_abc123", "status": "filled", "filled_avg_price": 1.25})]
n_accepted, n_total = broker_acceptance(broker_response)
print(f"broker_acceptance(1 filled leg) = ({n_accepted}, {n_total})")

rejected_response = [({}, {"id": "ord_xyz", "status": "rejected"})]
n_accepted_bad, n_total_bad = broker_acceptance(rejected_response)
print(f"broker_acceptance(1 rejected leg) = ({n_accepted_bad}, {n_total_bad})  "
      "← no live row should be written")

print()

# ── 3) open_trade in dry-run mode (no DB write, returns synthetic row) ────────
print("== open_trade dry-run (no DB write) ==")

chosen = {"symbol": "SPY250620C00450000", "strike": 450.0,
          "expiry": "2025-06-20", "right": "C"}
unchosen = {
    "legs": [
        {"symbol": "SPY250620C00445000", "side": "buy",  "strike": 445.0, "right": "C"},
        {"symbol": "SPY250620C00455000", "side": "sell", "strike": 455.0, "right": "C"},
    ],
    "expiry": "2025-06-20",
}

live_dry = open_trade(
    method="momentum_breakout",
    asset_class="options",
    symbol="SPY",
    direction="bullish",
    entry_price=1.25,
    qty=2,
    contract=chosen,
    stop=0.90,
    target=2.00,
    accepted=True,
    mode="live",
    confidence=0.78,
    reasoning="20-bar high broken with rising RSI; regime=trend_up.",
    webhook="http://example.com/hook",
    dry_run=True,
)
print(f"Live trade dry-run id: {live_dry['trade_id']}\n")

shadow_dry = open_trade(
    method="momentum_breakout",
    asset_class="options",
    symbol="SPY",
    direction="bullish",
    entry_price=0.42,
    qty=2,
    contract=unchosen,
    price_source="mark",
    mode="shadow",
    metadata={"shadow_pair_of": live_dry["trade_id"], "shadow_reason": "executor_unselected"},
    dry_run=True,
)
print(f"Shadow dry-run id:     {shadow_dry['trade_id']}\n")

# ── 4) Full round-trip — requires Postgres ────────────────────────────────────
if not HAS_DB:
    print("== Skipping live round-trip ==")
    print("DATABASE_URL not set. Set it and re-run to see open → find_open → close in action.")
else:
    print("== Live round-trip against Postgres ==")
    live = open_trade(
        method="momentum_breakout",
        asset_class="options",
        symbol="SPY",
        direction="bullish",
        entry_price=1.25,
        qty=2,
        contract=chosen,
        accepted=True,
        mode="live",
        webhook="http://example.com/hook",
    )
    print(f"Opened: {live['trade_id']}")

    found = find_open_trade(method="momentum_breakout", symbol="SPY",
                            contract_occ="SPY250620C00450000")
    print(f"find_open_trade returned trade_id={found and found['trade_id']}")

    close = close_trade(
        trade_id=live["trade_id"],
        exit_price=2.10,
        reason="target_hit",
        exit_price_source="fill",
        pnl_source="broker",
        webhook="http://example.com/hook",
    )
    print(f"Closed in {close.get('hold_duration')}: "
          f"pnl=${close['ledger_row']['pnl_dollars']:.2f} "
          f"({close['ledger_row']['pnl_pct']:+.2f}%)")
