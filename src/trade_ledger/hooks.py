"""Pluggable notification hooks for trade-ledger.

Hooks are best-effort: if a hook raises, the trade row is still written and the
error is captured in the result dict, never propagated to the caller.

Register your own notifier (Discord, Slack, ntfy, email, webhook, etc.):

    from trade_ledger import register_entry_hook, register_exit_hook

    def my_entry_notifier(*, trade_id, method, symbol, direction, entry,
                          stop, target, asset_class, confidence, reasoning,
                          hard_exit_at, dry_run, **kwargs):
        # Post to your channel; return {"message_id": "...", "posted": True}
        # message_id is stored on the row and passed to the exit hook for threading.
        ...

    def my_exit_notifier(*, trade_id, symbol, method, exit_price, reason,
                         pnl_pct, pnl_dollars, hold_duration,
                         reply_to_message_id, partial, dry_run, **kwargs):
        ...

    register_entry_hook(my_entry_notifier)
    register_exit_hook(my_exit_notifier)
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("trade_ledger.hooks")

_entry_hooks: list[Callable] = []
_exit_hooks: list[Callable] = []


def register_entry_hook(fn: Callable) -> None:
    """Register a notifier called on every open_trade with a webhook."""
    _entry_hooks.append(fn)


def register_exit_hook(fn: Callable) -> None:
    """Register a notifier called on every close_trade with a webhook."""
    _exit_hooks.append(fn)


def fire_entry_hooks(**payload) -> tuple[Optional[str], bool, Optional[str]]:
    """Invoke all registered entry hooks. Returns (message_id, posted, error).

    message_id and posted come from the LAST hook that returns them (so chains
    of notifiers stack). Errors from any hook are captured but never propagate.
    """
    message_id: Optional[str] = None
    posted = False
    error: Optional[str] = None
    for fn in _entry_hooks:
        try:
            r = fn(**payload) or {}
            if r.get("message_id"):
                message_id = r["message_id"]
            if r.get("posted"):
                posted = True
            if r.get("error"):
                error = r["error"]
        except Exception as e:
            logger.warning("entry hook %s failed: %s", getattr(fn, "__name__", fn), e)
            error = str(e)
    return message_id, posted, error


def fire_exit_hooks(**payload) -> tuple[Optional[str], bool, Optional[str]]:
    """Invoke all registered exit hooks. Returns (message_id, posted, error)."""
    message_id: Optional[str] = None
    posted = False
    error: Optional[str] = None
    for fn in _exit_hooks:
        try:
            r = fn(**payload) or {}
            if r.get("message_id"):
                message_id = r["message_id"]
            if r.get("posted"):
                posted = True
            if r.get("error"):
                error = r["error"]
        except Exception as e:
            logger.warning("exit hook %s failed: %s", getattr(fn, "__name__", fn), e)
            error = str(e)
    return message_id, posted, error


def _clear_hooks_for_tests() -> None:
    """Test-only: clear registered hooks between tests."""
    _entry_hooks.clear()
    _exit_hooks.clear()
