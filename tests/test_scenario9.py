"""Scenario 9 checks — quarterly estimated payments. Plain asserts.

Run directly:  python tests/test_scenario9.py

The scenario is deliberately arithmetic-clean: one checking account with no
apr (so no InterestPolicy), one flat monthly pension stream, no IRA and no
CashManager. That keeps withholding out of the picture so the safe-harbor
netting can be driven from the SEED and checked against hand arithmetic.
Withholding-sourced estimates need the IRA/CashManager path and belong in a
follow-on test once accounts.py is in scope.

HAND CALCULATION
----------------
Income  : 5,000.00/mo -> 60,000.00/yr, all Income:Ordinary
TY2026  : 60,000.00 - 29,200.00 std = 30,800.00 taxable
          23,200.00 @ 10% = 2,320.00
           7,600.00 @ 12% =   912.00   -> tax 3,232.00
Estimates for TY2026 (basis = seed 12,000.00 tax / 4,000.00 withheld):
          12,000.00 * 1.10 = 13,200.00 - 4,000.00 = 9,200.00 / 4 = 2,300.00
          Q1 Apr-26, Q2 Jun-26, Q3 Sep-26, Q4 Jan-27
Estimates for TY2027 (basis = TY2026: tax 3,232.00, withheld 0.00):
           3,232.00 * 1.10 =  3,555.20 - 0.00     = 3,555.20 / 4 =   888.80
          Q1 Apr-27, Q2 Jun-27, Q3 Sep-27; Q4 would be Jan-28, outside the run
Settle TY2026 (Apr-27): payable 3,232.00 vs prepaid 9,200.00 -> refund 5,968.00
Checking: 250,000 + 120,000 - 6,900 - 2,300 + 5,968 - 2,666.40 = 364,101.60
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO
from finplan.taxengine import TaxEngine


CONTROL = {
    "start": "2026-01-01",
    "years": 2,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "250000.00"},
    ],
    "streams": [
        {"name": "pension", "to": "Assets:Checking",
         "income": "Income:Ordinary", "amount": "5000.00", "owner": "chuck"},
    ],
    "tax": {
        "settle_month": 4,
        "estimates": {
            "safe_harbor_multiple": "1.10",
            "credit_withholding": True,
            "prior_year_tax": "12000.00",
            "prior_year_withholding": "4000.00",
        },
    },
}

# Same scenario with the estimates block removed: proves the feature is
# opt-in and that pre-S9 control files keep their exact cash path.
NO_ESTIMATES = {
    **CONTROL,
    "years": 1,
    "tax": {"settle_month": 4},
}


def run_two_years():
    engine, sim, years = build(CONTROL)
    sim.run(years)
    return engine, sim


def estimates_for(sim, year):
    return [e for e in sim.tax_engine.estimate_events if e.year == year]


# --- sizing rule (unit) ----------------------------------------------------

def test_quarterly_amount_nets_prior_year_withholding():
    te = TaxEngine(estimates=True, safe_harbor_multiple="1.10",
                   prior_year_tax="12000.00",
                   prior_year_withholding="4000.00")
    # (12000 * 1.10 - 4000) / 4
    assert te.quarterly_amount(2026) == money("2300.00")


def test_quarterly_amount_gross_when_not_crediting_withholding():
    te = TaxEngine(estimates=True, safe_harbor_multiple="1.10",
                   credit_withholding=False, prior_year_tax="12000.00",
                   prior_year_withholding="4000.00")
    # withholding ignored: 12000 * 1.10 / 4
    assert te.quarterly_amount(2026) == money("3300.00")


def test_quarterly_amount_floors_at_zero_when_withholding_covers_target():
    te = TaxEngine(estimates=True, safe_harbor_multiple="1.10",
                   prior_year_tax="1000.00",
                   prior_year_withholding="5000.00")
    # Fully withheld taxpayer owes no estimates; must not go negative and
    # quietly hand cash BACK from the prepaid bucket.
    assert te.quarterly_amount(2026) == ZERO


def test_quarterly_amount_prefers_accrued_result_over_seed():
    engine, sim = run_two_years()
    te = sim.tax_engine
    assert te.results[2026].tax == money("3232.00")
    assert te.results[2026].withheld == ZERO
    # 3232.00 * 1.10 / 4, seed ignored once a real prior year exists
    assert te.quarterly_amount(2027) == money("888.80")


# --- calendar --------------------------------------------------------------

def test_first_year_estimates_use_the_seed():
    engine, sim = run_two_years()
    ev = estimates_for(sim, 2026)
    assert len(ev) == 4
    assert all(e.amount == money("2300.00") for e in ev)
    assert [e.quarter for e in ev] == [1, 2, 3, 4]


def test_q1_q3_fall_in_april_june_september():
    engine, sim = run_two_years()
    ev = {e.quarter: e for e in estimates_for(sim, 2026)}
    assert (ev[1].date.year, ev[1].date.month) == (2026, 4)
    assert (ev[2].date.year, ev[2].date.month) == (2026, 6)
    assert (ev[3].date.year, ev[3].date.month) == (2026, 9)


def test_q4_cash_lands_in_january_of_the_following_year():
    engine, sim = run_two_years()
    q4 = {e.quarter: e for e in estimates_for(sim, 2026)}[4]
    # Cash date and tax year deliberately come apart.
    assert (q4.date.year, q4.date.month) == (2027, 1)
    assert q4.year == 2026


def test_no_phantom_q4_for_a_tax_year_predating_the_run():
    engine, sim = run_two_years()
    assert estimates_for(sim, 2025) == []
    assert engine.balance("Assets:PrepaidTax:TY2025:Estimated") == ZERO


def test_terminal_year_stops_at_q3():
    engine, sim = run_two_years()
    ev = estimates_for(sim, 2027)
    # Q4 would be January 2028, past the end of the run.
    assert [e.quarter for e in ev] == [1, 2, 3]
    assert all(e.amount == money("888.80") for e in ev)


# --- settlement ------------------------------------------------------------

def test_settlement_consumes_the_estimated_sub_account():
    engine, sim = run_two_years()
    assert engine.balance("Assets:PrepaidTax:TY2026:Estimated") == ZERO
    assert engine.balance("Assets:PrepaidTax:TY2026") == ZERO
    assert engine.balance("Liabilities:TaxPayable:TY2026") == ZERO


def test_settlement_event_splits_prepaid_by_kind():
    engine, sim = run_two_years()
    e = [x for x in sim.tax_engine.events if x.year == 2026][0]
    assert e.payable == money("3232.00")
    assert e.estimated == money("9200.00")     # 4 x 2300.00
    assert e.withheld == ZERO                   # no IRA in this scenario
    assert e.prepaid == money("9200.00")
    assert e.paid == money("-5968.00")          # negative == refund


def test_unsettled_year_still_holds_its_estimates():
    engine, sim = run_two_years()
    # TY2027 settles in April 2028, outside the run: 3 quarters still parked.
    assert engine.balance("Assets:PrepaidTax:TY2027:Estimated") == money("2666.40")


# --- whole-ledger ----------------------------------------------------------

def test_cash_path_matches_hand_calculation():
    engine, sim = run_two_years()
    assert engine.balance("Assets:Checking") == money("364101.60")


def test_whole_ledger_sums_to_zero_after_two_years():
    engine, sim = run_two_years()
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_estimates_are_opt_in():
    engine, sim, years = build(NO_ESTIMATES)
    sim.run(years)
    assert sim.tax_engine.estimate_events == []
    assert engine.balance("Assets:PrepaidTax:TY2026:Estimated") == ZERO
    # No estimate outflow: opening + 12 months of pension, untouched.
    assert engine.balance("Assets:Checking") == money("310000.00")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
