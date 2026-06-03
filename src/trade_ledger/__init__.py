"""trade-ledger — canonical trade lifecycle ledger for algorithmic trading systems.

Public API:
    open_trade(...)         → mints trade_id, INSERTs row, runs optional entry hooks
    close_trade(...)        → UPDATEs row, derives P&L, runs optional exit hooks
    find_open_trade(...)    → look up open live trade by (method, symbol [, contract_occ])
    reconcile_orphans(...)  → sweep stale opens past their hard exit window
    compute_pnl(...)        → pure helper: single source of truth for realized P&L
    broker_acceptance(...)  → pure helper: number of legs the broker actually accepted
    generate_trade_id(...)  → mint a deterministic trade id
    register_entry_hook(fn) → register a notifier called on every open_trade
    register_exit_hook(fn)  → register a notifier called on every close_trade

See README.md for usage.
"""
from .ledger import (
    open_trade,
    close_trade,
    find_open_trade,
    reconcile_orphans,
    compute_pnl,
    broker_acceptance,
    generate_trade_id,
    POLICY_VERSION,
)
from .hooks import register_entry_hook, register_exit_hook

__all__ = [
    "open_trade",
    "close_trade",
    "find_open_trade",
    "reconcile_orphans",
    "compute_pnl",
    "broker_acceptance",
    "generate_trade_id",
    "register_entry_hook",
    "register_exit_hook",
    "POLICY_VERSION",
]

__version__ = "0.1.0"
