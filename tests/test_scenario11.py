"""Scenario 11 checks — death and legacy. Plain asserts.

Run directly:  python tests/test_scenario11.py

Death is the one event that emits no transaction of its own. It MUTATES the
object graph at the top of the tick, before that month accrues, and on the
terminal death it halts the run and prices the estate. So most of what is
asserted here is state, not postings — with one exception: the ORDERING
assertion below, which is the only thing standing between "the benefit
stopped in June" and "the benefit stopped in July."

HAND CALCULATION (the base scenario)
------------------------------------
Checking opens 100,000.00; IRA opens 400,000.00 with zero growth, so it just
sits there and stays hand-checkable. No interest, no dividends, no tax.

Monthly inflow while both are living:
    ss_chuck   3,000.00  (owner chuck)
    ss_spouse  1,800.00  (owner spouse)
    pension    2,000.00  (owner spouse, survivorship 0.5)
                        -------
                          6,800.00

Spouse dies 2028-06-01. SS is cross-stream: the survivor keeps the LARGER
benefit, so ss_spouse (1,800.00, the smaller) goes to zero and ss_chuck
(3,000.00) is retitled to chuck and continues untouched. The pension is
per-stream: 2,000.00 x 0.5 = 1,000.00. New monthly inflow 4,000.00.

    Jan 2026 - May 2028 = 29 months x 6,800.00 = 197,200.00
    Jun 2028 - Aug 2030 = 27 months x 4,000.00 = 108,000.00

Chuck dies 2030-09-01: the run HALTS at the top of that tick, so September
never accrues.

    Checking  100,000.00 + 197,200.00 + 108,000.00 = 405,200.00
    IRA                                              400,000.00
    assets                                           805,200.00
    heir tax  400,000.00 x 0.24                       96,000.00
    NET TO HEIRS                                     709,200.00

THE SURVIVOR'S PENALTY
----------------------
With a tax block attached, TY2028 and TY2029 make the point that filing
status is the largest cash effect of a first death, and that it arrives a
year LATE:

  TY2028 (MFJ, the year of the death — a widow(er) may still file jointly)
    SS      3,000x12 + 1,800x5      = 45,000.00 -> 85% = 38,250.00 taxable
    pension 2,000x5  + 1,000x7      = 17,000.00 ordinary
    less MFJ standard deduction        29,200.00
    taxable                            26,050.00
    tax  23,200 @ 10% + 2,850 @ 12% =   2,662.00

  TY2029 (Single, first full survivor year)
    SS      3,000x12                = 36,000.00 -> 85% = 30,600.00 taxable
    pension 1,000x12                = 12,000.00 ordinary
    less SINGLE standard deduction     14,600.00
    taxable                            28,000.00
    tax  11,600 @ 10% + 16,400 @ 12% =  3,128.00

Income FELL by 16,000.00 and tax ROSE by 466.00. Had the same TY2029 income
been taxed as MFJ it would have owed 1,340.00 — so the status change alone
costs 1,788.00, and a model that skipped it would flatter every survivor
year in the plan.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.generators import Stream
from finplan.primitives import money, ZERO


BOTH_DEATHS = [
    {"owner": "spouse", "when": "2028-06-01", "survivor": "chuck"},
    {"owner": "chuck", "when": "2030-09-01"},
]

STREAMS = [
    {"name": "ss_chuck", "to": "Assets:Checking", "income": "Income:SS",
     "amount": "3000.00", "owner": "chuck"},
    {"name": "ss_spouse", "to": "Assets:Checking", "income": "Income:SS",
     "amount": "1800.00", "owner": "spouse"},
    {"name": "pension", "to": "Assets:Checking", "income": "Income:Ordinary",
     "amount": "2000.00", "owner": "spouse", "survivorship": "0.5"},
]


def _control(deaths=None, heir_rate="0.24", years=5, accounts=None,
             streams=None, **top):
    control = {
        "start": "2026-01-01", "years": years,
        "accounts": accounts if accounts is not None else [
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": "100000.00"},
            {"type": "traditional_ira", "name": "Assets:IRA", "owner": "chuck",
             "opening": "400000.00", "growth": "0"},
        ],
        "streams": streams if streams is not None else STREAMS,
        "legacy": {
            "heir_rate": heir_rate,
            "deaths": BOTH_DEATHS if deaths is None else deaths,
        },
    }
    control.update(top)
    return control


def run(**kw):
    engine, sim, years = build(_control(**kw))
    sim.run(years)
    return engine, sim


def _stream(sim, name):
    return next(o for o in sim.objects
                if isinstance(o, Stream) and o.name == name)


# --- Social Security is cross-stream ---------------------------------------

def test_survivor_keeps_the_larger_benefit_and_the_smaller_stops():
    engine, sim = run()
    # No per-stream fraction can express this: the rule compares the two
    # benefits to each other, which is why SS is special-cased.
    assert _stream(sim, "ss_chuck").amount == money("3000.00")
    assert _stream(sim, "ss_spouse").amount == ZERO


def test_the_kept_benefit_is_retitled_to_the_survivor():
    engine, sim = run()
    assert _stream(sim, "ss_chuck").attrs["owner"] == "chuck"


def test_the_larger_benefit_survives_even_when_the_decedent_owned_it():
    engine, sim = run(streams=[
        {"name": "ss_chuck", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "1500.00", "owner": "chuck"},
        {"name": "ss_spouse", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "2400.00", "owner": "spouse"},
    ])
    # The spouse died, but the spouse's was the larger benefit — so it is the
    # one that continues, retitled. Keying off who died rather than off which
    # is larger would have stopped exactly the wrong one.
    assert _stream(sim, "ss_spouse").amount == money("2400.00")
    assert _stream(sim, "ss_spouse").attrs["owner"] == "chuck"
    assert _stream(sim, "ss_chuck").amount == ZERO


# --- everything else is per-stream -----------------------------------------

def test_pension_continues_at_its_survivorship_fraction():
    engine, sim = run(deaths=[BOTH_DEATHS[0]])
    pension = _stream(sim, "pension")
    assert pension.amount == money("1000.00")
    assert pension.attrs["owner"] == "chuck"


def test_survivorship_is_one_shot_and_never_compounds():
    engine, sim = run()
    # The pension was retitled to chuck at the first death. Matching it by
    # owner again at the second and re-applying 0.5 would leave 500.00 — a
    # continuation for a survivor who does not exist. It stops instead.
    assert _stream(sim, "pension").amount == ZERO
    assert "survivorship already applied" in sim.estate.report()


def test_survivorship_defaults_to_zero_not_to_full_continuation():
    engine, sim = run(streams=[
        {"name": "annuity", "to": "Assets:Checking", "income": "Income:Ordinary",
         "amount": "2000.00", "owner": "spouse"},
    ])
    # A default of "keeps paying" would silently flatter every plan that
    # forgot to state a survivorship. Single-life is the harsher default and
    # the deliberate one.
    assert _stream(sim, "annuity").amount == ZERO


def test_the_survivors_own_streams_are_untouched():
    engine, sim = run()
    # Only streams the DECEDENT owned are adjusted.
    assert _stream(sim, "ss_chuck").amount == money("3000.00")


# --- ordering: the mutation lands BEFORE that month's accrue ---------------

def test_a_benefit_that_stops_in_june_does_not_pay_in_june():
    engine, sim = run()
    paid = [t for t in engine.journal if t.description == "ss_spouse income"]
    # Jan 2026 through May 2028 inclusive = 29 payments, and nothing in June.
    assert len(paid) == 29
    assert max((t.date.year, t.date.month) for t in paid) == (2028, 5)


def test_the_reduced_pension_pays_in_the_month_of_death():
    engine, sim = run()
    june = [t for t in engine.journal
            if t.description == "pension income"
            and (t.date.year, t.date.month) == (2028, 6)]
    assert len(june) == 1
    assert any(p.amount == money("1000.00") for p in june[0].postings)


# --- halting ----------------------------------------------------------------

def test_the_terminal_death_halts_the_run():
    engine, sim = run()
    assert sim.estate.halted_at == date(2030, 9, 1)
    # September never accrued: past the last death there is no plan left to
    # project, only an estate to distribute.
    assert max(t.date for t in engine.journal) == date(2030, 8, 1)


def test_one_declared_death_is_a_survivor_scenario_that_runs_to_term():
    engine, sim = run(deaths=[BOTH_DEATHS[0]])
    assert sim.estate.halted_at is None
    assert sim.estate.bequest is None
    assert max(t.date for t in engine.journal).year == 2030
    assert "survivor still" in sim.estate.report()


# --- the bequest ------------------------------------------------------------

def test_estate_is_valued_at_the_top_of_the_terminal_tick():
    engine, sim = run()
    b = sim.estate.bequest
    assert engine.balance("Assets:Checking") == money("405200.00")
    assert b.assets == money("805200.00")
    assert b.net == money("805200.00")
    assert not b.insolvent


def test_heir_tax_is_charged_on_deferred_balances_only():
    engine, sim = run()
    b = sim.estate.bequest
    # The IRA carries its deferred income tax to the heir (IRD, no step-up);
    # the checking account does not.
    assert b.deferred == money("400000.00")
    assert b.heir_tax == money("96000.00")
    assert b.net_to_heirs == money("709200.00")


def test_appreciated_brokerage_passes_at_full_value():
    engine, sim = run(accounts=[
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage", "owner": "chuck",
         "opening": "400000.00", "basis": "50000.00", "growth": "0"},
    ])
    b = sim.estate.bequest
    # 350,000.00 of embedded gain, and the heir owes nothing on it: basis
    # steps up to market value at death. This is the whole reason the report
    # separates gross from net — an IRA and a brokerage account of equal size
    # are NOT equally good things to leave behind.
    assert b.deferred == ZERO
    assert b.heir_tax == ZERO
    assert b.net_to_heirs == b.net == money("805200.00")


def test_debt_does_not_die_and_can_leave_the_estate_insolvent():
    engine, sim = run(accounts=[
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00"},
        {"type": "heloc", "name": "Liabilities:HELOC", "owner": "chuck",
         "opening": "-900000.00", "credit_limit": "1000000.00",
         "rate": "0", "opened": "2026-01-01"},
    ])
    b = sim.estate.bequest
    assert b.assets == money("405200.00")
    assert b.debts == money("-900000.00")
    assert b.net == money("-494800.00")
    # Netting the two into a single "bequest" figure would hide this outright.
    assert b.insolvent
    assert "INSOLVENT" in sim.estate.report()


# --- filing status ----------------------------------------------------------

TAX = {"settle_month": 4}


def test_filing_status_is_still_joint_in_the_year_of_the_death():
    engine, sim = run(tax=TAX)
    # A widow(er) may still file jointly for the year of death, so TY2028
    # takes the MFJ standard deduction even though the spouse died in June.
    assert sim.tax_engine.results[2028].deduction == money("29200.00")
    assert sim.tax_engine.results[2028].tax == money("2662.00")


def test_filing_status_flips_to_single_the_following_year():
    engine, sim = run(tax=TAX)
    assert sim.tax_engine.filing_status == "single"
    assert sim.tax_engine.results[2029].deduction == money("14600.00")
    assert sim.tax_engine.results[2029].tax == money("3128.00")


def test_the_survivors_penalty_raises_tax_on_falling_income():
    engine, sim = run(tax=TAX)
    r = sim.tax_engine.results
    # 2029 has 16,000.00 LESS income than 2028 and owes MORE tax. That
    # inversion is the survivor's penalty, and it is invisible to any model
    # that leaves filing status alone.
    assert r[2029].tax > r[2028].tax


def test_single_brackets_can_be_overridden_from_the_control_file():
    control = _control(tax=TAX)
    control["legacy"]["single"] = {"std_deduction": "20000.00"}
    engine, sim, years = build(control)
    sim.run(years)
    # Same swap machinery, scenario-supplied figures — so a future year's
    # tables need no code change.
    assert sim.tax_engine.results[2029].deduction == money("20000.00")


# --- whole-ledger -----------------------------------------------------------

def test_whole_ledger_sums_to_zero_in_every_variant():
    for engine, _ in (run(),
                      run(tax=TAX),
                      run(deaths=[BOTH_DEATHS[0]]),
                      run(accounts=[
                          {"type": "asset", "name": "Assets:Checking",
                           "owner": "chuck", "opening": "100000.00"},
                          {"type": "brokerage", "name": "Assets:Brokerage",
                           "owner": "chuck", "opening": "400000.00",
                           "basis": "50000.00", "growth": "0"}])):
        assert sum(engine.balances().values(), ZERO) == ZERO


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
