"""trade_ledger — canonical trade lifecycle ledger.

One row per trade in `trades` Postgres table; every signal-producing method
routes entries and exits through `open_trade` / `close_trade`.

Invariants:
- Errors NEVER raise — they return in the result dict and write to a fallback
  JSONL so trades are never silently dropped (failure isolation).
- All external side effects (DB, notifications) are wrapped.
- P&L is derived in exactly one place (`compute_pnl`), so pnl_dollars and pnl_pct
  cannot disagree in sign or magnitude.
- A caller-supplied `pnl_dollars` (e.g. a broker realized number) is honored only
  when it agrees in sign with the fill-derived value; a sign disagreement means
  a stale mark-based value leaked in and is rejected.
- A `mode='live'` row is refused when the broker accepted no leg, even when the
  order came back with an id (the 200-OK status='rejected' case).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
from typing import Optional

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.errors
    _PSYCOPG2_AVAILABLE = True
except Exception as _pg_err:  # pragma: no cover
    # Allow importing the module (and its pure helpers — broker_acceptance,
    # compute_pnl, generate_trade_id — for unit tests) without the DB driver.
    # Any actual DB op raises a clear error via _connect(). The stub keeps the
    # UniqueViolation / extras.Json references in the DB paths resolvable.
    logging.warning("trade_ledger: psycopg2 unavailable (%s) — DB ops disabled; "
                    "pure helpers still usable", _pg_err)
    _PSYCOPG2_AVAILABLE = False

    class _Psycopg2Stub:
        class errors:
            class UniqueViolation(Exception):
                pass

        class extras:
            RealDictCursor = None

            @staticmethod
            def Json(x):
                return x

        @staticmethod
        def connect(*_a, **_k):
            raise RuntimeError("psycopg2 not installed — install with: pip install 'trade-ledger[postgres]'")

    psycopg2 = _Psycopg2Stub()  # type: ignore

from .hooks import fire_entry_hooks, fire_exit_hooks

__all__ = [
    "open_trade",
    "close_trade",
    "find_open_trade",
    "reconcile_orphans",
    "compute_pnl",
    "broker_acceptance",
    "generate_trade_id",
    "POLICY_VERSION",
]

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
_FALLBACK_LOG = os.environ.get("TRADE_LEDGER_FALLBACK_LOG", "./trade_ledger_errors.jsonl")
_AUDIT_LOG = os.environ.get("TRADE_LEDGER_AUDIT_LOG", "./trade_ledger.jsonl")
POLICY_VERSION = "v1"

logger = logging.getLogger("trade_ledger")

# Enum values mirror the DB CHECK constraints in migrations/001_create_trades.sql.
_VALID_STATUS = {"open", "closed", "partial", "expired", "orphaned"}
_VALID_MODE = {"live", "shadow", "paper"}
_VALID_TIER = {"native", "reconstructed", "estimate"}
_VALID_ASSET = {"options", "equity", "crypto", "forex"}
_VALID_SRC = {"fill", "mid", "mark", "estimate"}
_VALID_PNL_SRC = {"broker", "computed", "estimate"}

# Broker order statuses that mean the order is NOT a live position.
# A 200-OK order with status='rejected' (broker-side risk reject) is the case an
# id-presence check misses — it has an id, but no position was opened.
_BROKER_DEAD_STATUSES = {"rejected", "canceled", "cancelled", "expired",
                         "suspended", "stopped", "replaced", "done_for_day"}


# ──────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────
def _resolve_dsn() -> Optional[str]:
    """Return the Postgres DSN from the DATABASE_URL env var (if set)."""
    return os.environ.get("DATABASE_URL")


def _connect():
    """Return a new Postgres connection. Caller closes."""
    dsn = _resolve_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL env var not set")
    return psycopg2.connect(dsn)


def _audit(event: str, payload: dict) -> None:
    """Append-only audit log. Never raises."""
    try:
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        os.makedirs(os.path.dirname(os.path.abspath(_AUDIT_LOG)) or ".", exist_ok=True)
        with open(_AUDIT_LOG, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass  # audit is best-effort


def _fallback(event: str, payload: dict, error: str) -> None:
    """Persist the payload when DB writes fail, so we never silently lose a trade."""
    try:
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event,
            "error": error,
            **payload,
        }
        os.makedirs(os.path.dirname(os.path.abspath(_FALLBACK_LOG)) or ".", exist_ok=True)
        with open(_FALLBACK_LOG, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        logger.error("trade_ledger %s wrote to fallback log: %s", event, error)
    except Exception as e2:  # pragma: no cover
        logger.exception("trade_ledger fallback log also failed: %s", e2)


def _row_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _options_multiplier(asset_class: str) -> int:
    return 100 if asset_class == "options" else 1


# ──────────────────────────────────────────────────────────────
# Pure helpers (no DB, no I/O)
# ──────────────────────────────────────────────────────────────
def generate_trade_id(method: str, symbol: str) -> str:
    """Mint a deterministic trade_id.

    Format: {method}_{symbol}_{utc_isoformat_compact}_{4hex}.
    Collisions are vanishingly improbable but the schema's PK enforces uniqueness.
    """
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{method}_{symbol}_{ts}_{secrets.token_hex(2)}"


def broker_acceptance(leg_orders) -> tuple[int, int]:
    """How many legs the broker actually accepted, as (n_accepted, n_total).

    `leg_orders` is a list of (leg, order_dict). A leg counts as accepted only
    when its order has an id AND its status is not a terminal dead state
    (rejected/canceled/expired/...). This is the single place broker order
    status is interpreted — engines must not re-implement it. accepted = n>0.
    """
    legs = leg_orders or []
    n_total = len(legs)
    n_accepted = 0
    for _leg, order in legs:
        order = order or {}
        if not order.get("id"):
            continue
        if (order.get("status") or "").lower() in _BROKER_DEAD_STATUSES:
            continue
        n_accepted += 1
    return n_accepted, n_total


def compute_pnl(*, entry_price, exit_price, qty, asset_class, direction,
                supplied_dollars=None, supplied_pnl_source="computed") -> dict:
    """Single source of truth for realized P&L. Pure / DB-free.

    pnl_dollars and pnl_pct are ALWAYS derived together from entry+exit, so the
    two can never disagree in sign or magnitude.

    Options are long-premium (you BUY the call or the put), so the direction
    sign applies only to assets you can actually be short (equity/forex/crypto).

    A caller-supplied `supplied_dollars` figure (e.g. a broker realized number
    incl. fees) is honored only when it agrees in sign with the fill-derived
    value; a sign disagreement means a stale/mark-based value leaked in and
    is rejected (returned as `rejected_supplied` for audit). The fill-derived
    value is used instead.

    Returns: {pnl_dollars, pnl_pct, pnl_source, rejected_supplied}
    """
    entry_price = float(entry_price)
    exit_price = float(exit_price)
    qty = float(qty)
    mult = _options_multiplier(asset_class)
    sign = -1 if (asset_class != "options"
                  and str(direction).lower() in ("short", "bearish", "sell")) else 1
    price_move = (exit_price - entry_price) * sign
    computed_dollars = round(price_move * qty * mult, 4)

    pnl_source = supplied_pnl_source
    rejected = None
    dollars = supplied_dollars
    if dollars is not None and (dollars >= 0) != (computed_dollars >= 0):
        rejected = dollars
        dollars = None
        pnl_source = "computed"
    if dollars is None:
        dollars = computed_dollars
    denom = entry_price * qty * mult
    pct = round((dollars / denom) * 100.0, 4) if denom else None
    return {"pnl_dollars": dollars, "pnl_pct": pct,
            "pnl_source": pnl_source, "rejected_supplied": rejected}


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────
def open_trade(
    *,
    method: str,
    asset_class: str,
    symbol: str,
    direction: str,
    entry_price: float,
    qty: float,
    contract: Optional[dict] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    hard_exit_at: Optional[str] = None,
    confidence: Optional[float] = None,
    reasoning: Optional[str] = None,
    price_source: str = "fill",
    mode: str = "live",
    accepted: Optional[bool] = None,
    confidence_tier: str = "native",
    webhook: Optional[str] = None,
    metadata: Optional[dict] = None,
    dry_run: bool = False,
    trade_id: Optional[str] = None,
    entry_message_id: Optional[str] = None,
) -> dict:
    """Mint trade_id (if not given), INSERT a row with status='open'.

    Args:
        accepted: When mode='live', refuse the INSERT if False (the broker
            accepted no leg). None means the caller did not assert acceptance
            (e.g. equity callers, backfill) — allowed through.
        entry_message_id: If set, stored on the row and no entry hook is fired.
            Use this when an upstream signal generator already posted to the
            comms channel and you just want the exit to thread under it.
        webhook: If set, passed through to entry hooks (which may post and
            return a message_id).

    Returns: {trade_id, entry_message_id, ledger_row, posted, error}
    Never raises.
    """
    if asset_class not in _VALID_ASSET:
        return {"trade_id": None, "ledger_row": None, "posted": False,
                "error": f"invalid asset_class={asset_class!r}"}
    if price_source not in _VALID_SRC:
        return {"trade_id": None, "ledger_row": None, "posted": False,
                "error": f"invalid price_source={price_source!r}"}
    if mode not in _VALID_MODE:
        return {"trade_id": None, "ledger_row": None, "posted": False,
                "error": f"invalid mode={mode!r}"}
    if confidence_tier not in _VALID_TIER:
        return {"trade_id": None, "ledger_row": None, "posted": False,
                "error": f"invalid confidence_tier={confidence_tier!r}"}

    # Broker-acceptance C-gate: a live row represents a real broker position.
    # If the caller passed acceptance=False, refuse the INSERT here — once, for
    # every engine — instead of each engine guarding it.
    if mode == "live" and accepted is False:
        _audit("open_trade_rejected_no_broker_ack",
               {"trade_id": trade_id or "(unassigned)", "method": method, "symbol": symbol})
        return {"trade_id": None, "ledger_row": None, "posted": False,
                "error": "broker accepted no leg — no live row written"}

    tid = trade_id or generate_trade_id(method, symbol)
    entry_ts = _now_utc()
    payload = {
        "trade_id": tid,
        "method": method,
        "asset_class": asset_class,
        "symbol": symbol,
        "direction": direction,
        "qty": qty,
        "entry_price": entry_price,
        "entry_price_source": price_source,
        "stop": stop,
        "target": target,
        "hard_exit_at": hard_exit_at,
        "confidence": confidence,
        "reasoning": reasoning,
        "mode": mode,
        "confidence_tier": confidence_tier,
        "metadata": metadata,
        "dry_run": dry_run,
    }

    # 1) Entry notification hook (best-effort; before DB so we can store message_id).
    posted = False
    hook_error: Optional[str] = None
    if entry_message_id is not None:
        # Caller-provided message_id (signal generator posted upstream)
        posted = True
    elif webhook:
        message_id, posted, hook_error = fire_entry_hooks(
            trade_id=tid, method=method, symbol=symbol, direction=direction,
            entry=float(entry_price), stop=stop, target=target,
            webhook=webhook, asset_class=asset_class,
            confidence=confidence, reasoning=reasoning,
            hard_exit_at=hard_exit_at, dry_run=dry_run,
        )
        entry_message_id = message_id

    # 2) DB INSERT
    if dry_run:
        ledger_row = {**payload, "entry_ts": entry_ts, "entry_message_id": entry_message_id,
                      "status": "open", "policy_version": POLICY_VERSION,
                      "created_at": entry_ts, "updated_at": entry_ts}
        _audit("open_trade_dryrun", {"trade_id": tid, "method": method, "symbol": symbol})
        return {"trade_id": tid, "entry_message_id": entry_message_id,
                "ledger_row": ledger_row, "posted": posted, "error": hook_error,
                "dry_run": True}

    try:
        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO trades (
                        trade_id, method, asset_class, symbol, direction, qty, contract,
                        entry_ts, entry_price, entry_price_source, entry_message_id,
                        stop, target, hard_exit_at, confidence, reasoning,
                        status, mode, confidence_tier, policy_version, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        'open', %s, %s, %s, %s
                    )
                    RETURNING *;
                    """,
                    (
                        tid, method, asset_class, symbol, direction, qty,
                        psycopg2.extras.Json(contract) if contract is not None else None,
                        entry_ts, entry_price, price_source, entry_message_id,
                        stop, target, hard_exit_at, confidence, reasoning,
                        mode, confidence_tier, POLICY_VERSION,
                        psycopg2.extras.Json(metadata) if metadata is not None else None,
                    ),
                )
                row = _row_to_dict(cur.fetchone())
            conn.commit()
        finally:
            conn.close()
    except psycopg2.errors.UniqueViolation:
        # Idempotency: re-open of same trade_id is a no-op (return the existing row).
        try:
            conn = _connect()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM trades WHERE trade_id = %s;", (tid,))
                    row = _row_to_dict(cur.fetchone())
            finally:
                conn.close()
            _audit("open_trade_idempotent_hit", {"trade_id": tid})
            return {"trade_id": tid, "entry_message_id": (row or {}).get("entry_message_id"),
                    "ledger_row": row, "posted": posted, "error": None}
        except Exception as e2:
            _fallback("open_trade", payload, f"unique_violation_and_reread_failed: {e2}")
            return {"trade_id": tid, "ledger_row": None, "posted": posted,
                    "error": f"unique_violation_and_reread_failed: {e2}"}
    except Exception as e:
        _fallback("open_trade", payload, str(e))
        return {"trade_id": tid, "ledger_row": None, "posted": posted,
                "error": f"db_insert_failed: {e}"}

    _audit("open_trade", {"trade_id": tid, "method": method, "symbol": symbol,
                          "entry_price": entry_price, "mode": mode})
    return {"trade_id": tid, "entry_message_id": entry_message_id,
            "ledger_row": row, "posted": posted, "error": hook_error}


def close_trade(
    *,
    trade_id: str,
    exit_price: float,
    reason: str,
    pnl_dollars: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    exit_price_source: str = "fill",
    pnl_source: str = "broker",
    webhook: Optional[str] = None,
    partial: bool = False,
    metadata: Optional[dict] = None,
    dry_run: bool = False,
) -> dict:
    """UPDATE the row: set exit_ts/exit_price/pnl_*, status='closed' (or 'partial').

    P&L is ALWAYS derived here, in one place, from the stored entry_price + this
    exit fill, so pnl_dollars and pnl_pct can never disagree in the stored row.
    Callers should pass the real exit FILL price (the NET debit for spreads).
    A caller-supplied pnl_dollars that disagrees in sign with the fill-derived
    value is rejected and stashed in metadata.broker_pnl_dollars for audit.

    Returns: {trade_id, exit_message_id, ledger_row, posted, hold_duration, error}
    Never raises.
    """
    if exit_price_source not in _VALID_SRC:
        return {"trade_id": trade_id, "ledger_row": None, "posted": False,
                "error": f"invalid exit_price_source={exit_price_source!r}"}
    if pnl_source not in _VALID_PNL_SRC:
        return {"trade_id": trade_id, "ledger_row": None, "posted": False,
                "error": f"invalid pnl_source={pnl_source!r}"}

    # Pull the open row first to compute P&L, find entry_message_id, etc.
    try:
        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM trades WHERE trade_id = %s FOR UPDATE;", (trade_id,))
                existing = _row_to_dict(cur.fetchone())
                if existing is None:
                    return {"trade_id": trade_id, "ledger_row": None, "posted": False,
                            "error": "trade_id not found"}
                if existing["status"] not in ("open", "partial"):
                    # Idempotency: re-close is a no-op
                    _audit("close_trade_idempotent_skip",
                           {"trade_id": trade_id, "existing_status": existing["status"]})
                    return {"trade_id": trade_id, "ledger_row": existing, "posted": False,
                            "exit_message_id": existing.get("exit_message_id"),
                            "error": f"already {existing['status']}"}

                entry_price = float(existing["entry_price"])
                qty = float(existing["qty"])
                asset_class = existing["asset_class"]
                direction = existing["direction"]
                _pnl = compute_pnl(
                    entry_price=entry_price, exit_price=exit_price, qty=qty,
                    asset_class=asset_class, direction=direction,
                    supplied_dollars=pnl_dollars, supplied_pnl_source=pnl_source,
                )
                if _pnl["rejected_supplied"] is not None:
                    logger.warning(
                        "close_trade %s: supplied pnl_dollars=%s disagrees in sign with "
                        "fill-derived %s (entry=%s exit=%s qty=%s) -- using fill-derived, "
                        "stashing supplied in metadata.broker_pnl_dollars",
                        trade_id, _pnl["rejected_supplied"], _pnl["pnl_dollars"],
                        entry_price, exit_price, qty)
                    metadata = dict(metadata or {})
                    metadata["broker_pnl_dollars"] = _pnl["rejected_supplied"]
                pnl_dollars = _pnl["pnl_dollars"]
                pnl_pct = _pnl["pnl_pct"]
                pnl_source = _pnl["pnl_source"]

                exit_ts = _now_utc()
                new_status = "partial" if partial else "closed"
                entry_message_id = existing.get("entry_message_id")

                merged_meta = dict(existing.get("metadata") or {})
                if metadata:
                    merged_meta.update(metadata)

                if dry_run:
                    row = {**existing,
                           "exit_ts": exit_ts, "exit_price": exit_price,
                           "exit_price_source": exit_price_source,
                           "exit_reason": reason,
                           "pnl_dollars": pnl_dollars, "pnl_pct": pnl_pct,
                           "pnl_source": pnl_source,
                           "status": new_status, "metadata": merged_meta}
                else:
                    cur.execute(
                        """
                        UPDATE trades SET
                            exit_ts = %s,
                            exit_price = %s,
                            exit_price_source = %s,
                            exit_reason = %s,
                            pnl_dollars = %s,
                            pnl_pct = %s,
                            pnl_source = %s,
                            status = %s,
                            metadata = COALESCE(%s, metadata),
                            updated_at = NOW()
                        WHERE trade_id = %s
                        RETURNING *;
                        """,
                        (exit_ts, exit_price, exit_price_source, reason,
                         pnl_dollars, pnl_pct, pnl_source, new_status,
                         psycopg2.extras.Json(merged_meta) if merged_meta else None,
                         trade_id),
                    )
                    row = _row_to_dict(cur.fetchone())
                    conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _fallback("close_trade", {"trade_id": trade_id, "exit_price": exit_price,
                                  "reason": reason, "partial": partial}, str(e))
        return {"trade_id": trade_id, "ledger_row": None, "posted": False,
                "error": f"db_update_failed: {e}"}

    # Exit notification hook (best-effort, threaded under entry if message_id known)
    exit_message_id: Optional[str] = None
    posted = False
    hook_error: Optional[str] = None
    hold_duration = _format_duration(row["entry_ts"], row.get("exit_ts") or _now_utc())
    if webhook:
        exit_message_id, posted, hook_error = fire_exit_hooks(
            trade_id=trade_id, symbol=row["symbol"], method=row["method"],
            exit_price=float(exit_price), reason=reason,
            pnl_pct=float(pnl_pct) if pnl_pct is not None else 0.0,
            pnl_dollars=float(pnl_dollars) if pnl_dollars is not None else None,
            webhook=webhook,
            hold_duration=hold_duration,
            reply_to_message_id=entry_message_id,
            partial=partial,
            dry_run=dry_run,
        )
        if exit_message_id and not dry_run:
            _store_exit_message_id(trade_id, exit_message_id)

    _audit("close_trade", {"trade_id": trade_id, "exit_price": exit_price,
                           "reason": reason, "pnl_dollars": pnl_dollars,
                           "pnl_pct": pnl_pct, "partial": partial, "dry_run": dry_run})

    return {"trade_id": trade_id, "exit_message_id": exit_message_id,
            "ledger_row": row, "posted": posted,
            "hold_duration": hold_duration, "error": hook_error}


def find_open_trade(
    *,
    method: str,
    symbol: str,
    contract_occ: Optional[str] = None,
) -> Optional[dict]:
    """Return the most recent open *live* trade matching (method, symbol[, contract_occ]).

    Used by exit monitors to look up the trade_id when a position is about to close.
    Returns None on miss; never raises.

    Important: only mode='live' rows are returned. A broker exit must NEVER attach
    to a mode='shadow' counterfactual row — shadows share the same (method, symbol)
    and an overlapping OCC leg, are written milliseconds AFTER the live row, and
    would otherwise win `ORDER BY entry_ts DESC`, stealing the real exit and leaving
    the live row dangling open. Shadows are closed by a separate paired-shadow
    resolver.
    """
    try:
        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if contract_occ:
                    cur.execute(
                        """
                        SELECT * FROM trades
                        WHERE status = 'open' AND mode = 'live'
                          AND method = %s AND symbol = %s
                          AND (
                            contract->>'symbol' = %s
                            OR EXISTS (
                              SELECT 1 FROM jsonb_array_elements(contract->'legs') AS leg
                              WHERE leg->>'symbol' = %s
                                 OR REPLACE(leg->>'symbol',' ','') = REPLACE(%s,' ','')
                            )
                          )
                        ORDER BY entry_ts DESC LIMIT 1;
                        """,
                        (method, symbol, contract_occ, contract_occ, contract_occ),
                    )
                else:
                    cur.execute(
                        """
                        SELECT * FROM trades
                        WHERE status = 'open' AND mode = 'live'
                          AND method = %s AND symbol = %s
                        ORDER BY entry_ts DESC LIMIT 1;
                        """,
                        (method, symbol),
                    )
                return _row_to_dict(cur.fetchone())
        finally:
            conn.close()
    except Exception as e:
        logger.warning("trade_ledger.find_open_trade failed: %s", e)
        return None


def reconcile_orphans(*, dry_run: bool = False) -> dict:
    """Sweep open trades with entry_ts > 24h old; mark them status='orphaned'.

    Returns: {"swept": int, "orphaned": int, "skipped": int, "dry_run": bool}
    """
    swept = 0
    orphaned = 0
    skipped = 0
    try:
        conn = _connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT trade_id, entry_ts, hard_exit_at, method, symbol
                    FROM trades
                    WHERE status = 'open'
                      AND entry_ts < NOW() - INTERVAL '24 hours'
                    ORDER BY entry_ts ASC;
                    """
                )
                stale = [_row_to_dict(r) for r in cur.fetchall()]
                swept = len(stale)
                for row in stale:
                    tid = row["trade_id"]
                    if dry_run:
                        orphaned += 1
                        continue
                    cur.execute(
                        """
                        UPDATE trades SET
                            status = 'orphaned',
                            exit_ts = COALESCE(exit_ts, NOW()),
                            exit_price = COALESCE(exit_price, entry_price),
                            exit_price_source = COALESCE(exit_price_source, 'estimate'),
                            exit_reason = COALESCE(exit_reason, 'reconcile_orphan'),
                            pnl_dollars = COALESCE(pnl_dollars, 0),
                            pnl_pct = COALESCE(pnl_pct, 0),
                            pnl_source = COALESCE(pnl_source, 'estimate'),
                            metadata = COALESCE(metadata, '{}'::jsonb)
                                        || jsonb_build_object('orphan_swept_at', NOW()::text),
                            updated_at = NOW()
                        WHERE trade_id = %s AND status = 'open';
                        """,
                        (tid,),
                    )
                    if cur.rowcount == 1:
                        orphaned += 1
                        _audit("reconcile_orphan", {"trade_id": tid, "method": row.get("method"),
                                                   "symbol": row.get("symbol")})
                    else:
                        skipped += 1
            if not dry_run:
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.exception("trade_ledger.reconcile_orphans failed: %s", e)
        return {"swept": swept, "orphaned": orphaned, "skipped": skipped,
                "dry_run": dry_run, "error": str(e)}
    return {"swept": swept, "orphaned": orphaned, "skipped": skipped, "dry_run": dry_run}


# ──────────────────────────────────────────────────────────────
# Internal: post-close message_id persistence + duration formatting
# ──────────────────────────────────────────────────────────────
def _store_exit_message_id(trade_id: str, exit_message_id: str) -> None:
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trades SET exit_message_id = %s WHERE trade_id = %s;",
                    (exit_message_id, trade_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("trade_ledger: persisting exit_message_id failed: %s", e)


def _format_duration(start, end) -> str:
    try:
        if isinstance(start, str):
            start = dt.datetime.fromisoformat(start)
        if isinstance(end, str):
            end = dt.datetime.fromisoformat(end)
        delta = end - start
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        h, m = divmod(secs // 60, 60)
        if h < 24:
            return f"{h}h {m}m" if m else f"{h}h"
        d = h // 24
        return f"{d}d {h % 24}h"
    except Exception:
        return ""
