"""Scenario 2 checks — Stream income (SS / annuity) + snapshot accrue.

Run directly:  python tests/test_scenario2.py
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build, build_scenario
from finplan.primitives import money, ZERO
from finplan.simulation import Period


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine


# --- basic Stream behaviour ------------------------------------------------

# No interest here, so checking growth is exactly the sum of stream deposits.
SS_ONLY = {
    "start": "2026-01-01",
    "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00"},
    ],
    "streams": [
        {"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "3000.00", "start": "2026-01-01", "owner": "chuck"},
    ],
}


def test_stream_deposits_twelve_months():
    engine = _run(SS_ONLY)
    # 12 * 3000 = 36000 of external cash landed in checking.
    assert engine.balance("Assets:Checking") == money("136000.00")


def test_income_gate_swept_and_named_distinctly():
    engine = _run(SS_ONLY)
    # The gate is Income:SS (not Income:Interest) so tax rules can find it,
    # and it is swept to zero at year-end close like every nominal account.
    assert "Income:SS" in engine.accounts("Income:")
    assert engine.balance("Income:SS") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("-36000.00")


def test_whole_ledger_zero_with_stream():
    engine = _run(SS_ONLY)
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_owner_rides_through_attrs_onto_postings():
    # owner is cross-cutting metadata in attrs (not a constructor field); it
    # must still land on every posting the stream emits, tagged 'chuck'.
    engine = _run(SS_ONLY)
    ss_postings = [p for txn in engine.journal for p in txn.postings
                   if p.meta.get("stream") == "SS"]
    assert ss_postings, "expected SS postings in the journal"
    assert all(p.owner == "chuck" for p in ss_postings)


# --- start / end gating (month-granular) -----------------------------------

def test_start_date_gates_months():
    control = {**SS_ONLY, "streams": [
        {"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "3000.00", "start": "2026-07-01", "owner": "chuck"},
    ]}
    engine = _run(control)
    # Jul..Dec = 6 deposits = 18000.
    assert engine.balance("Assets:Checking") == money("118000.00")


def test_start_is_month_granular_not_day():
    # Mid-month start still fires that whole month: day is not modeled.
    control = {**SS_ONLY, "streams": [
        {"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "3000.00", "start": "2026-07-15", "owner": "chuck"},
    ]}
    engine = _run(control)
    # July still counts -> 6 deposits, same as a 2026-07-01 start.
    assert engine.balance("Assets:Checking") == money("118000.00")


def test_end_date_gates_months():
    control = {**SS_ONLY, "streams": [
        {"name": "Annuity", "to": "Assets:Checking", "income": "Income:Annuity",
         "amount": "3000.00", "start": "2026-01-01", "end": "2026-03-01"},
    ]}
    engine = _run(control)
    # Jan..Mar = 3 deposits = 9000.
    assert engine.balance("Assets:Checking") == money("109000.00")


# --- the snapshot decision -------------------------------------------------

SS_PLUS_INTEREST = {
    "start": "2026-01-01",
    "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00", "apr": "0.03"},
    ],
    "streams": [
        {"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "3000.00", "start": "2026-01-01", "owner": "chuck"},
    ],
}


def test_snapshot_interest_ignores_same_tick_deposit():
    # Run exactly one month by hand and inspect the first tick.
    engine, sim, _ = build(SS_PLUS_INTEREST)
    sim._accrue(Period(date=dt.date(2026, 1, 1), year=2026, month=1))

    # Interest is computed on the TICK-OPEN balance (100000), NOT on
    # 103000 (post-SS). 100000 * 0.03/12 = 250.00 exactly.
    #   snapshot   -> Income:Interest == -250.00, checking == 103250.00
    #   sequential -> would be -257.50 / 103257.50 if SS posted first
    assert engine.balance("Income:Interest") == money("-250.00")
    assert engine.balance("Assets:Checking") == money("103250.00")


def test_accrue_is_order_independent():
    # The whole point of snapshot: reversing object order changes nothing.
    e1, s1, y1 = build(SS_PLUS_INTEREST)
    s1.run(y1)

    e2, s2, y2 = build(SS_PLUS_INTEREST)
    s2.objects.reverse()
    s2.run(y2)

    assert e1.balances() == e2.balances()


# --- stream inheritance rides on the scenario merge (by name) --------------

STREAM_ROOT = {
    "scenarios": {
        "base": SS_ONLY,
        "bigger_ss": {
            "extends": "base",
            "streams": [{"name": "SS", "amount": "4000.00"}],
        },
        "no_ss": {
            "extends": "base",
            "streams": [{"name": "SS", "_remove": True}],
        },
    }
}


def test_stream_override_by_name():
    engine, sim, years = build_scenario(STREAM_ROOT, "bigger_ss")
    sim.run(years)
    # amount retuned to 4000 by name; other fields inherited. 12 * 4000.
    assert engine.balance("Assets:Checking") == money("148000.00")


def test_stream_removed_by_sentinel():
    engine, sim, years = build_scenario(STREAM_ROOT, "no_ss")
    sim.run(years)
    # SS dropped -> no deposits, only the opening balance remains.
    assert engine.balance("Assets:Checking") == money("100000.00")
    assert "Income:SS" not in engine.accounts("Income:")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))