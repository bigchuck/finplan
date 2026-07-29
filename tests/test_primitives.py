"""Milestone 1a checks — plain asserts, no test framework.

Run directly:  python tests/test_primitives.py
"""

import datetime as dt
import sys
from pathlib import Path

# Make `finplan` importable when run directly: add …/src (the dir above the
# package/repo root) to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.primitives import (
    Posting, Transaction, UnbalancedTransaction, money, ZERO,
)
from finplan.engine import Engine


D = dt.date(2026, 1, 1)


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} to be raised")


def test_balanced_transaction_constructs():
    txn = Transaction(D, "interest", [
        Posting("Assets:Checking", money("100.00")),
        Posting("Income:Interest", money("-100.00")),
    ])
    assert sum((p.amount for p in txn.postings), ZERO) == ZERO


def test_unbalanced_transaction_rejected():
    _raises(UnbalancedTransaction, lambda: Transaction(D, "leak", [
        Posting("Assets:Checking", money("100.00")),
        Posting("Income:Interest", money("-99.99")),
    ]))


def test_money_quantizes_to_cents():
    assert money("1.005") == money("1.01")   # ROUND_HALF_UP
    assert money(3) == money("3.00")
    assert str(money("0.1")) == "0.10"


def test_engine_whole_ledger_stays_zero():
    eng = Engine()
    eng.post(Transaction(D, "open", [
        Posting("Assets:Checking", money("100000")),
        Posting("Equity:Opening", money("-100000")),
    ]))
    assert eng.balance("Assets:Checking") == money("100000")
    assert sum(eng.balances().values(), ZERO) == ZERO


def test_leak_cannot_be_constructed():
    _raises(UnbalancedTransaction,
            lambda: Transaction(D, "bad", [Posting("Assets:Checking", money("5"))]))


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))