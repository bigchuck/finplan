"""Scenario 7 checks — Schedule: forced/planned withdrawals (RMD, fixed).

A Schedule never reimplements gross-up/withholding/recognition; it decides
HOW MUCH and WHEN, then delegates to the source account's own fund_from —
same contract CashManager uses. So an RMD off a TraditionalIRAAccount still
grosses up, withholds to PrepaidTax:TYn, and recognizes the full pull as
Income:Ordinary "for free".

Run directly:  python tests/test_scenario7.py
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


# --- A: fixed mode — a declared gross amount, every year, one named month --

FIXED = {
    "start": "2026-01-01", "years": 2,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "roth", "name": "Assets:Roth", "opening": "100000.00"},
    ],
    "schedules": [
        {"name": "PlannedPull", "source": "Assets:Roth", "to": "Assets:Checking",
         "mode": "fixed", "amount": "10000.00", "month": 3},
    ],
}


def test_fixed_schedule_fires_once_per_year_in_named_month():
    engine, _ = _run(FIXED)
    # 2 years * 10000 = 20000 pulled; Roth pull is a pure transfer (no tax).
    assert engine.balance("Assets:Checking") == money("20000.00")
    assert engine.balance("Assets:Roth") == money("80000.00")
    assert engine.accounts("Income:") == []
    assert _zero(engine)


def test_fixed_schedule_fires_in_the_configured_month_only():
    engine, _ = _run(FIXED)
    march_pulls = [t for t in engine.journal
                   if t.meta.get("trigger") == "PlannedPull"]
    assert len(march_pulls) == 2
    assert all(t.date.month == 3 for t in march_pulls)


def test_scheduled_transactions_are_tagged():
    engine, _ = _run(FIXED)
    tagged = [t for t in engine.journal if t.meta.get("scheduled")]
    assert tagged
    assert all(t.meta.get("trigger") == "PlannedPull" for t in tagged)
    assert all(t.meta.get("source") == "Assets:Roth" for t in tagged)


# --- B: fixed schedule caps at capacity rather than overdrawing ------------

CAPPED = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "roth", "name": "Assets:Roth", "opening": "3000.00"},
    ],
    "schedules": [
        {"name": "TooBig", "source": "Assets:Roth", "to": "Assets:Checking",
         "mode": "fixed", "amount": "10000.00", "month": 1},
    ],
}


def test_fixed_schedule_caps_at_available_capacity():
    engine, _ = _run(CAPPED)
    # Only 3000 available; the schedule pulls 3000, not the requested 10000.
    assert engine.balance("Assets:Roth") == ZERO
    assert engine.balance("Assets:Checking") == money("3000.00")
    assert _zero(engine)


# --- C: RMD mode — balance / divisor, gated by age, reusing IRA fund_from --

RMD = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "530000.00", "withholding": "0.20"},
    ],
    "schedules": [
        {"name": "RMD", "source": "Assets:IRA", "to": "Assets:Checking",
         "mode": "rmd", "owner_birth_year": 1952},   # turns 74 in 2026
    ],
}


def test_rmd_uses_divisor_table_for_owners_age():
    engine, sim = _run(RMD)
    # Age 74 in 2026 -> divisor 25.5 (2022 Uniform Lifetime Table).
    # gross = 530000 / 25.5 = 20784.31... -> money-rounded.
    expected_gross = money(money("530000.00") / money("25.5"))
    assert expected_gross == money("20784.31")
    # IRA fund_from shape: withhold 20%, net delivered to checking.
    withheld = money(expected_gross * money("0.20"))
    net = money(expected_gross - withheld)
    assert engine.balance("Assets:PrepaidTax:TY2026") == withheld
    assert engine.balance("Assets:Checking") == net
    assert engine.balance("Assets:IRA") == money("530000.00") - expected_gross


def test_rmd_recognizes_full_pull_as_ordinary_income():
    engine, _ = _run(RMD)
    # Same recognition shape as a CashManager IRA pull: full gross is
    # ordinary income, swept to RetainedEarnings via the Recognized contra.
    gross = money(money("530000.00") / money("25.5"))
    assert engine.balance("Income:Ordinary") == ZERO   # swept
    assert engine.balance("Equity:Recognized") == gross
    assert engine.balance("Equity:RetainedEarnings") == -gross
    assert _zero(engine)


def test_rmd_fires_in_january_reading_prior_year_end_balance():
    engine, _ = _run(RMD)
    rmd_txns = [t for t in engine.journal if t.meta.get("trigger") == "RMD"]
    assert rmd_txns
    assert all(t.date.month == 1 for t in rmd_txns)


# --- D: RMD does not fire before the owner reaches rmd_start_age -----------

TOO_YOUNG = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "500000.00"},
    ],
    "schedules": [
        {"name": "RMD", "source": "Assets:IRA", "to": "Assets:Checking",
         "mode": "rmd", "owner_birth_year": 1970},   # turns 56 in 2026
    ],
}


def test_rmd_does_not_fire_before_start_age():
    engine, _ = _run(TOO_YOUNG)
    assert engine.balance("Assets:IRA") == money("500000.00")
    assert engine.balance("Assets:Checking") == ZERO
    assert engine.accounts("Income:") == []
    assert _zero(engine)


# --- E: RMD start age is configurable per schedule --------------------------

CUSTOM_AGE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "500000.00", "withholding": "0.00"},
    ],
    "schedules": [
        {"name": "RMD", "source": "Assets:IRA", "to": "Assets:Checking",
         "mode": "rmd", "owner_birth_year": 1956, "rmd_start_age": 70},
    ],
}


def test_custom_rmd_start_age_is_honored():
    engine, sim = _run(CUSTOM_AGE)
    # Age 70 in 2026 with rmd_start_age=70 -> fires, even though the default
    # table's floor (72) would otherwise gate it out.
    assert engine.balance("Assets:IRA") < money("500000.00")


# --- F: divisors table is overridable per schedule --------------------------

CUSTOM_DIVISOR = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "100000.00", "withholding": "0.00"},
    ],
    "schedules": [
        {"name": "RMD", "source": "Assets:IRA", "to": "Assets:Checking",
         "mode": "rmd", "owner_birth_year": 1952,
         "divisors": {"74": "10.0"}},   # override the default 25.5 for age 74
    ],
}


def test_divisor_override_is_honored():
    engine, _ = _run(CUSTOM_DIVISOR)
    # 100000 / 10.0 = 10000 exactly, not the default-table 25.5 result.
    assert engine.balance("Assets:Checking") == money("10000.00")
    assert engine.balance("Assets:IRA") == money("90000.00")


# --- G: Roth conversion — full amount to Roth, NO withholding leg ----------

CONVERSION = {
    "start": "2026-01-01", "years": 2,
    "accounts": [
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "200000.00", "withholding": "0.20"},   # rate irrelevant here
        {"type": "roth", "name": "Assets:Roth", "opening": "0.00"},
    ],
    "schedules": [
        {"name": "Conversion", "source": "Assets:IRA", "to": "Assets:Roth",
         "mode": "roth_conversion", "amount": "50000.00", "month": 11},
    ],
}


def test_conversion_moves_full_gross_no_withholding():
    engine, _ = _run(CONVERSION)
    # 2 years * 50000 converted; the FULL amount lands in Roth even though
    # the IRA is configured with 20% withholding — conversions don't withhold.
    assert engine.balance("Assets:Roth") == money("100000.00")
    assert engine.balance("Assets:IRA") == money("100000.00")
    # No PrepaidTax account ever materializes: no withholding leg was posted.
    assert engine.accounts("Assets:PrepaidTax:") == []


def test_conversion_recognizes_full_amount_as_ordinary():
    engine, _ = _run(CONVERSION)
    # Same recognition gate as a distribution: full converted amount is
    # ordinary income, swept to RetainedEarnings via the Recognized contra.
    assert engine.balance("Income:Ordinary") == ZERO   # swept each year
    assert engine.balance("Equity:Recognized") == money("100000.00")
    assert engine.balance("Equity:RetainedEarnings") == money("-100000.00")
    assert _zero(engine)


def test_conversion_requires_traditional_ira_source():
    bad = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "roth", "name": "Assets:Roth", "opening": "10000.00"},
            {"type": "roth", "name": "Assets:Roth2", "opening": "0.00"},
        ],
        "schedules": [
            {"name": "BadConversion", "source": "Assets:Roth",
             "to": "Assets:Roth2", "mode": "roth_conversion", "amount": "1000.00"},
        ],
    }
    try:
        build(bad)
    except ValueError as e:
        assert "roth_conversion" in str(e)
        return
    raise AssertionError("expected ValueError for non-IRA conversion source")


# --- H: start/end windows bound a schedule (any mode) -----------------------

WINDOWED = {
    "start": "2026-01-01", "years": 4,
    "accounts": [
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "400000.00", "withholding": "0.00"},
        {"type": "roth", "name": "Assets:Roth", "opening": "0.00"},
    ],
    "schedules": [
        {"name": "Conversion", "source": "Assets:IRA", "to": "Assets:Roth",
         "mode": "roth_conversion", "amount": "20000.00", "month": 6,
         "start": "2027-01-01", "end": "2028-12-31"},
    ],
}


def test_start_end_window_bounds_a_schedule():
    engine, _ = _run(WINDOWED)
    # 4 years declared (2026-2029) but the window only covers 2027 and 2028:
    # exactly 2 conversions of 20000 = 40000, not 4 * 20000 = 80000.
    assert engine.balance("Assets:Roth") == money("40000.00")
    assert engine.balance("Assets:IRA") == money("360000.00")


# --- I: forced-withdrawal-style report for schedules -------------------------

def test_report_names_schedule_that_never_fired():
    # TOO_YOUNG's owner never reaches rmd_start_age within the 1-year horizon.
    _, sim = _run(TOO_YOUNG)
    schedules = [o for o in sim.objects if isinstance(o, Schedule)]
    report = schedule_report(schedules)
    assert "RMD" in report
    assert "never fired" in report


def test_report_lists_each_firing_with_amounts():
    engine, sim = _run(RMD)
    schedules = [o for o in sim.objects if isinstance(o, Schedule)]
    report = schedule_report(schedules)
    gross = money(money("530000.00") / money("25.5"))
    assert "RMD" in report
    assert f"{gross:,.2f}" in report
    assert "withheld" in report   # RMD test fixture withholds 20%


def test_report_flags_capacity_capped_pulls():
    _, sim = _run(CAPPED)
    schedules = [o for o in sim.objects if isinstance(o, Schedule)]
    report = schedule_report(schedules)
    assert "capped by capacity" in report
    assert "10,000.00" in report   # the requested amount, distinct from gross


def test_schedule_events_recorded_with_requested_vs_gross():
    _, sim = _run(CAPPED)
    sched = next(o for o in sim.objects if isinstance(o, Schedule))
    assert len(sched.events) == 1
    ev = sched.events[0]
    assert ev.requested == money("10000.00")
    assert ev.gross == money("3000.00")   # capped to available capacity


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))