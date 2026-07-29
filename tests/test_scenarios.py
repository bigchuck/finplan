"""Scenario inheritance / override checks — plain asserts, no framework.

Run directly:  python tests/test_scenarios.py
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.scenarios import resolve, deep_merge, names, ScenarioError
from finplan.control import build_scenario
from finplan.primitives import money, ZERO


ROOT = {
    "scenarios": {
        "base": {
            "start": "2026-01-01",
            "years": 1,
            "accounts": [
                {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
                 "opening": "100000.00", "apr": "0.03"},
            ],
        },
        "higher_rate": {
            "extends": "base",
            "accounts": [{"name": "Assets:Checking", "apr": "0.05"}],
        },
        "long_high": {"extends": "higher_rate", "years": 5},
        "two_acct": {
            "extends": "base",
            "accounts": [
                {"type": "asset", "name": "Assets:Savings",
                 "opening": "5000.00", "apr": "0.04"},
            ],
        },
        "drop_checking": {
            "extends": "two_acct",
            "accounts": [{"name": "Assets:Checking", "_remove": True}],
        },
    }
}


def _s(name):
    return resolve(ROOT["scenarios"], name)


def test_names_listed():
    assert "base" in names(ROOT) and "long_high" in names(ROOT)


def test_scalar_override_wins():
    assert _s("base")["years"] == 1
    assert _s("long_high")["years"] == 5


def test_keyed_merge_preserves_siblings():
    # higher_rate touches only apr; type/owner/opening are inherited intact.
    acct = _s("higher_rate")["accounts"][0]
    assert acct["apr"] == "0.05"
    assert acct["type"] == "asset"
    assert acct["owner"] == "chuck"
    assert acct["opening"] == "100000.00"


def test_multi_level_depth():
    # long_high extends higher_rate extends base: inherits 0.05 apr AND years=5.
    s = _s("long_high")
    assert s["years"] == 5
    assert s["accounts"][0]["apr"] == "0.05"
    assert s["start"] == "2026-01-01"


def test_new_account_appended():
    accts = {a["name"] for a in _s("two_acct")["accounts"]}
    assert accts == {"Assets:Checking", "Assets:Savings"}


def test_remove_drops_inherited_account():
    accts = {a["name"] for a in _s("drop_checking")["accounts"]}
    assert accts == {"Assets:Savings"}


def test_unknown_scenario_raises():
    try:
        resolve(ROOT["scenarios"], "nope")
    except ScenarioError:
        return
    raise AssertionError("expected ScenarioError for unknown scenario")


def test_cycle_detected():
    cyclic = {"a": {"extends": "b"}, "b": {"extends": "a"}}
    try:
        resolve(cyclic, "a")
    except ScenarioError:
        return
    raise AssertionError("expected ScenarioError for cyclic chain")


def test_inputs_not_mutated():
    before = copy.deepcopy(ROOT)
    _s("long_high")
    assert ROOT == before, "resolve must not mutate its inputs"


def test_resolved_scenario_builds_and_balances():
    engine, sim, years = build_scenario(ROOT, "long_high")
    assert years == 5
    sim.run(years)
    assert sum(engine.balances().values(), ZERO) == ZERO
    # 5 years at 5% compounded monthly clears simple 5%*5 = 25k of growth.
    growth = engine.balance("Assets:Checking") - money("100000.00")
    assert growth > money("28000")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))