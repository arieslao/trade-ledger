"""open_trade / close_trade input validation and the C-gate.

These tests cover the codepaths that return BEFORE any DB connection — so no
psycopg2, no Postgres required. Full DB-path coverage requires a live Postgres
and is exercised by integration tests on the consuming application's side.
"""
import os
os.environ.setdefault("TRADE_LEDGER_AUDIT_LOG", "/tmp/trade_ledger_test_audit.jsonl")
os.environ.setdefault("TRADE_LEDGER_FALLBACK_LOG", "/tmp/trade_ledger_test_fallback.jsonl")

import pytest
from trade_ledger import open_trade, close_trade


def _open_kwargs(**over):
    base = dict(method="momentum_breakout", asset_class="options", symbol="SPY",
                direction="bullish", entry_price=1.0, qty=1, mode="live")
    base.update(over)
    return base


# ── C-gate: refuse live row when broker accepted nothing ──────────────────────

def test_gate_refuses_live_when_not_accepted():
    r = open_trade(accepted=False, **_open_kwargs())
    assert r["trade_id"] is None
    assert "broker accepted no leg" in (r["error"] or "")


def test_gate_allows_live_when_accepted_dryrun():
    r = open_trade(accepted=True, dry_run=True, **_open_kwargs())
    assert r["trade_id"] is not None
    assert r["ledger_row"] is not None


def test_gate_allows_when_acceptance_unknown_backcompat():
    """accepted=None (equity callers, backfill) is allowed through."""
    r = open_trade(accepted=None, dry_run=True, **_open_kwargs())
    assert r["trade_id"] is not None


def test_gate_allows_shadow_without_acceptance_check():
    """Shadow rows aren't broker positions, so the C-gate doesn't apply."""
    r = open_trade(mode="shadow", accepted=False, dry_run=True,
                   **{k: v for k, v in _open_kwargs().items() if k != "mode"})
    assert r["trade_id"] is not None  # shadow row should still be written


# ── Input validation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_asset", ["stock", "ETF", "", None])
def test_invalid_asset_class_rejected(bad_asset):
    r = open_trade(**{**_open_kwargs(), "asset_class": bad_asset})
    assert r["trade_id"] is None
    assert "invalid asset_class" in r["error"]


@pytest.mark.parametrize("bad_src", ["limit", "market", "", None])
def test_invalid_price_source_rejected(bad_src):
    r = open_trade(**{**_open_kwargs(), "price_source": bad_src})
    assert r["trade_id"] is None
    assert "invalid price_source" in r["error"]


@pytest.mark.parametrize("bad_mode", ["real", "fake", "", None])
def test_invalid_mode_rejected(bad_mode):
    r = open_trade(**{**_open_kwargs(), "mode": bad_mode})
    assert r["trade_id"] is None
    assert "invalid mode" in r["error"]


@pytest.mark.parametrize("bad_tier", ["estimated", "guess", "", None])
def test_invalid_confidence_tier_rejected(bad_tier):
    r = open_trade(**{**_open_kwargs(), "confidence_tier": bad_tier})
    assert r["trade_id"] is None
    assert "invalid confidence_tier" in r["error"]


def test_close_invalid_exit_price_source_rejected():
    r = close_trade(trade_id="x", exit_price=1.0, reason="target_hit",
                    exit_price_source="invalid_src")
    assert r["ledger_row"] is None
    assert "invalid exit_price_source" in r["error"]


def test_close_invalid_pnl_source_rejected():
    r = close_trade(trade_id="x", exit_price=1.0, reason="target_hit",
                    pnl_source="guess")
    assert r["ledger_row"] is None
    assert "invalid pnl_source" in r["error"]


# ── Dry-run round-trip (no DB) ────────────────────────────────────────────────

def test_dryrun_returns_synthetic_row():
    r = open_trade(dry_run=True, **_open_kwargs())
    assert r["dry_run"] is True
    assert r["trade_id"] is not None
    row = r["ledger_row"]
    assert row["method"] == "momentum_breakout"
    assert row["symbol"] == "SPY"
    assert row["status"] == "open"
    assert row["mode"] == "live"


def test_dryrun_with_explicit_trade_id_honored():
    r = open_trade(dry_run=True, trade_id="backfill_001", **_open_kwargs())
    assert r["trade_id"] == "backfill_001"
    assert r["ledger_row"]["trade_id"] == "backfill_001"
