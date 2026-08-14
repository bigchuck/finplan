"""Scenario 14 checks -- output reports. Plain asserts.

Run directly:  python tests/test_scenario14.py

No new production code: this scenario exercises report surfaces that already
exist on the classes --

  Engine.balances(prefix)      Tier 1/2: raw dict of account -> balance,
                                filterable by prefix ("Assets:", "Income:").
  generators.schedule_report   Tier 3: per-Schedule firing history, already
                                formatted as text via Schedule.report().
  CashManager.report()         Tier 3: forced-withdrawal report, soft
                                reserve-breach vs hard reserve-exhausted,
                                including the gross-up math on a withholding
                                source (IRA).

-- against real ledger state, to confirm the exact text/values they produce
before deciding whether any of it needs a Tier 4 (waterfall) report on top.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.cashmanager import HARD, SOFT
from finplan.control import build
from finplan.generators import schedule_report
from finplan.primitives import money, ZERO


def run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


# --- Tier 1/2: Engine.balances(prefix) --------------------------------------

def _schedule_control():
    return {
        "start": "2026-01-01", "years": 2,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": "0"},
            {"type": "asset", "name": "Assets:Savings", "owner": "chuck",
             "opening": "100000.00"},
        ],
        "schedules": [
            {"name": "withdrawal", "source": "Assets:Savings",
             "to": "Assets:Checking", "mode": "fixed", "amount": "1000.00",
             "month": 3, "owner": "chuck"},
        ],
    }


def test_engine_balances_prefix_filters_and_matches_running_totals():
    engine, sim = run(_schedule_control())
    # Two firings of 1,000.00 (March 2026, March 2027), no inflation.
    assert engine.balances("Assets:") == {
        "Assets:Checking": money("2000.00"),
        "Assets:Savings": money("98000.00"),
    }


# --- Tier 3: schedule_report -------------------------------------------------

def test_schedule_report_lists_each_firing_with_gross_and_net():
    engine, sim = run(_schedule_control())
    sched = next(o for o in sim.objects
                if getattr(o, "name", None) == "withdrawal")
    text = schedule_report([sched])
    assert "Scheduled-withdrawal report:" in text
    assert "withdrawal (fixed) — 2 distribution(s):" in text
    assert ("2026-03-01  Assets:Savings -> Assets:Checking: "
           "gross 1,000.00, net 1,000.00") in text
    assert ("2027-03-01  Assets:Savings -> Assets:Checking: "
           "gross 1,000.00, net 1,000.00") in text


def test_schedule_report_never_fired_case():
    control = _schedule_control()
    control["schedules"][0]["month"] = 3
    control["schedules"][0]["start"] = "2030-01-01"   # after the 2-year run
    engine, sim = run(control)
    sched = next(o for o in sim.objects
                if getattr(o, "name", None) == "withdrawal")
    assert sched.report() == "  withdrawal (fixed): never fired."


# --- Tier 3: CashManager.report() -------------------------------------------

def _shock_control(savings_opening, ira=False, extra_shocks=None):
    source_account = (
        {"type": "traditional_ira", "name": "Assets:IRA", "owner": "chuck",
         "opening": savings_opening}
        if ira else
        {"type": "asset", "name": "Assets:Savings", "owner": "chuck",
         "opening": savings_opening}
    )
    source_name = "Assets:IRA" if ira else "Assets:Savings"
    shocks = [
        {"name": "medical", "from": "Assets:Checking",
         "to": "Expenses:Medical", "amount": "3000.00",
         "when": "2026-02-01", "owner": "chuck"},
    ]
    if extra_shocks:
        shocks.extend(extra_shocks)
    return {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
             "opening": "2000.00"},
            source_account,
        ],
        "shocks": shocks,
        "cash_management": {
            "account": "Assets:Checking", "floor": "1000.00",
            "target": "3000.00",
            "waterfall": [{"source": source_name, "floor": "0"}],
        },
    }


def test_cashmanager_no_shortfall_reports_floor_held():
    control = _shock_control(savings_opening="100000.00")
    control["shocks"][0]["amount"] = "0.00"   # no drain -> floor never breached
    engine, sim = run(control)
    assert sim.cash_managers[0].report() == (
        "No forced withdrawals: the cash floor held every month.")


def test_cashmanager_soft_reserve_breach_recovers_to_target():
    engine, sim = run(_shock_control(savings_opening="100000.00"))
    # Feb: checking 2,000 - 3,000 shock = -1,000 (below 1,000 floor).
    # need_net = target(3,000) - (-1,000) = 4,000; Savings has ample
    # capacity and no withholding, so gross == net == 4,000.
    assert engine.balance("Assets:Checking") == money("3000.00")
    report = sim.cash_managers[0].report()
    assert "[soft] reserve-breach" in report
    assert "cash -1,000.00 -> 3,000.00" in report
    assert "from Assets:Savings: gross 4,000.00, net 4,000.00" in report
    assert "still short" not in report
    assert "(1 funded month(s), 0 hard / insolvent)" in report


def test_cashmanager_hard_insolvent_when_waterfall_runs_dry():
    # cover_shortfall logs a FundingEvent any month cash opens below floor,
    # even when the pull is empty -- so without recovery, every month from
    # March on would re-fire against a dry waterfall too. A one-time March
    # relief shock (external, not from the waterfall) pushes cash back
    # above the floor so Feb is the ONLY month that ever triggers.
    relief = [{"name": "relief", "from": "Income:Windfall",
              "to": "Assets:Checking", "amount": "2000.00",
              "when": "2026-03-01", "owner": "chuck"}]
    engine, sim = run(_shock_control(savings_opening="500.00",
                                     extra_shocks=relief))
    # Savings can only supply 500.00 of the 4,000.00 needed net; cash ends
    # Feb at -1,000 + 500 = -500 (below the 1,000 floor -> HARD), then the
    # March relief brings it to -500 + 2,000 = 1,500 (above floor, no
    # further events for the rest of the year).
    assert engine.balance("Assets:Checking") == money("1500.00")
    report = sim.cash_managers[0].report()
    assert "[HARD] reserve-exhausted" in report
    assert "cash -1,000.00 -> -500.00" in report
    assert "from Assets:Savings: gross 500.00, net 500.00" in report
    assert "still short of target by 3,500.00" in report
    assert "(1 funded month(s), 1 hard / insolvent)" in report


def test_cashmanager_report_shows_gross_up_on_withholding_source():
    engine, sim = run(_shock_control(savings_opening="100000.00", ira=True))
    # need_net = 4,000; IRA withholds 20%, so gross = 4,000 / 0.8 = 5,000,
    # withheld = 1,000, net delivered = 4,000 -- exactly covers the target.
    assert engine.balance("Assets:Checking") == money("3000.00")
    assert engine.balance("Assets:PrepaidTax:TY2026") == money("1000.00")
    report = sim.cash_managers[0].report()
    assert "[soft] reserve-breach" in report
    assert ("from Assets:IRA: gross 5,000.00, net 4,000.00, "
           "withheld 1,000.00, ordinary") in report


# --- whole-ledger ------------------------------------------------------

def test_whole_ledger_sums_to_zero_in_every_variant():
    relief = [{"name": "relief", "from": "Income:Windfall",
              "to": "Assets:Checking", "amount": "2000.00",
              "when": "2026-03-01", "owner": "chuck"}]
    for engine, _ in (
        run(_schedule_control()),
        run(_shock_control(savings_opening="100000.00")),
        run(_shock_control(savings_opening="500.00", extra_shocks=relief)),
        run(_shock_control(savings_opening="100000.00", ira=True)),
    ):
        assert sum(engine.balances().values(), ZERO) == ZERO


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
