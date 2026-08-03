"""Scenario 9.5 checks — state income tax and SALT flow-through. Plain asserts.

Run directly:  python tests/test_scenario9_5.py

Deliberately arithmetic-clean, and deliberately WITHOUT quarterly estimates:
one checking account with no apr, one flat pension stream, no IRA, no
CashManager. Every figure below is exact in cents, so nothing here depends on
a rounding mode.

The point of the scenario is the TIMING of the SALT deduction. State tax
accrued in December Y is paid in April Y+1 and therefore deductible on the
federal TY(Y+1) return. Year one has no prior state payment, so the standard
deduction wins; from year two on, the SALT payment tips the taxpayer into
itemizing. That switch is the whole feature.

HAND CALCULATION
----------------
Income      : 5,000.00/mo -> 60,000.00/yr, all Income:Ordinary
Federal std : 29,200.00     Other itemized: 27,000.00     SALT cap: 10,000.00
State       : flat 6.85% on income less a 16,000.00 state standard deduction

State tax, every year (income is flat):
    60,000.00 - 16,000.00 = 44,000.00 @ 6.85% = 3,014.00

TY2026  SALT paid during CY2026 = 0.00 (no prior state year to settle)
        itemized 0.00 + 27,000.00 = 27,000.00 < 29,200.00 -> STANDARD
        60,000.00 - 29,200.00 = 30,800.00 taxable
        23,200.00 @ 10% = 2,320.00 ;  7,600.00 @ 12% =   912.00 -> 3,232.00

Apr 2027  settle TY2026 federal 3,232.00 and TY2026 state 3,014.00 (cash out)
          -> state_paid[2027] = 3,014.00

TY2027  SALT paid during CY2027 = 3,014.00 (under the cap)
        itemized 3,014.00 + 27,000.00 = 30,014.00 > 29,200.00 -> ITEMIZE
        60,000.00 - 30,014.00 = 29,986.00 taxable
        23,200.00 @ 10% = 2,320.00 ;  6,786.00 @ 12% =   814.32 -> 3,134.32

Apr 2028  settle TY2027 federal 3,134.32 and TY2027 state 3,014.00
          -> state_paid[2028] = 3,014.00
TY2028  same shape as TY2027 -> federal 3,134.32, state 3,014.00, unsettled

Checking: 250,000.00 + 180,000.00
          - 3,232.00 - 3,014.00 - 3,134.32 - 3,014.00 = 417,605.68
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO
from finplan.taxengine import TaxEngine


CONTROL = {
    "start": "2026-01-01",
    "years": 3,
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
        "state": {"rate": "0.0685", "std_deduction": "16000.00"},
        "deductions": {"salt_cap": "10000.00",
                       "other_itemized": "27000.00"},
    },
}

# Same scenario with the state and deductions blocks removed: proves S9.5 is
# opt-in and that pre-S9.5 control files keep their exact cash path.
NO_STATE = {
    **CONTROL,
    "years": 1,
    "tax": {"settle_month": 4},
}


def run_three_years():
    engine, sim, years = build(CONTROL)
    sim.run(years)
    return engine, sim


def state_events(sim):
    return [e for e in sim.tax_engine.events if e.jurisdiction == "state"]


# --- the deduction choice (unit) -------------------------------------------

def test_standard_wins_when_itemized_falls_short():
    te = TaxEngine(deductions={"other_itemized": "27000.00"})
    assert te.itemized_for(2026) == money("27000.00")
    assert te.deduction_for(2026) == money("29200.00")


def test_itemized_wins_once_salt_tips_it_over():
    te = TaxEngine(deductions={"other_itemized": "27000.00"})
    te.state_paid[2027] = money("3014.00")
    assert te.deduction_for(2027) == money("30014.00")


def test_salt_is_capped():
    te = TaxEngine(deductions={"salt_cap": "10000.00",
                               "other_itemized": "27000.00"})
    te.state_paid[2027] = money("25000.00")
    # 25,000.00 clipped to 10,000.00, not carried in whole.
    assert te.itemized_for(2027) == money("37000.00")


def test_salt_is_keyed_by_payment_year_not_accrual_year():
    te = TaxEngine(deductions={"other_itemized": "27000.00"})
    te.state_paid[2027] = money("3014.00")
    # The year the liability arose sees nothing; the year the cash left does.
    assert te.deduction_for(2026) == money("29200.00")
    assert te.deduction_for(2027) == money("30014.00")


# --- state config (unit) ---------------------------------------------------

def test_flat_rate_expands_to_a_one_tier_bracket_list():
    te = TaxEngine(state={"rate": "0.0685", "std_deduction": "16000.00"})
    assert len(te.state_brackets) == 1
    # Rates are stored unquantized: money() would round 0.0685 to cents.
    assert te.state_brackets[0][1] == Decimal("0.0685")


def test_explicit_state_brackets_are_honoured():
    te = TaxEngine(state={"brackets": [["20000", "0.04"], ["1e12", "0.06"]]})
    assert [r for _, r in te.state_brackets] == [Decimal("0.04"),
                                                 Decimal("0.06")]
    assert te.state_brackets[0][0] == money("20000.00")


def test_state_gate_character_can_diverge_from_federal():
    te = TaxEngine(state={"rate": "0.05",
                          "gate_character": {"Income:SS": "exempt",
                                             "Income:CapGains:LT": "ordinary"}})
    assert te.character_of("Income:SS") == "ss"
    assert te.character_of("Income:SS", state=True) == "exempt"
    assert te.character_of("Income:CapGains:LT") == "preferential"
    assert te.character_of("Income:CapGains:LT", state=True) == "ordinary"


# --- accrual ---------------------------------------------------------------

def test_state_accrues_every_december():
    engine, sim = run_three_years()
    for y in (2026, 2027, 2028):
        assert sim.tax_engine.results[y].state_tax == money("3014.00")


def test_state_payable_sits_outside_the_federal_prepaid_prefix():
    engine, sim = run_three_years()
    # The trap this scenario was built around: a state bucket under
    # Assets:PrepaidTax:TY{y} would be swallowed by the federal settlement.
    assert engine.balance("Liabilities:StateTaxPayable:TY2028") == money("-3014.00")
    assert engine.balance("Assets:PrepaidTax:TY2028") == ZERO


# --- the timing switch -----------------------------------------------------

def test_first_year_takes_the_standard_deduction():
    engine, sim = run_three_years()
    r = sim.tax_engine.results[2026]
    assert r.deduction == money("29200.00")
    assert r.tax == money("3232.00")


def test_second_year_itemizes_because_salt_was_paid_in_april():
    engine, sim = run_three_years()
    assert sim.tax_engine.state_paid[2027] == money("3014.00")
    r = sim.tax_engine.results[2027]
    assert r.deduction == money("30014.00")
    assert r.tax == money("3134.32")


def test_salt_recurs_in_the_third_year():
    engine, sim = run_three_years()
    assert sim.tax_engine.state_paid[2028] == money("3014.00")
    assert sim.tax_engine.results[2028].deduction == money("30014.00")


def test_no_salt_credited_in_the_first_calendar_year():
    engine, sim = run_three_years()
    # Nothing was settled in April 2026: TY2025 never ran.
    assert 2026 not in sim.tax_engine.state_paid


# --- settlement ------------------------------------------------------------

def test_state_settlement_clears_the_payable_in_full():
    engine, sim = run_three_years()
    assert engine.balance("Liabilities:StateTaxPayable:TY2026") == ZERO
    assert engine.balance("Liabilities:StateTaxPayable:TY2027") == ZERO


def test_state_settlement_events_are_tagged_and_ordered_after_federal():
    engine, sim = run_three_years()
    ev = state_events(sim)
    assert [e.year for e in ev] == [2026, 2027]
    assert all(e.paid == money("3014.00") and e.prepaid == ZERO for e in ev)
    # Federal stays first for a given year, so existing index-0 lookups hold.
    both = [e for e in sim.tax_engine.events if e.year == 2026]
    assert both[0].jurisdiction == "federal"


def test_state_expense_is_swept_at_year_end():
    engine, sim = run_three_years()
    assert engine.balance("Expenses:Tax:State") == ZERO


# --- whole-ledger ----------------------------------------------------------

def test_cash_path_matches_hand_calculation():
    engine, sim = run_three_years()
    assert engine.balance("Assets:Checking") == money("417605.68")


def test_whole_ledger_sums_to_zero_after_three_years():
    engine, sim = run_three_years()
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_state_is_opt_in():
    engine, sim, years = build(NO_STATE)
    sim.run(years)
    assert sim.tax_engine.state is None
    assert state_events(sim) == []
    assert engine.balance("Liabilities:StateTaxPayable:TY2026") == ZERO
    assert engine.balance("Expenses:Tax:State") == ZERO
    # Standard deduction, unchanged from every pre-S9.5 scenario.
    assert sim.tax_engine.results[2026].deduction == money("29200.00")
    assert engine.balance("Assets:Checking") == money("310000.00")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
