"""Scenario 3 checks — CashManager: waterfall, gross-up, source-specific
shapes, Equity:Recognized contra, basis, and the forced-withdrawal report.

Run directly:  python tests/test_scenario3.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.cashmanager import SOFT, HARD
from finplan.primitives import money, ZERO


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _find(sim, name):
    for o in sim.objects:
        if getattr(o, "name", None) == name:
            return o
    raise KeyError(name)


def _zero(engine):
    return sum(engine.balances().values(), ZERO) == ZERO


# --- A: brokerage pull realizes the GAIN SLICE only -----------------------

BROKERAGE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "200000.00", "basis": "120000.00"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "30000.00",
        "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}],
    },
}


def test_brokerage_refills_to_target():
    engine, _ = _run(BROKERAGE)
    assert engine.balance("Assets:Checking") == money("30000.00")
    assert engine.balance("Assets:Brokerage") == money("175000.00")


def test_brokerage_recognizes_gain_slice_only():
    engine, _ = _run(BROKERAGE)
    # Pulled 25000 gross; gain = 25000 * (200000-120000)/200000 = 10000.
    # Gate swept at close -> RetainedEarnings holds exactly that gain; the
    # Equity:Recognized contra holds its mirror so net worth is untouched.
    assert engine.balance("Income:CapGains:LT") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("-10000.00")
    assert engine.balance("Equity:Recognized") == money("10000.00")


def test_basis_decrements_proportionally():
    _, sim = _run(BROKERAGE)
    brok = _find(sim, "Assets:Brokerage")
    # basis_sold = gross - gain = 25000 - 10000 = 15000 -> 120000 - 15000.
    assert money(brok.attrs["basis"]) == money("105000.00")


def test_brokerage_sale_is_networth_neutral():
    engine, _ = _run(BROKERAGE)
    assets = (engine.balance("Assets:Checking")
              + engine.balance("Assets:Brokerage"))
    equity = (engine.balance("Equity:Opening")
              + engine.balance("Equity:RetainedEarnings")
              + engine.balance("Equity:Recognized"))
    assert assets + equity == ZERO
    assert _zero(engine)


# --- B: IRA gross-up + withholding to PrepaidTax ---------------------------

IRA = {
    "start": "2026-01-01", "years": 1,
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


def test_ira_grosses_up_to_deliver_net():
    engine, _ = _run(IRA)
    # Need 15000 net. Gross = 15000/(1-0.20) = 18750; withhold 3750; net 15000.
    assert engine.balance("Assets:Checking") == money("20000.00")
    assert engine.balance("Assets:IRA") == money("481250.00")
    assert engine.balance("Assets:PrepaidTax:TY2026") == money("3750.00")


def test_ira_recognizes_full_pull_as_ordinary():
    engine, _ = _run(IRA)
    # Full 18750 gross is ordinary income (not just a slice); swept at close.
    assert engine.balance("Income:Ordinary") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("-18750.00")
    assert engine.balance("Equity:Recognized") == money("18750.00")
    assert _zero(engine)


# --- C: multi-source waterfall proves subtract-NET, not gross --------------

MULTI = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "traditional_ira", "name": "Assets:IRA",
         "opening": "20000.00", "withholding": "0.20"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "100000.00", "basis": "100000.00"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "40000.00",
        "waterfall": [
            {"source": "Assets:IRA", "floor": "0.00"},
            {"source": "Assets:Brokerage", "floor": "0.00"},
        ],
    },
}


def test_subtract_net_reaches_target_across_sources():
    engine, _ = _run(MULTI)
    # IRA capacity 20000 -> net 16000 (4000 withheld); still need 24000.
    # Brokerage delivers the remaining 24000 net. Correct code hits 40000.
    # The subtract-GROSS bug would stop at 36000 (under-funded by the 4000
    # it wrongly counted as delivered). This asserts we subtract NET.
    assert engine.balance("Assets:Checking") == money("40000.00")
    assert engine.balance("Assets:IRA") == ZERO
    assert engine.balance("Assets:PrepaidTax:TY2026") == money("4000.00")
    assert engine.balance("Assets:Brokerage") == money("76000.00")
    assert _zero(engine)


# --- D: reserve-exhausted (HARD / insolvent) -------------------------------

INSOLVENT = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "1000.00"},
        {"type": "roth", "name": "Assets:Roth", "opening": "3000.00"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "30000.00",
        "waterfall": [{"source": "Assets:Roth", "floor": "0.00"}],
    },
}


def test_reserve_exhausted_is_hard():
    engine, sim = _run(INSOLVENT)
    ev = sim.cash_manager.events
    # Never recovers, so every month is a breach: one HARD event per month.
    assert len(ev) == 12
    assert all(e.severity == HARD for e in ev)
    # Month 1 drains the Roth; 1000 + 3000 = 4000, still far below the floor.
    assert ev[0].cash_before == money("1000.00")
    assert ev[0].cash_after == money("4000.00")
    assert ev[0].shortfall_remaining == money("26000.00")
    # Month 2+ find an empty waterfall: nothing left to pull.
    assert ev[1].pulls == []
    assert ev[1].cash_after == money("4000.00")
    assert engine.balance("Assets:Roth") == ZERO


def test_roth_pull_recognizes_no_income():
    engine, _ = _run(INSOLVENT)
    # Roth is tax-free: a pure transfer, so no Income gate ever materializes.
    assert engine.accounts("Income:") == []
    assert engine.balance("Equity:Recognized") == ZERO
    assert _zero(engine)


# --- E: no breach -> no events ---------------------------------------------

COMFORTABLE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "50000.00"},
        {"type": "roth", "name": "Assets:Roth", "opening": "10000.00"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "30000.00",
        "waterfall": [{"source": "Assets:Roth", "floor": "0.00"}],
    },
}


def test_no_breach_no_forced_withdrawals():
    engine, sim = _run(COMFORTABLE)
    assert sim.cash_manager.events == []
    assert "No forced withdrawals" in sim.cash_manager.report()
    assert engine.balance("Assets:Checking") == money("50000.00")
    assert engine.balance("Assets:Roth") == money("10000.00")


# --- F: waterfall order — Roth is break-glass, tapped last -----------------

ORDERED = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "0.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "15000.00", "basis": "15000.00"},
        {"type": "roth", "name": "Assets:Roth", "opening": "100000.00"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "20000.00",
        "waterfall": [
            {"source": "Assets:Brokerage", "floor": "0.00"},
            {"source": "Assets:Roth", "floor": "0.00"},
        ],
    },
}


def test_waterfall_drains_in_order_roth_last():
    engine, sim = _run(ORDERED)
    # Brokerage (15000) is fully drained before Roth is touched; Roth gives up
    # only the remaining 5000 -> proves order and break-glass-last.
    assert engine.balance("Assets:Brokerage") == ZERO
    assert engine.balance("Assets:Roth") == money("95000.00")
    assert engine.balance("Assets:Checking") == money("20000.00")
    assert sim.cash_manager.events[0].severity == SOFT


def test_forced_withdrawals_are_tagged():
    engine, _ = _run(ORDERED)
    forced = [t for t in engine.journal if t.meta.get("forced")]
    assert forced, "expected forced-withdrawal transactions in the journal"
    assert all(t.meta.get("trigger") == "cash-floor" for t in forced)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))