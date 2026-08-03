"""Scenario 10 checks — HELOC-centered processing. Plain asserts.

Run directly:  python tests/test_scenario10.py

A HELOC is the break-glass rung: the waterfall reaches it when depletion is
at hand and the other sources cannot meet the cash need. Drawing on it is
BORROWING, not liquidating, so it is the simplest fund_from shape in the
model — cash up, liability down, no recognition pair, because borrowed money
is not income.

Rate is 12% APR throughout = exactly 1.00% per month, so every figure below
is hand-checkable.

HAND CALCULATION (capitalizing scenario)
----------------------------------------
Checking opens 30,000.00; origination fee 1,000.00 charged Jan 2026 -> 29,000
Living expense 25,000.00 every June. Floor 10,000.00, refill target 25,000.00.

Jun 2026  accrue: 29,000.00 - 25,000.00 =  4,000.00 (below floor)
          fund  : need 25,000.00 - 4,000.00 = 21,000.00 -> DRAW 21,000.00
Jul-Dec 2026  interest capitalizes at 1%/mo on the tick-open balance:
          21,000.00 -> 21,210.00 -> 21,422.10 -> 21,636.32 -> 21,852.68
                    -> 22,071.21 -> 22,291.92
Jun 2027  accrue: 25,000.00 - 25,000.00 = 0.00
          fund  : need 25,000.00 -> DRAW 25,000.00 (on top of the accrued
                  balance, which keeps compounding underneath)
Dec 2027  owed 51,657.09  (46,000.00 drawn + 5,657.09 capitalized interest)

BALLOON
-------
Same shape, maturity Jan 2027, one 55,000.00 expense in Jun 2026 only:
  draw 20,000.00 -> six months of 1% -> 21,230.40 at Dec 2026
  Jan 2027: interest 212.30 accrues AND the balloon pays 21,442.70
            (tick-open balance + that same tick's charge) -> exactly 0.00
  Cash 25,000.00 - 21,442.70 = 3,557.30, which is below the 10,000.00 floor
  and the line has matured, so no source is eligible -> HARD severity.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.cashmanager import HARD, SOFT
from finplan.control import build
from finplan.primitives import money, ZERO
from finplan.taxengine import DEDUCTIBLE_INTEREST


CASH_MGMT = {
    "account": "Assets:Checking",
    "floor": "10000.00",
    "target": "25000.00",
    "waterfall": [{"source": "Liabilities:HELOC", "floor": "0"}],
}


def _control(heloc_extra=None, opening="30000.00", expense="25000.00",
             years=2, expense_once=False, **top):
    heloc = {
        "type": "heloc", "name": "Liabilities:HELOC", "owner": "chuck",
        "credit_limit": "100000.00", "rate": "0.12", "opened": "2026-01-01",
    }
    heloc.update(heloc_extra or {})
    sched = {
        "name": "living", "source": "Assets:Checking", "to": "Expenses:Living",
        "mode": "fixed", "amount": expense, "month": 6,
    }
    if expense_once:
        sched.update({"start": "2026-06-01", "end": "2026-06-30"})
    control = {
        "start": "2026-01-01", "years": years,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": opening},
            heloc,
        ],
        "cash_management": CASH_MGMT,
        "schedules": [sched],
    }
    control.update(top)
    return control


def run(**kw):
    engine, sim, years = build(_control(**kw))
    sim.run(years)
    return engine, sim


def capitalizing():
    return run(heloc_extra={"origination_fee": "1000.00"})


# --- the draw is borrowing, not liquidation --------------------------------

def test_draw_posts_no_recognition_and_no_income():
    engine, sim = capitalizing()
    # Borrowed money is not income: nothing reached an Income gate, and the
    # Equity:Recognized contra was never needed.
    assert engine.accounts("Income:") == []
    assert engine.balance("Equity:Recognized") == ZERO


def test_draw_delivers_gross_with_no_withholding():
    engine, sim = capitalizing()
    wd = sim.cash_manager.events[0].pulls[0]
    assert wd.gross == money("21000.00")
    assert wd.net == wd.gross
    assert wd.withheld == ZERO
    assert wd.gain == ZERO
    assert wd.character is None


def test_waterfall_refills_to_target_from_the_line():
    engine, sim = capitalizing()
    ev = sim.cash_manager.events
    assert [e.date.year for e in ev] == [2026, 2027]
    assert ev[0].cash_before == money("4000.00")
    assert ev[0].cash_after == money("25000.00")
    assert all(e.severity == SOFT for e in ev)


# --- interest capitalizes --------------------------------------------------

def test_interest_capitalizes_at_one_percent_a_month():
    engine, sim = capitalizing()
    # 46,000.00 drawn + 5,657.09 of capitalized interest.
    assert engine.balance("Liabilities:HELOC") == money("-51657.09")


def test_capitalized_interest_is_expensed_not_invented():
    engine, sim = capitalizing()
    # Expenses:* is swept to RetainedEarnings each December, so the running
    # balance is zero at year end; the ledger still has to close.
    assert engine.balance("Expenses:Interest:HELOC") == ZERO
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_origination_fee_is_a_plain_expense_on_the_open_date():
    engine, sim = capitalizing()
    fee = [t for t in engine.journal if "fee:" in t.description]
    assert len(fee) == 1
    assert (fee[0].date.year, fee[0].date.month) == (2026, 1)
    # Not deductible interest — a separate gate the TaxEngine ignores.
    assert any(p.account == "Expenses:LoanFees" and p.amount == money("1000.00")
               for p in fee[0].postings)


# --- payment modes ---------------------------------------------------------

def test_interest_only_holds_the_balance_exactly_flat():
    engine, sim = run(heloc_extra={"payment": {"mode": "interest_only"}},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    # Payment and interest read the SAME tick-open balance, so they cancel to
    # the cent with no ordering hack.
    assert engine.balance("Liabilities:HELOC") == money("-20000.00")


def test_capitalizing_is_the_default_when_no_payment_block_is_given():
    engine, sim = run(opening="60000.00", expense="55000.00",
                      expense_once=True)
    # A default that quietly held the balance flat would flatter the plan.
    # 20,000.00 drawn Jun 2026, then 18 months of capitalization to Dec 2027.
    assert engine.balance("Liabilities:HELOC") == money("-23922.93")


def test_fixed_payment_amortizes_through_the_balance_alone():
    engine, sim = run(heloc_extra={"payment": {"mode": "fixed",
                                               "amount": "500.00"}},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    # 20,000.00 drawn Jun 2026, then 18 months of (1% interest - 500.00).
    owed = money("20000.00")
    for _ in range(18):
        owed = money(owed + money(owed * money("0.01")) - money("500.00"))
    assert engine.balance("Liabilities:HELOC") == money(-owed)


# --- maturity balloon ------------------------------------------------------

def test_balloon_clears_the_line_to_exactly_zero_in_one_month():
    engine, sim = run(heloc_extra={"maturity": "2027-01-01"},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    assert engine.balance("Liabilities:HELOC") == ZERO


def test_balloon_pays_tick_open_balance_plus_that_ticks_interest():
    engine, sim = run(heloc_extra={"maturity": "2027-01-01"},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    payoff = [t for t in engine.journal if "Balloon payoff" in t.description]
    assert len(payoff) == 1
    # 21,230.40 tick-open + 212.30 charged the same tick.
    assert any(p.amount == money("21442.70") for p in payoff[0].postings)


def test_matured_line_is_no_longer_an_eligible_source():
    engine, sim = run(heloc_extra={"maturity": "2027-01-01"},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    # The balloon drops cash below the floor, but the line has closed, so the
    # waterfall runs dry: a plan-failure signal, not a silent redraw.
    hard = [e for e in sim.cash_manager.events if e.severity == HARD]
    assert hard, "expected a hard reserve-exhausted month after the balloon"
    assert hard[0].pulls == []
    assert engine.balance("Assets:Checking") == money("3557.30")


def test_unopened_line_cannot_be_drawn():
    engine, sim = run(heloc_extra={"opened": "2027-01-01"},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    # Every 2026 month breaches the floor with nothing to draw on; the first
    # pull is not until the line opens in January 2027.
    ev = sim.cash_manager.events
    assert all(e.pulls == [] and e.severity == HARD
               for e in ev if e.date.year == 2026)
    first_pull = next(e for e in ev if e.pulls)
    assert (first_pull.date.year, first_pull.date.month) == (2027, 1)
    # Drawn Jan 2027, then 11 months of capitalization to Dec 2027.
    assert engine.balance("Liabilities:HELOC") == money("-22313.36")


# --- credit limit ----------------------------------------------------------

def test_capacity_is_limit_minus_owed_not_balance_to_zero():
    engine, sim = run(heloc_extra={"credit_limit": "12000.00"},
                      opening="60000.00", expense="55000.00",
                      expense_once=True)
    # Wanted 20,000.00, limit allows 12,000.00: partial fill.
    ev = sim.cash_manager.events[0]
    assert ev.pulls[0].gross == money("12000.00")
    # 5,000.00 + 12,000.00 = 17,000.00 clears the 10,000.00 FLOOR but misses
    # the 25,000.00 target. Severity keys off the floor, so this is soft; the
    # unmet target shows up as shortfall_remaining instead.
    assert ev.cash_after == money("17000.00")
    assert ev.severity == SOFT
    assert ev.shortfall_remaining == money("8000.00")


def test_a_source_floor_reserves_borrowing_headroom():
    control = _control(opening="60000.00", expense="55000.00",
                       expense_once=True)
    control["cash_management"] = {
        **CASH_MGMT,
        "waterfall": [{"source": "Liabilities:HELOC", "floor": "85000.00"}],
    }
    engine, sim, years = build(control)
    sim.run(years)
    # 100,000.00 limit less an 85,000.00 reserve = 15,000.00 drawable.
    assert sim.cash_manager.events[0].pulls[0].gross == money("15000.00")


# --- deductibility routing -------------------------------------------------

def test_deductible_interest_routes_to_the_gate_the_taxengine_reads():
    engine, sim = run(
        heloc_extra={"deductible": True},
        opening="60000.00", expense="85000.00", expense_once=True,
        streams=[{"name": "pension", "to": "Assets:Checking",
                  "income": "Income:Ordinary", "amount": "5000.00",
                  "owner": "chuck"}],
        tax={"settle_month": 4,
             "deductions": {"other_itemized": "28500.00"}},
    )
    # Jul-Dec 2026 interest on a 20,000.00 draw = 1,230.40, which tips
    # 28,500.00 of other itemized deductions past the 29,200.00 standard.
    assert sim.tax_engine.results[2026].deduction == money("29730.40")


def test_non_deductible_interest_never_reaches_the_return():
    engine, sim = run(
        opening="60000.00", expense="85000.00", expense_once=True,
        streams=[{"name": "pension", "to": "Assets:Checking",
                  "income": "Income:Ordinary", "amount": "5000.00",
                  "owner": "chuck"}],
        tax={"settle_month": 4,
             "deductions": {"other_itemized": "28500.00"}},
    )
    # Same interest, same amount, but the account is not flagged deductible:
    # it lands on a gate the TaxEngine does not read, so the standard
    # deduction still wins.
    assert engine.balance(DEDUCTIBLE_INTEREST) == ZERO
    assert sim.tax_engine.results[2026].deduction == money("29200.00")


# --- whole-ledger ----------------------------------------------------------

def test_whole_ledger_sums_to_zero_in_every_variant():
    for engine, _ in (capitalizing(),
                      run(heloc_extra={"maturity": "2027-01-01"},
                          opening="60000.00", expense="55000.00",
                          expense_once=True),
                      run(heloc_extra={"payment": {"mode": "interest_only"}},
                          opening="60000.00", expense="55000.00",
                          expense_once=True)):
        assert sum(engine.balances().values(), ZERO) == ZERO


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
