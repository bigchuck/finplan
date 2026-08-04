"""Scenario 13 checks -- the nominal/real (inflation) switch. Plain asserts.

Run directly:  python tests/test_scenario13.py

A top-level "inflation" block is opt-in, mirroring "estimates"/"state" in
taxengine.py: absent means mode "nominal", every declared dollar amount
posts exactly as written, and Period.inflation is 1 for the whole run.
Under mode "real", the "rates" dict is a YEAR-KEYED, CARRY-FORWARD annual
CPI schedule -- {"2026": "0.12", "2028": "0.06"} means 12%/yr through all
of 2026 and 2027 (no 2027 key -> carries forward the 2026 rate), then 6%/yr
from 2028 onward. It escalates only DECLARED DOLLAR AMOUNTS: Stream.amount,
Schedule.amount (fixed/roth_conversion modes), and Shock.amount. It does
NOT touch apr/growth/dividend_yield, and Schedule's `rmd` mode is
balance-driven and already nominal, so it is untouched too.

HAND CALCULATION (compounding)
-------------------------------
The annual rate is applied as a MONTHLY rate (annual/12), compounded every
period, factor 1.0 (undiscounted) at the very first period of the sim. At
12% annual -> 1% monthly:

    factor at Jan 2026 (period 0)  = 1.00
    factor at Feb 2026 (period 1)  = 1.01
    factor at Jan 2027 (period 12) = 1.01^12 = 1.126825030131969720661201...

A 1,000.00 salary stream posts money(1000.00 * factor) each period:

    Jan 2026: 1000.00
    Feb 2026: 1010.00
    Jan 2027: 1126.83

Summed over 36 months (2026-2028) with the carry-forward switch to 6%/yr
at 2028, a flat 1,000.00/mo stream totals exactly 36,000.00 under mode
"nominal" and 42,636.36 under mode "real" with the schedule above (salary
only -- see test_salary_stream_escalates_and_totals_correctly_over_3_years).
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.inflation import InflationError
from finplan.primitives import money, ZERO


def _control(inflation=None, years=3, salary="1000.00",
             shock_amount="0.00", shock_when="2027-06-01",
             schedule_amount="0.00", schedule_month=3):
    control = {
        "start": "2026-01-01", "years": years,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": "0"},
            {"type": "asset", "name": "Assets:Savings", "owner": "chuck",
             "opening": "1000000.00"},
        ],
        "streams": [
            {"name": "salary", "to": "Assets:Checking",
             "income": "Income:Ordinary", "amount": salary, "owner": "chuck"},
        ],
        "shocks": [
            {"name": "bonus", "from": "Income:Bonus", "to": "Assets:Checking",
             "amount": shock_amount, "when": shock_when, "owner": "chuck"},
        ],
        "schedules": [
            {"name": "withdrawal", "source": "Assets:Savings",
             "to": "Assets:Checking", "mode": "fixed",
             "amount": schedule_amount, "month": schedule_month,
             "owner": "chuck"},
        ],
    }
    if inflation is not None:
        control["inflation"] = inflation
    return control


def run(**kw):
    engine, sim, years = build(_control(**kw))
    sim.run(years)
    return engine, sim


RATES = {"2026": "0.12", "2028": "0.06"}


# --- mode "nominal" is the default and is fully inert -----------------------

def test_absent_inflation_block_defaults_to_nominal_and_does_not_escalate():
    engine, sim = run()   # no "inflation" key at all
    assert sim.inflation.mode == "nominal"
    # 1,000.00/mo x 36 months, flat -- no compounding.
    assert engine.balance("Assets:Checking") == money("36000.00")


def test_mode_nominal_ignores_a_rates_block_entirely():
    engine, sim = run(inflation={"mode": "nominal", "rates": RATES})
    # Same total as the no-block case: rates are parsed but never consulted.
    assert engine.balance("Assets:Checking") == money("36000.00")


# --- mode "real" escalates and carries the rate forward ---------------------

def test_salary_stream_escalates_and_totals_correctly_over_3_years():
    engine, sim = run(inflation={"mode": "real", "rates": RATES})
    assert sim.inflation.mode == "real"
    # 12%/yr through 2026-2027 (2027 carries the 2026 rate forward), then
    # 6%/yr from 2028 -- see the module docstring for the compounding math.
    assert engine.balance("Assets:Checking") == money("42636.36")


def test_first_period_is_undiscounted():
    # A 1-year run isolates period 0: factor 1.0, so January's posting is
    # the declared amount exactly, unescalated.
    engine, sim = run(inflation={"mode": "real", "rates": RATES}, years=1)
    # 1,000 + 1,010 + ... for 12 months at 1%/mo; if period 0 were anything
    # other than 1.0 this total would not match the hand-calculated figure.
    assert engine.balance("Assets:Checking") == money("12682.51")


def test_rate_carries_forward_across_years_with_no_declared_key():
    # Only a 2026 key; 2027 and 2028 must both carry it forward at 12%/yr
    # (no 6% step-down, unlike RATES above).
    engine, sim = run(inflation={"mode": "real",
                                 "rates": {"2026": "0.12"}})
    assert engine.balance("Assets:Checking") == money("43076.88")


# --- Shock and Schedule (fixed mode) also escalate; rmd mode does not ------

def test_shock_escalates_by_the_factor_in_force_on_its_own_date():
    # Salary disabled so Checking holds only the shock's escalated posting.
    engine, sim = run(inflation={"mode": "real", "rates": RATES},
                      salary="0.00", shock_amount="500.00",
                      shock_when="2027-06-01", years=2)
    assert engine.balance("Assets:Checking") == money("592.15")


def test_schedule_fixed_mode_escalates_each_firing():
    engine, sim = run(inflation={"mode": "real", "rates": RATES},
                      salary="0.00", schedule_amount="300.00",
                      schedule_month=3, years=3)
    sched = next(o for o in sim.objects
                if getattr(o, "name", None) == "withdrawal")
    assert [e.gross for e in sched.events] == [
        money("306.03"), money("344.84"), money("384.74"),
    ]


def test_rmd_mode_is_balance_driven_and_never_escalates():
    ira_control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "traditional_ira", "name": "Assets:IRA", "owner": "chuck",
             "opening": "1000000.00"},
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": "0"},
        ],
        "schedules": [
            {"name": "rmd", "source": "Assets:IRA", "to": "Assets:Checking",
             "mode": "rmd", "owner_birth_year": 1950, "owner": "chuck"},
        ],
    }
    engine_nom, sim_nom, y_nom = build(dict(ira_control))
    sim_nom.run(y_nom)
    real_control = dict(ira_control)
    real_control["inflation"] = {"mode": "real", "rates": {"2026": "0.50"}}
    engine_real, sim_real, y_real = build(real_control)
    sim_real.run(y_real)

    sched_nom = next(o for o in sim_nom.objects
                     if getattr(o, "name", None) == "rmd")
    sched_real = next(o for o in sim_real.objects
                      if getattr(o, "name", None) == "rmd")
    # age 76 at 2026, divisor 23.7: 1,000,000 / 23.7, identical regardless
    # of a wildly different inflation rate, since rmd mode never reads
    # period.inflation.
    assert sched_nom.events[0].gross == sched_real.events[0].gross
    assert sched_nom.events[0].gross == money("42194.09")


# --- the coverage gap is a hard error at build time -------------------------

def test_real_mode_hard_fails_when_rates_do_not_cover_the_start_year():
    try:
        run(inflation={"mode": "real", "rates": {"2027": "0.041"}})
        assert False, "expected InflationError"
    except InflationError:
        pass


def test_real_mode_hard_fails_when_rates_is_empty():
    try:
        run(inflation={"mode": "real", "rates": {}})
        assert False, "expected InflationError"
    except InflationError:
        pass


def test_unknown_mode_is_rejected():
    try:
        run(inflation={"mode": "chained-cpi", "rates": RATES})
        assert False, "expected InflationError"
    except InflationError:
        pass


# --- whole-ledger ------------------------------------------------------

def test_whole_ledger_sums_to_zero_in_every_variant():
    for engine, _ in (
        run(),
        run(inflation={"mode": "real", "rates": RATES}),
        run(inflation={"mode": "real", "rates": RATES},
            shock_amount="500.00", schedule_amount="300.00"),
    ):
        assert sum(engine.balances().values(), ZERO) == ZERO


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
