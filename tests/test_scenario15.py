"""Scenario 15 checks -- RecurringExpense: a dated transfer that repeats
every ``interval`` months, anchored at ``start``. Plain asserts.

Run directly:  python tests/test_scenario15.py

RecurringExpense is the general case of Shock's one-off transfer: same plain
transfer shape (no recognition -- an expense is not income), but gated on a
repeating schedule instead of a single date. interval=1 covers a monthly
bill (mortgage, groceries); interval=6 covers a semi-annual premium; the
anchor month need not be January, so a policy starting in March that pays
semi-annually fires March and September, not January and July.

HAND CALCULATION (nominal, monthly mortgage)
---------------------------------------------
Checking opens 100,000.00. Mortgage 2,400.00/mo, interval 1, starting
2026-01-01, run 1 year: 12 * 2,400.00 = 28,800.00 drained.
    100,000.00 - 28,800.00 = 71,200.00

HAND CALCULATION (semi-annual insurance, anchor not January)
--------------------------------------------------------------
Checking opens 20,000.00. Insurance 650.00, interval 6, starting
2026-03-01, run 1 year: fires March and September only (offset 0 and 6
months from start), NOT January/July.
    20,000.00 - 2 * 650.00 = 18,700.00

HAND CALCULATION (inflation, mode "real")
-------------------------------------------
Same shape as inflation.py / test_scenario13.py: monthly rate = annual/12,
compounded from an undiscounted factor of 1.0 at the very first period.
A 1,000.00/mo expense at 12% annual (1%/mo) over 12 months totals
12,682.51 -- identical to test_scenario13's stream total for the same
rate/period, since the escalation math is the same.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import date

from finplan.control import build
from finplan.engine import Engine
from finplan.generators import RecurringExpense
from finplan.primitives import money, ZERO
from finplan.simulation import Period


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _zero(engine):
    return sum(engine.balances().values(), ZERO) == ZERO


def _tick(engine, objects, period):
    pending = []
    for obj in objects:
        pending.extend(obj.step(period, engine))
    for txn in pending:
        engine.post(txn)
    for obj in objects:
        obj.settle_effects(period, engine)


# --- A: monthly (interval=1) fires every month -----------------------------

MORTGAGE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00"},
    ],
    "recurring_expenses": [
        {"name": "Mortgage", "from": "Assets:Checking",
         "to": "Expenses:Mortgage", "amount": "2400.00",
         "start": "2026-01-01", "interval": 1, "owner": "chuck"},
    ],
}


def test_monthly_expense_fires_every_month():
    engine, _ = _run(MORTGAGE)
    assert engine.balance("Assets:Checking") == money("71200.00")


def test_monthly_expense_is_expensed_not_income():
    engine, _ = _run(MORTGAGE)
    # Expenses:*, not Income:*, and it is swept to RetainedEarnings at close.
    assert engine.accounts("Income:") == []
    assert engine.balance("Expenses:Mortgage") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("28800.00")


def test_default_interval_is_one_when_omitted():
    control = {
        **MORTGAGE,
        "recurring_expenses": [
            {"name": "Mortgage", "from": "Assets:Checking",
             "to": "Expenses:Mortgage", "amount": "2400.00",
             "start": "2026-01-01"},   # no "interval" key at all
        ],
    }
    engine, _ = _run(control)
    assert engine.balance("Assets:Checking") == money("71200.00")


def test_whole_ledger_zero_with_monthly_expense():
    engine, _ = _run(MORTGAGE)
    assert _zero(engine)


def test_owner_rides_through_attrs_onto_postings():
    engine, _ = _run(MORTGAGE)
    postings = [p for txn in engine.journal for p in txn.postings
                if p.meta.get("recurring") == "Mortgage"]
    assert postings, "expected Mortgage postings in the journal"
    assert all(p.owner == "chuck" for p in postings)


# --- B: semi-annual (interval=6), anchor NOT January -----------------------

INSURANCE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "20000.00"},
    ],
    "recurring_expenses": [
        {"name": "InsCars", "from": "Assets:Checking",
         "to": "Expenses:Insurance:Cars", "amount": "650.00",
         "start": "2026-03-01", "interval": 6},
    ],
}


def test_semiannual_fires_twice_a_year_from_its_anchor():
    engine, _ = _run(INSURANCE)
    assert engine.balance("Assets:Checking") == money("18700.00")


def test_semiannual_fires_in_march_and_september_not_jan_jul():
    engine, _ = _run(INSURANCE)
    fired = sorted(
        (t.date.year, t.date.month) for t in engine.journal
        if t.meta.get("recurring") == "InsCars"
        or any(p.meta.get("recurring") == "InsCars" for p in t.postings)
    )
    assert fired == [(2026, 3), (2026, 9)]


# --- C: start/end gate the window, month-granular ---------------------------

def test_start_date_gates_months_before_it():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "10000.00"},
        ],
        "recurring_expenses": [
            {"name": "Grocery", "from": "Assets:Checking",
             "to": "Expenses:Grocery:Costco", "amount": "300.00",
             "start": "2026-07-01", "interval": 1},
        ],
    }
    engine, _ = _run(control)
    # Jul..Dec = 6 firings = 1800.00.
    assert engine.balance("Assets:Checking") == money("8200.00")


def test_start_is_month_granular_not_day():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "10000.00"},
        ],
        "recurring_expenses": [
            {"name": "Grocery", "from": "Assets:Checking",
             "to": "Expenses:Grocery:Costco", "amount": "300.00",
             "start": "2026-07-15", "interval": 1},
        ],
    }
    engine, _ = _run(control)
    # Mid-month start still fires that whole month -> same as 2026-07-01.
    assert engine.balance("Assets:Checking") == money("8200.00")


def test_end_date_gates_months_after_it():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "10000.00"},
        ],
        "recurring_expenses": [
            {"name": "Grocery", "from": "Assets:Checking",
             "to": "Expenses:Grocery:Costco", "amount": "300.00",
             "start": "2026-01-01", "end": "2026-03-01", "interval": 1},
        ],
    }
    engine, _ = _run(control)
    # Jan..Mar = 3 firings = 900.00.
    assert engine.balance("Assets:Checking") == money("9100.00")


# --- D: multiple accounts of the same kind, independently reportable -------

MULTI_SAME_KIND = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "50000.00"},
    ],
    "recurring_expenses": [
        {"name": "InsCars", "from": "Assets:Checking",
         "to": "Expenses:Insurance:Cars", "amount": "650.00",
         "start": "2026-03-01", "interval": 6},
        {"name": "InsHealth", "from": "Assets:Checking",
         "to": "Expenses:Insurance:Health", "amount": "480.00",
         "start": "2026-01-01", "interval": 1},
        {"name": "GroceryCostco", "from": "Assets:Checking",
         "to": "Expenses:Grocery:Costco", "amount": "300.00",
         "start": "2026-01-01", "interval": 1},
        {"name": "GroceryTraderJoes", "from": "Assets:Checking",
         "to": "Expenses:Grocery:TraderJoes", "amount": "150.00",
         "start": "2026-01-01", "interval": 1},
        {"name": "CCAmazon", "from": "Assets:Checking",
         "to": "Expenses:CreditCard:Amazon", "amount": "150.00",
         "start": "2026-01-01", "interval": 1},
    ],
}


def test_prefix_rollup_sums_multiple_accounts_of_the_same_kind():
    engine, _ = _run(MULTI_SAME_KIND)
    # No new mechanism needed: Engine.accounts(prefix) already rolls these
    # up by naming convention alone. Grocery: two sources, 300+150=450/mo.
    grocery_leaves = engine.accounts("Expenses:Grocery:")
    assert grocery_leaves == ["Expenses:Grocery:Costco",
                              "Expenses:Grocery:TraderJoes"]
    # Everything is swept to zero at year-end close regardless of depth.
    assert engine.balance("Expenses:Grocery:Costco") == ZERO
    assert engine.balance("Expenses:Grocery:TraderJoes") == ZERO


def test_retained_earnings_reflects_every_leaf_combined():
    engine, _ = _run(MULTI_SAME_KIND)
    # InsCars: 2 * 650 = 1300; InsHealth: 12 * 480 = 5760;
    # Grocery: 12 * (300+150) = 5400; CCAmazon: 12 * 150 = 1800.
    total = (money("1300.00") + money("5760.00")
             + money("5400.00") + money("1800.00"))
    assert engine.balance("Equity:RetainedEarnings") == total
    assert _zero(engine)


# --- E: inflation escalation (mirrors Stream/Shock/Schedule.fixed) ---------

def test_nominal_mode_does_not_escalate():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "0"},
            {"type": "asset", "name": "Assets:Savings", "opening": "1000000.00"},
        ],
        "recurring_expenses": [
            {"name": "Bill", "from": "Assets:Savings", "to": "Expenses:Bill",
             "amount": "1000.00", "start": "2026-01-01", "interval": 1},
        ],
    }
    engine, _ = _run(control)
    assert engine.balance("Expenses:Bill") == ZERO   # swept
    # Flat 1000/mo x 12, no compounding.
    assert engine.balance("Assets:Savings") == money("988000.00")


def test_real_mode_escalates_monthly_expense():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "0"},
            {"type": "asset", "name": "Assets:Savings", "opening": "1000000.00"},
        ],
        "recurring_expenses": [
            {"name": "Bill", "from": "Assets:Savings", "to": "Expenses:Bill",
             "amount": "1000.00", "start": "2026-01-01", "interval": 1},
        ],
        "inflation": {"mode": "real", "rates": {"2026": "0.12"}},
    }
    engine, _ = _run(control)
    # Same compounding math as test_scenario13's salary stream: 12,682.51
    # drained over 12 months at 12%/yr -> 1%/mo, undiscounted first period.
    drained = money("1000000.00") - engine.balance("Assets:Savings")
    assert drained == money("12682.51")


def test_real_mode_escalates_semiannual_amounts_over_time():
    control = {
        "start": "2026-01-01", "years": 2,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "0"},
            {"type": "asset", "name": "Assets:Savings", "opening": "1000000.00"},
        ],
        "recurring_expenses": [
            {"name": "InsCars", "from": "Assets:Savings",
             "to": "Expenses:Insurance:Cars", "amount": "650.00",
             "start": "2026-03-01", "interval": 6},
        ],
        "inflation": {"mode": "real", "rates": {"2026": "0.12"}},
    }
    engine, sim = _run(control)
    fired = sorted(
        (t for t in engine.journal
         if t.meta.get("recurring") == "InsCars"
         or any(p.meta.get("recurring") == "InsCars" for p in t.postings)),
        key=lambda t: t.date,
    )
    assert len(fired) == 4   # Mar/Sep 2026, Mar/Sep 2027

    def amt(txn):
        return next(p.amount for p in txn.postings
                   if p.account == "Expenses:Insurance:Cars")

    amounts = [amt(t) for t in fired]
    # Each later firing is escalated further than the one before it; the
    # inflation factor is monotonically increasing across the run.
    assert amounts == sorted(amounts)
    assert amounts[-1] > amounts[0]
    assert all(a >= money("650.00") for a in amounts)


# --- F: zero amount and interval validation --------------------------------

def test_zero_amount_is_a_noop():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
        ],
        "recurring_expenses": [
            {"name": "Nothing", "from": "Assets:Checking",
             "to": "Expenses:Nothing", "amount": "0.00",
             "start": "2026-01-01", "interval": 1},
        ],
    }
    engine, _ = _run(control)
    assert engine.balance("Assets:Checking") == money("5000.00")
    assert "Expenses:Nothing" not in engine.accounts()


def test_interval_below_one_is_rejected():
    try:
        RecurringExpense(name="Bad", frm="Assets:Checking", to="Expenses:Bad",
                         amount="100.00", start=date(2026, 1, 1), interval=0)
    except ValueError as e:
        assert "interval" in str(e)
        return
    raise AssertionError("expected ValueError for interval < 1")


# --- G: order independence (mirrors the snapshot-accrue guarantee) ---------

def test_order_independence_with_other_generators():
    from finplan.primitives import Posting, Transaction

    def build_and_run(reversed_order):
        engine = Engine()
        exp = RecurringExpense(name="Bill", frm="Assets:Checking",
                               to="Expenses:Bill", amount="500.00",
                               start=date(2026, 1, 1), interval=1)
        engine.post(Transaction(
            date=date(2026, 1, 1), description="Opening balances",
            postings=[Posting("Assets:Checking", money("10000.00"),
                              meta={"kind": "opening"}),
                     Posting("Equity:Opening", money("-10000.00"),
                             meta={"kind": "opening"})],
        ))
        objs = [exp] if not reversed_order else list(reversed([exp]))
        period = Period(date=date(2026, 1, 1), year=2026, month=1)
        _tick(engine, objs, period)
        return engine.balance("Assets:Checking")

    assert build_and_run(False) == build_and_run(True)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
