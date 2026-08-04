"""Scenario 12 checks — tax law changes. Plain asserts.

Run directly:  python tests/test_scenario12.py

A TaxLawChange is dated to a TAX YEAR and applied at the January of that
year — before that year's December accrual, the same lead time
Estate._change_filing_status uses for a filing-status flip. Each field
(brackets, ltcg_brackets, std_deduction, ss_inclusion, safe_harbor_multiple,
salt_cap, other_itemized) is independently settable: a scenario can move one
lever without restating the whole schedule. There is no sunset — once
applied, a change stands until a later TaxLawChange supersedes that field.

HAND CALCULATION (the base scenario)
-------------------------------------
Assets:Checking opens at 0.00. A single "salary" stream pays 5,000.00/month
to Income:Ordinary, so ordinary income is 60,000.00 every year. With the
DEFAULT std deduction (29,200.00) and DEFAULT brackets:

    taxable = 60,000.00 - 29,200.00 = 30,800.00
    tax = 23,200 @ 10% + 7,600 @ 12% = 2,320.00 + 912.00 = 3,232.00

Bumping the standard deduction to 40,000.00 for TY2028 onward:

    taxable = 60,000.00 - 40,000.00 = 20,000.00
    tax = 20,000 @ 10% = 2,000.00  (falls entirely in the first bracket)
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO


def _control(law_changes=(), years=4, salary="5000.00", streams=None):
    tax = {"settle_month": 4, "law_changes": list(law_changes)}
    control = {
        "start": "2026-01-01", "years": years,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": "0"},
        ],
        "streams": streams if streams is not None else [
            {"name": "salary", "to": "Assets:Checking",
             "income": "Income:Ordinary", "amount": salary, "owner": "chuck"},
        ],
        "tax": tax,
    }
    return control


def run(**kw):
    engine, sim, years = build(_control(**kw))
    sim.run(years)
    return engine, sim


# --- the change lands on the targeted tax-year boundary ---------------------

def test_law_change_takes_effect_at_the_targeted_tax_year_boundary():
    engine, sim = run(law_changes=[{"year": 2028, "std_deduction": "40000.00"}])
    te = sim.tax_engine
    # TY2026 and TY2027 predate the change: default deduction, default tax.
    assert te.results[2026].deduction == money("29200.00")
    assert te.results[2026].tax == money("3232.00")
    assert te.results[2027].deduction == money("29200.00")
    assert te.results[2027].tax == money("3232.00")
    # TY2028 is the targeted year: applied in January, so December's
    # accrual already sees the new deduction.
    assert te.results[2028].deduction == money("40000.00")
    assert te.results[2028].tax == money("2000.00")


def test_law_change_persists_with_no_sunset():
    engine, sim = run(law_changes=[{"year": 2028, "std_deduction": "40000.00"}])
    te = sim.tax_engine
    # No sunset: TY2029 keeps the new deduction, not the old one.
    assert te.results[2029].deduction == money("40000.00")
    assert te.results[2029].tax == money("2000.00")


def test_law_change_effective_at_sim_start_applies_immediately():
    engine, sim = run(law_changes=[{"year": 2026, "std_deduction": "0.00"}])
    te = sim.tax_engine
    # Applied in the sim's very first period (Jan 2026), so even TY2026's
    # own December accrual is affected.
    assert te.results[2026].deduction == ZERO
    assert te.results[2026].tax == money("6736.00")


def test_multiple_law_changes_apply_in_order():
    engine, sim = run(law_changes=[
        {"year": 2027, "std_deduction": "35000.00"},
        {"year": 2029, "std_deduction": "40000.00"},
    ])
    te = sim.tax_engine
    assert te.results[2026].deduction == money("29200.00")
    assert te.results[2027].deduction == money("35000.00")
    assert te.results[2028].deduction == money("35000.00")
    assert te.results[2029].deduction == money("40000.00")


def test_law_change_fires_exactly_once():
    engine, sim = run(law_changes=[{"year": 2027, "std_deduction": "40000.00"}])
    te = sim.tax_engine
    assert len(te.law_change_log) == 1
    assert te.law_changes[0].fired is True
    assert "TY2027 tax law change applied" in te.law_change_log[0]


# --- each lever is independently settable ------------------------------

def test_bracket_change_replaces_the_whole_schedule():
    flat = [["1000000000000.00", "0.05"]]
    engine, sim = run(law_changes=[{"year": 2027, "brackets": flat}])
    te = sim.tax_engine
    # TY2026 still uses the default progressive schedule.
    assert te.results[2026].tax == money("3232.00")
    # TY2027 onward is a single flat 5% bracket, same deduction untouched.
    assert te.results[2027].deduction == money("29200.00")
    assert te.results[2027].tax == money("1540.00")


def test_ss_inclusion_can_be_changed_independently():
    streams = [
        {"name": "salary", "to": "Assets:Checking", "income": "Income:Ordinary",
         "amount": "1000.00", "owner": "chuck"},
        {"name": "ss", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "2000.00", "owner": "chuck"},
    ]
    engine, sim = run(law_changes=[{"year": 2027, "ss_inclusion": "0.50"}],
                       streams=streams)
    te = sim.tax_engine
    # Default inclusion (0.85) governs TY2026's taxable SS.
    assert te.results[2026].taxable_ss == money("20400.00")
    # The lowered rate governs TY2027 onward, and nothing else moved.
    assert te.results[2027].taxable_ss == money("12000.00")
    assert te.results[2027].deduction == money("29200.00")


def test_safe_harbor_salt_cap_and_other_itemized_are_independently_settable():
    engine, sim = run(law_changes=[{
        "year": 2027, "safe_harbor_multiple": "1.50",
        "salt_cap": "5000.00", "other_itemized": "1200.00",
    }])
    te = sim.tax_engine
    assert te.safe_harbor_multiple == Decimal("1.50")
    assert te.salt_cap == money("5000.00")
    assert te.other_itemized == money("1200.00")


# --- whole-ledger ------------------------------------------------------

def test_whole_ledger_sums_to_zero_in_every_variant():
    for engine, _ in (
        run(law_changes=[{"year": 2028, "std_deduction": "40000.00"}]),
        run(law_changes=[{"year": 2027, "brackets":
                          [["1000000000000.00", "0.05"]]}]),
        run(law_changes=[{"year": 2027, "ss_inclusion": "0.5"}], streams=[
            {"name": "salary", "to": "Assets:Checking",
             "income": "Income:Ordinary", "amount": "1000.00", "owner": "chuck"},
            {"name": "ss", "to": "Assets:Checking", "income": "Income:SS",
             "amount": "2000.00", "owner": "chuck"},
        ]),
    ):
        assert sum(engine.balances().values(), ZERO) == ZERO


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
