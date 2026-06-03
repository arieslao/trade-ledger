"""broker_acceptance — interprets multi-leg broker order responses correctly.

The headline failure mode this guards against: a 200-OK broker response that
contains an order id but reports status='rejected' (broker-side risk reject).
An id-presence check passes; the position does not exist. Writing a live row
from that would create a phantom trade.
"""
import os
os.environ.setdefault("TRADE_LEDGER_AUDIT_LOG", "/tmp/trade_ledger_test_audit.jsonl")
os.environ.setdefault("TRADE_LEDGER_FALLBACK_LOG", "/tmp/trade_ledger_test_fallback.jsonl")

from trade_ledger import broker_acceptance


def test_acceptance_filled():
    assert broker_acceptance([({}, {"id": "a", "status": "filled"})]) == (1, 1)


def test_acceptance_200ok_rejected_is_not_accepted():
    """The phantom-row bug: 200-OK order, has an id, status='rejected' → NOT live."""
    assert broker_acceptance([({}, {"id": "b", "status": "rejected"})]) == (0, 1)


def test_acceptance_http_error_no_order():
    assert broker_acceptance([({}, None)]) == (0, 1)


def test_acceptance_partial_spread():
    legs = [({}, {"id": "c", "status": "filled"}),
            ({}, {"id": "d", "status": "rejected"})]
    assert broker_acceptance(legs) == (1, 2)


def test_acceptance_pending_states_count():
    assert broker_acceptance([({}, {"id": "e", "status": "new"})]) == (1, 1)
    assert broker_acceptance([({}, {"id": "f", "status": "accepted"})]) == (1, 1)


def test_acceptance_canceled_not_accepted():
    assert broker_acceptance([({}, {"id": "g", "status": "canceled"})]) == (0, 1)


def test_acceptance_expired_not_accepted():
    assert broker_acceptance([({}, {"id": "h", "status": "expired"})]) == (0, 1)


def test_acceptance_empty():
    assert broker_acceptance([]) == (0, 0)
    assert broker_acceptance(None) == (0, 0)


def test_acceptance_case_insensitive_status():
    """Status comparison should be case-insensitive (brokers vary in casing)."""
    assert broker_acceptance([({}, {"id": "i", "status": "REJECTED"})]) == (0, 1)
    assert broker_acceptance([({}, {"id": "j", "status": "Filled"})]) == (1, 1)
