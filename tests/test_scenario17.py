"""Scenario 17 checks -- multiple cash_management blocks: list vs. legacy
dict input, independent per-block forced funding, cross-block waterfall
cycle detection, and the "no silent default across ambiguous blocks" rule.

Run directly:  python tests/test_scenario17.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


# --- A: two independent, non-cyclic blocks -----------------------------------

TWO_OWNERS = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:CheckingA", "opening": "5000.00"},
        {"type": "asset", "name": "Assets:CheckingB", "opening": "30000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "200000.00", "basis": "200000.00"},
    ],
    "shocks": [
        {"name": "MedicalA", "from": "Assets:CheckingA", "to": "Expenses:Medical",
         "amount": "10000.00", "when": "2026-02-01"},
    ],
    "cash_management": [
        {"account": "Assets:CheckingA", "floor": "3000.00", "target": "8000.00",
         "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}]},
        {"account": "Assets:CheckingB", "floor": "3000.00", "target": "8000.00",
         "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}]},
    ],
}


def test_list_form_builds_one_manager_per_block():
    engine, sim = _run(TWO_OWNERS)
    assert len(sim.cash_managers) == 2
    assert sim.cash_managers[0].cash_account == "Assets:CheckingA"
    assert sim.cash_managers[1].cash_account == "Assets:CheckingB"


def test_each_block_reacts_only_to_its_own_account():
    engine, sim = _run(TWO_OWNERS)
    # CheckingA breached its floor and got topped up from the shared brokerage.
    assert len(sim.cash_managers[0].events) >= 1
    assert engine.balance("Assets:CheckingA") == money("8000.00")
    # CheckingB never dropped below its floor, so its manager never fired.
    assert sim.cash_managers[1].events == []
    assert engine.balance("Assets:CheckingB") == money("30000.00")


# --- B: legacy single-dict form still works, wrapped to a one-item list ------

SINGLE_DICT = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "50000.00", "basis": "50000.00"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "3000.00", "target": "10000.00",
        "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}],
    },
}


def test_legacy_dict_form_still_builds_a_single_element_list():
    engine, sim = _run(SINGLE_DICT)
    assert len(sim.cash_managers) == 1
    assert sim.cash_managers[0].cash_account == "Assets:Checking"


# --- C: a waterfall cycle across blocks is a build-time error ----------------

CYCLE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:CheckingA", "opening": "5000.00"},
        {"type": "asset", "name": "Assets:CheckingB", "opening": "5000.00"},
    ],
    "cash_management": [
        {"account": "Assets:CheckingA", "floor": "1000.00", "target": "2000.00",
         "waterfall": [{"source": "Assets:CheckingB", "floor": "0.00"}]},
        {"account": "Assets:CheckingB", "floor": "1000.00", "target": "2000.00",
         "waterfall": [{"source": "Assets:CheckingA", "floor": "0.00"}]},
    ],
}


def test_mutual_waterfall_cycle_is_rejected_at_build_time():
    try:
        build(CYCLE)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Assets:CheckingA" in str(e)
        assert "Assets:CheckingB" in str(e)


SELF_CYCLE = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
    ],
    "cash_management": [
        {"account": "Assets:Checking", "floor": "1000.00", "target": "2000.00",
         "waterfall": [{"source": "Assets:Checking", "floor": "0.00"}]},
    ],
}


def test_self_referential_waterfall_is_rejected_too():
    try:
        build(SELF_CYCLE)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Assets:Checking" in str(e)


# --- D: ambiguous default account with 2+ blocks is a hard error ------------

AMBIGUOUS_DIVIDEND_TARGET = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:CheckingA", "opening": "5000.00"},
        {"type": "asset", "name": "Assets:CheckingB", "opening": "5000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "50000.00", "basis": "50000.00",
         "dividend_yield": "0.02", "reinvest": False},
    ],
    "cash_management": [
        {"account": "Assets:CheckingA", "floor": "1000.00", "target": "2000.00",
         "waterfall": []},
        {"account": "Assets:CheckingB", "floor": "1000.00", "target": "2000.00",
         "waterfall": []},
    ],
}


def test_two_blocks_without_explicit_dividend_to_still_raises():
    try:
        build(AMBIGUOUS_DIVIDEND_TARGET)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "dividend_to" in str(e)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
