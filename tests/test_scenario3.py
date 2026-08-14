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
    assert engine.balance("Income:CapGains:LT") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("-10000.00")
    # Post-Scenario-4 (Option-B invariant): a brokerage sale unwinds the
    # realized slice out of Equity:UnrealizedGains, not a fresh Recognized
    # contra (that contra is IRA-only). See test_scenario4.py. The bucket
    # holds -(mv - basis) = -(175000 - 105000), not the 10000 gain slice.
    assert engine.balance("Equity:UnrealizedGains") == money("-70000.00")
    assert engine.balance("Equity:Recognized") == ZERO


def test_basis_decrements_proportionally():
    _, sim = _run(BROKERAGE)
    brok = _find(sim, "Assets:Brokerage")
    assert money(brok.attrs["basis"]) == money("105000.00")


def test_brokerage_sale_is_networth_neutral():
    engine, _ = _run(BROKERAGE)
    assets = (engine.balance("Assets:Checking")
              + engine.balance("Assets:Brokerage"))
    equity = (engine.balance("Equity:Opening")
              + engine.balance("Equity:RetainedEarnings")
              + engine.balance("Equity:UnrealizedGains"))
    assert assets + equity == ZERO
    assert _zero(engine)


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
    assert engine.balance("Assets:Checking") == money("20000.00")
    assert engine.balance("Assets:IRA") == money("481250.00")
    assert engine.balance("Assets:PrepaidTax:TY2026") == money("3750.00")


def test_ira_recognizes_full_pull_as_ordinary():
    engine, _ = _run(IRA)
    assert engine.balance("Income:Ordinary") == ZERO
    assert engine.balance("Equity:RetainedEarnings") == money("-18750.00")
    assert engine.balance("Equity:Recognized") == money("18750.00")
    assert _zero(engine)


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
    assert engine.balance("Assets:Checking") == money("40000.00")
    assert engine.balance("Assets:IRA") == ZERO
    assert engine.balance("Assets:PrepaidTax:TY2026") == money("4000.00")
    assert engine.balance("Assets:Brokerage") == money("76000.00")
    assert _zero(engine)


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
    ev = sim.cash_managers[0].events
    assert len(ev) == 12
    assert all(e.severity == HARD for e in ev)
    assert ev[0].cash_before == money("1000.00")
    assert ev[0].cash_after == money("4000.00")
    assert ev[0].shortfall_remaining == money("26000.00")
    assert ev[1].pulls == []
    assert ev[1].cash_after == money("4000.00")
    assert engine.balance("Assets:Roth") == ZERO


def test_roth_pull_recognizes_no_income():
    engine, _ = _run(INSOLVENT)
    assert engine.accounts("Income:") == []
    assert engine.balance("Equity:Recognized") == ZERO
    assert _zero(engine)


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
    assert sim.cash_managers[0].events == []
    assert "No forced withdrawals" in sim.cash_managers[0].report()
    assert engine.balance("Assets:Checking") == money("50000.00")
    assert engine.balance("Assets:Roth") == money("10000.00")


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
    assert engine.balance("Assets:Brokerage") == ZERO
    assert engine.balance("Assets:Roth") == money("95000.00")
    assert engine.balance("Assets:Checking") == money("20000.00")
    assert sim.cash_managers[0].events[0].severity == SOFT


def test_forced_withdrawals_are_tagged():
    engine, _ = _run(ORDERED)
    forced = [t for t in engine.journal if t.meta.get("forced")]
    assert forced, "expected forced-withdrawal transactions in the journal"
    assert all(t.meta.get("trigger") == "cash-floor" for t in forced)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))