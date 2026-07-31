"""Scenario 6 checks — TaxEngine: annual accrual, expense-vs-payable, per-TY
separation, spring settlement, prepaid offset, LTCG stacking, SS inclusion.

Simple override brackets make the arithmetic hand-checkable:
  ordinary: 10% to 10k, 20% to 40k, 30% above; std deduction 0
  LTCG:     0% to 50k total, 15% to 500k, 20% above

Run directly:  python tests/test_scenario6.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO

TAX = {
    "brackets": [[10000, "0.10"], [40000, "0.20"], [1000000000, "0.30"]],
    "ltcg_brackets": [[50000, "0.00"], [500000, "0.15"], [1000000000, "0.20"]],
    "std_deduction": "0.00", "ss_inclusion": "0.85", "settle_month": 4,
}


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _zero(engine):
    return sum(engine.balances().values(), ZERO) == ZERO


# --- A: ordinary accrual books an EXPENSE and a per-TY PAYABLE --------------

PENSION = {
    "start": "2026-01-01", "years": 1, "tax": TAX,
    "accounts": [{"type": "asset", "name": "Assets:Checking",
                  "opening": "200000.00"}],
    "streams": [{"name": "Pension", "to": "Assets:Checking",
                 "income": "Income:Pension", "amount": "5000.00",
                 "start": "2026-01-01"}],
}


def test_ordinary_tax_accrued_as_expense_and_payable():
    engine, sim = _run(PENSION)
    # 60k ordinary -> 1000 + 6000 + 6000 = 13000 tax.
    assert sim.tax_engine.results[2026].tax == money("13000.00")
    # Booked as an expense (swept to retained at close) + a per-TY liability
    # that rides across the boundary until next spring's settlement.
    assert engine.balance("Expenses:Tax") == ZERO                 # swept
    assert engine.balance("Liabilities:TaxPayable:TY2026") == money("-13000.00")
    # Net worth fell by the tax: retained holds -(60k income) + 13k expense.
    assert engine.balance("Equity:RetainedEarnings") == money("-47000.00")
    assert _zero(engine)


# --- B: settlement RETIRES the payable (not an expense) + two live TYs ------

def test_settlement_retires_payable_and_two_years_coexist():
    engine, sim = _run({**PENSION, "years": 2})
    # Spring 2027 settles TY2026 (no prepaid -> pay 13000 from cash); meanwhile
    # TY2027 has already been accruing all year. Both tax years coexist.
    assert engine.balance("Liabilities:TaxPayable:TY2026") == ZERO      # settled
    assert engine.balance("Liabilities:TaxPayable:TY2027") == money("-13000.00")
    assert sim.tax_engine.events[0].year == 2026
    assert sim.tax_engine.events[0].paid == money("13000.00")
    assert _zero(engine)


# --- C: IRA withholding prepaid offsets the payable -> a refund -------------

IRA = {
    "start": "2026-01-01", "years": 2, "tax": TAX,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "500000.00", "withholding": "0.20"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "20000.00",
        "waterfall": [{"source": "Assets:IRA", "floor": "0.00"}],
    },
}


def test_prepaid_withholding_offsets_and_refunds():
    engine, sim = _run(IRA)
    # Month1: pull 18750 gross -> 15000 net, 3750 withheld to PrepaidTax:TY2026.
    # TY2026 ordinary = 18750 -> tax = 1000 + 1750 = 2750.
    assert sim.tax_engine.results[2026].tax == money("2750.00")
    # Spring 2027: prepaid 3750 exceeds payable 2750 -> 1000 refund to cash.
    ev = sim.tax_engine.events[0]
    assert ev.payable == money("2750.00")
    assert ev.prepaid == money("3750.00")
    assert ev.paid == money("-1000.00")                       # negative == refund
    assert engine.balance("Assets:PrepaidTax:TY2026") == ZERO   # consumed
    assert engine.balance("Liabilities:TaxPayable:TY2026") == ZERO
    # Cash: 5000 + 15000 net pull + 1000 refund = 21000.
    assert engine.balance("Assets:Checking") == money("21000.00")
    assert _zero(engine)


# --- D: LTCG is stacked ABOVE ordinary -> preferential 15%, not 30% ---------

STACK = {
    "start": "2026-01-01", "years": 1, "tax": TAX,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "100000.00", "basis": "40000.00"},
    ],
    "streams": [{"name": "Pension", "to": "Assets:Checking",
                 "income": "Income:Pension", "amount": "5000.00",
                 "start": "2026-01-01"}],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "20000.00",
        "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}],
    },
}


def test_ltcg_stacks_on_ordinary_at_preferential_rate():
    engine, sim = _run(STACK)
    r = sim.tax_engine.results[2026]
    # Ordinary 60k -> 13000. Gain slice = 15000*(100000-40000)/100000 = 9000.
    # Gain stacks on 60k (already >50k) -> all 9000 at 15% = 1350 (not 30%).
    assert r.ordinary == money("60000.00")
    assert r.ltcg == money("9000.00")
    assert r.ordinary_tax == money("13000.00")
    assert r.ltcg_tax == money("1350.00")
    assert r.tax == money("14350.00")
    assert _zero(engine)


# --- E: SS is included at ~85%, not 100% -----------------------------------

SS = {
    "start": "2026-01-01", "years": 1, "tax": TAX,
    "accounts": [{"type": "asset", "name": "Assets:Checking",
                  "opening": "50000.00"}],
    "streams": [{"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
                 "amount": "2000.00", "start": "2026-01-01"}],
}


def test_ss_included_at_85_percent():
    engine, sim = _run(SS)
    r = sim.tax_engine.results[2026]
    # 24000 SS * 0.85 = 20400 taxable. Tax: 1000 + (20400-10000)*0.20 = 3080.
    assert r.taxable_ss == money("20400.00")
    assert r.tax == money("3080.00")
    assert _zero(engine)


# --- F: no taxable income -> no accrual, no payable -------------------------

def test_no_income_no_tax():
    engine, sim = _run({
        "start": "2026-01-01", "years": 1, "tax": TAX,
        "accounts": [{"type": "asset", "name": "Assets:Checking",
                      "opening": "100000.00"}],
    })
    assert sim.tax_engine.results[2026].tax == ZERO
    assert engine.accounts("Liabilities:") == []
    assert _zero(engine)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))