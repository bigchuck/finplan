"""Scenario 1 checks — plain asserts, no test framework.

Run directly:  python tests/test_scenario1.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO


CONTROL = {
    "start": "2026-01-01",
    "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00", "apr": "0.03"},
    ],
}


def run_one_year():
    engine, sim, years = build(CONTROL)
    sim.run(years)
    return engine


def test_whole_ledger_sums_to_zero_after_full_year():
    engine = run_one_year()
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_opening_snapshot_frozen():
    engine = run_one_year()
    assert engine.balance("Equity:Opening") == money("-100000.00")


def test_interest_accrued_and_compounded():
    engine = run_one_year()
    interest = engine.balance("Assets:Checking") - money("100000.00")
    # 3% APR compounded monthly on 100k lands just above simple 3000.
    assert money("3000") < interest < money("3050")


def test_income_gate_swept_at_close():
    engine = run_one_year()
    assert engine.balance("Income:Interest") == ZERO
    growth = engine.balance("Assets:Checking") - money("100000.00")
    assert engine.balance("Equity:RetainedEarnings") == -growth


def test_net_worth_identity_holds():
    engine = run_one_year()
    assets = engine.balance("Assets:Checking")
    equity = (engine.balance("Equity:Opening")
              + engine.balance("Equity:RetainedEarnings"))
    assert assets + equity == ZERO


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))