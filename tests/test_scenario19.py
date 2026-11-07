"""Scenario 19 checks -- a roth_conversion that funds its own tax.

A ``tax_funding`` clause ties the conversion to the account that pays for
it: each firing converts only as much as that account can pay tax on while
staying at or above its floor (scaled down, never drawing on any other
account), pays the tax in the same firing, records a skipped event when
there is no headroom, and converts again once the account recovers.

Run directly:  python tests/test_scenario19.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.generators import Schedule, schedule_report
from finplan.primitives import money, ZERO


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _zero(engine):
    return sum(engine.balances().values(), ZERO) == ZERO


def _posted(engine, account):
    """Everything ever posted to ``account`` -- Income/Expenses are closed
    out at year end, so their closing balance reads zero."""
    return sum((p.amount for t in engine.journal for p in t.postings
                if p.account == account and p.meta.get("kind") != "close"),
               ZERO)


def _schedule(sim, name="Conversion"):
    return next(o for o in sim.objects
                if isinstance(o, Schedule) and o.name == name)


def _control(brokerage="200000.00", basis=None, growth="0.0", years=1,
             **funding):
    tax_funding = {"from": "Assets:Brokerage", "floor": "50000.00",
                   "fed_rate": "0.30", "state_rate": "0.05",
                   "state_to": "Expenses:Tax:State"}
    tax_funding.update(funding)
    return {
        "start": "2026-01-01", "years": years,
        "accounts": [
            {"type": "traditional_ira", "name": "Assets:IRA",
             "opening": "500000.00"},
            {"type": "roth", "name": "Assets:IRA:Roth", "opening": "0.00"},
            {"type": "brokerage", "name": "Assets:Brokerage",
             "opening": brokerage, "basis": basis or brokerage,
             "growth": growth},
        ],
        "schedules": [
            {"name": "Conversion", "source": "Assets:IRA",
             "to": "Assets:IRA:Roth", "mode": "roth_conversion",
             "amount": "100000.00", "month": 1,
             "tax_funding": tax_funding},
        ],
    }


# --- A: plenty of headroom -- full conversion, tax paid same firing ---------

def test_full_conversion_pays_fed_and_state_tax_from_the_source():
    engine, sim = _run(_control())
    assert engine.balance("Assets:IRA:Roth") == money("100000.00")
    assert engine.balance("Assets:IRA") == money("400000.00")
    assert engine.balance(
        "Assets:PrepaidTax:TY2026:Estimated") == money("30000.00")
    assert _posted(engine, "Expenses:Tax:State") == money("5000.00")
    assert engine.balance("Assets:Brokerage") == money("165000.00")
    ev = _schedule(sim).events[0]
    assert ev.tax_paid == money("35000.00")
    assert ev.limited_by is None and ev.skipped is None
    assert _zero(engine)


# --- B: scaled down so the source lands exactly on its floor ----------------

def test_conversion_is_scaled_to_what_the_source_can_pay_for():
    # headroom 71,000 - 50,000 = 21,000 at 35% -> 60,000 converted.
    engine, sim = _run(_control(brokerage="71000.00"))
    assert engine.balance("Assets:IRA:Roth") == money("60000.00")
    assert engine.balance(
        "Assets:PrepaidTax:TY2026:Estimated") == money("18000.00")
    assert _posted(engine, "Expenses:Tax:State") == money("3000.00")
    assert engine.balance("Assets:Brokerage") == money("50000.00")
    ev = _schedule(sim).events[0]
    assert ev.requested == money("100000.00")
    assert ev.limited_by == "tax_funding"
    assert "scaled to fit tax funding" in schedule_report([_schedule(sim)])
    assert _zero(engine)


def test_scaling_never_takes_the_source_below_its_floor():
    # An awkward headroom (10,000.01 / 0.35) exercises the cent rounding.
    engine, _ = _run(_control(brokerage="60000.01"))
    assert engine.balance("Assets:Brokerage") >= money("50000.00")
    assert _zero(engine)


# --- C: at/below the floor -- skip, draw on nothing, then resume -------------

def test_below_floor_converts_nothing_and_touches_no_account():
    engine, sim = _run(_control(brokerage="40000.00"))
    assert engine.balance("Assets:IRA:Roth") == ZERO
    assert engine.balance("Assets:IRA") == money("500000.00")
    assert engine.balance("Assets:Brokerage") == money("40000.00")
    assert engine.accounts("Assets:PrepaidTax") == []
    ev = _schedule(sim).events[0]
    assert ev.skipped is not None and ev.gross == ZERO
    report = schedule_report([_schedule(sim)])
    assert "0 distribution(s), 1 skipped" in report
    assert "below its floor 50,000.00" in report


def test_conversions_resume_once_the_source_recovers():
    # 40k grows 5%/month: below the floor in Jan 2026, ~71.8k by Jan 2027.
    engine, sim = _run(_control(brokerage="40000.00", growth="0.60",
                                years=2))
    first, second = _schedule(sim).events
    assert first.skipped is not None
    assert second.skipped is None and second.gross > ZERO
    assert second.limited_by == "tax_funding"
    assert abs(second.tax_paid - second.gross * money("0.35")) <= money("0.01")
    assert engine.balance(
        "Assets:PrepaidTax:TY2027:Estimated") == money(second.gross
                                                        * money("0.30"))
    assert _zero(engine)


# --- D: one sale for the whole bill keeps realized gain right ---------------

def test_tax_sale_realizes_gain_once_on_the_combined_amount():
    # Half the brokerage is gain, so half of the 35,000 sale is LTCG.
    engine, _ = _run(_control(basis="100000.00"))
    assert _posted(engine, "Income:CapGains:LT") == money("-17500.00")
    assert _zero(engine)


def test_fed_only_funding_needs_no_state_account():
    engine, _ = _run(_control(state_rate="0", state_to=None))
    assert engine.balance(
        "Assets:PrepaidTax:TY2026:Estimated") == money("30000.00")
    assert engine.accounts("Expenses:Tax") == []
    assert engine.balance("Assets:Brokerage") == money("170000.00")


# --- D2: min_amount -- no sliver conversions --------------------------------

def test_scaled_conversion_below_min_amount_is_skipped():
    # Headroom 21,000 at 35% affords 60,000 -- under a 75,000 minimum.
    engine, sim = _run(_control(brokerage="71000.00", min_amount="75000.00"))
    assert engine.balance("Assets:IRA:Roth") == ZERO
    assert engine.balance("Assets:Brokerage") == money("71000.00")
    ev = _schedule(sim).events[0]
    assert ev.skipped is not None and "min_amount 75,000.00" in ev.skipped
    assert "would convert only 60,000.00" in ev.skipped
    assert _zero(engine)


def test_scaled_conversion_at_or_above_min_amount_still_fires():
    engine, sim = _run(_control(brokerage="71000.00", min_amount="60000.00"))
    assert engine.balance("Assets:IRA:Roth") == money("60000.00")
    assert _schedule(sim).events[0].skipped is None


def test_min_amount_scales_with_inflation():
    # 10% inflation makes the 60,000 minimum 66,000 in 2027 nominal terms;
    # the requested 100,000 becomes 110,000. Headroom 21,000 affords 60,000.
    control = _control(brokerage="71000.00", min_amount="60000.00", years=2)
    control["inflation"] = {"mode": "real", "rates": {"2026": "0.10"}}
    control["schedules"][0]["start"] = "2027-01-01"
    _, sim = _run(control)
    ev = _schedule(sim).events[0]
    assert ev.skipped is not None and "min_amount 66" in ev.skipped


def test_min_amount_above_amount_is_rejected():
    _raises(_control(min_amount="100000.01"), "could never convert")


# --- E: build-time validation ----------------------------------------------

def _raises(control, fragment):
    try:
        build(control)
        assert False, "expected an error"
    except (ValueError, KeyError) as e:
        assert fragment in str(e), str(e)


def test_state_rate_without_state_to_is_rejected():
    _raises(_control(state_to=None), "state_to")


def test_withholding_source_is_rejected():
    _raises(_control(**{"from": "Assets:IRA"}), "withholds")


def test_unknown_tax_funding_key_is_rejected():
    _raises(_control(flor="1.00"), "flor")


def test_undeclared_tax_funding_source_is_rejected():
    _raises(_control(**{"from": "Assets:Nope"}), "Assets:Nope")


def test_tax_funding_on_a_non_conversion_schedule_is_rejected():
    control = _control()
    control["schedules"][0]["mode"] = "fixed"
    _raises(control, "roth_conversion")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
