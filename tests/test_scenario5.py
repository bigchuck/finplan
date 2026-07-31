"""Scenario 5 checks — Shock: one-off dated events, and the mid-run breach
they create for the CashManager.

Run directly:  python tests/test_scenario5.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.cashmanager import SOFT
from finplan.primitives import money, ZERO


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _zero(engine):
    return sum(engine.balances().values(), ZERO) == ZERO


# --- A: spending shock drains cash, hits an Expense gate, lowers net worth --

SPEND = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "100000.00"},
    ],
    "shocks": [
        {"name": "NewRoof", "from": "Assets:Checking", "to": "Expenses:Home",
         "amount": "25000.00", "when": "2026-06-01"},
    ],
}


def test_spending_shock_drains_and_sweeps():
    engine, _ = _run(SPEND)
    assert engine.balance("Assets:Checking") == money("75000.00")
    # Expense gate swept to RetainedEarnings at close: net worth fell by 25000
    # (RetainedEarnings positive == net worth down, in this sign convention).
    assert engine.balance("Expenses:Home") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("25000.00")
    assert _zero(engine)


# --- B: windfall shock lifts cash via an Income gate, raises net worth -------

WINDFALL = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "100000.00"},
    ],
    "shocks": [
        {"name": "Inheritance", "from": "Income:Inheritance",
         "to": "Assets:Checking", "amount": "50000.00", "when": "2026-03-01"},
    ],
}


def test_windfall_shock_lifts_networth():
    engine, _ = _run(WINDFALL)
    assert engine.balance("Assets:Checking") == money("150000.00")
    assert engine.balance("Income:Inheritance") == ZERO           # swept
    assert engine.balance("Equity:RetainedEarnings") == money("-50000.00")
    assert _zero(engine)


# --- C: fires exactly once, month-granular (day ignored) --------------------

ONCE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "100000.00"},
    ],
    "shocks": [
        {"name": "Car", "from": "Assets:Checking", "to": "Expenses:Auto",
         "amount": "10000.00", "when": "2026-07-15"},
    ],
}


def test_shock_fires_once_month_granular():
    engine, _ = _run(ONCE)
    # A mid-month 'when' still fires that whole month, exactly once: 100k - 10k.
    assert engine.balance("Assets:Checking") == money("90000.00")
    fired = [t for t in engine.journal if t.meta.get("shock") == "Car"
             or any(p.meta.get("shock") == "Car" for p in t.postings)]
    assert len(fired) == 1


# --- D: a cash-draining shock triggers the CashManager mid-run --------------

BREACH = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "30000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "200000.00", "basis": "200000.00"},
    ],
    "shocks": [
        {"name": "Medical", "from": "Assets:Checking", "to": "Expenses:Medical",
         "amount": "25000.00", "when": "2026-06-01"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "40000.00",
        "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}],
    },
}


def test_shock_breaches_floor_midrun_and_cashmanager_refills():
    engine, sim = _run(BREACH)
    ev = sim.cash_manager.events
    # No breach until June: the shock drains 30000 -> 5000, then funding fires
    # exactly once, in June, and refills to target from the brokerage.
    assert len(ev) == 1
    assert ev[0].date.month == 6
    assert ev[0].severity == SOFT
    assert engine.balance("Assets:Checking") == money("40000.00")
    assert engine.balance("Assets:Brokerage") == money("165000.00")
    assert _zero(engine)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))