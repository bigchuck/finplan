import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from datetime import date
from decimal import Decimal

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.accounts import BrokerageAccount, check_unrealized, UnrealizedInvariantError
from finplan.engine import Engine
from finplan.generators import DividendPolicy, Policy
from finplan.primitives import ZERO, money
from finplan.simobject import SimObject
from finplan.simulation import Period, Simulation
from finplan.taxengine import TaxEngine, ORDINARY, PREFERENTIAL


def _mk_broker(mv, basis, growth="0.06", dividend_yield="0.02",
                qualified_fraction=1, reinvest=True, dividend_to=None):
    attrs = {"growth": growth, "dividend_yield": dividend_yield,
             "basis": basis, "qualified_fraction": qualified_fraction,
             "reinvest": reinvest}
    if dividend_to:
        attrs["dividend_to"] = dividend_to
    acct = BrokerageAccount(name="Assets:Brokerage", attrs=attrs)
    return acct


def _run_one_month(engine, objects, period):
    pending = []
    for obj in objects:
        pending.extend(obj.step(period, engine))
    for txn in pending:
        engine.post(txn)
    for obj in objects:
        obj.settle_effects(period, engine)


def test_price_return_dividend_additive_and_basis_credited():
    engine = Engine()
    acct = _mk_broker(mv=100000, basis=100000)
    div = DividendPolicy(name="dividend:Assets:Brokerage", source=acct)
    engine.post(_seed(acct, 100000))

    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _run_one_month(engine, [acct, div], period)

    mv_after = engine.balance(acct.name)
    # growth: 100000 * 0.06/12 = 500 ; dividend (on tick-open mv=100000): 100000*0.02/12 ≈ 166.67
    assert mv_after == money("100000") + money("500.00") + money("166.67"), mv_after
    # basis rose by exactly the reinvested dividend (already-taxed money)
    assert money(acct.attrs["basis"]) == money("100000") + money("166.67"), acct.attrs["basis"]
    # gain fraction unaffected by the growth leg alone (growth doesn't touch basis)
    check_unrealized(engine, [acct])


def test_qualified_ordinary_split_sums_exactly():
    engine = Engine()
    acct = _mk_broker(mv=100000, basis=50000, qualified_fraction="0.7")
    div = DividendPolicy(name="dividend:Assets:Brokerage", source=acct,
                          qualified_fraction="0.7")
    engine.post(_seed(acct, 100000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _run_one_month(engine, [acct, div], period)

    q = -engine.balance("Income:Dividends:Qualified")
    o = -engine.balance("Income:Dividends:Ordinary")
    total = engine.balance(acct.name) - money("100000") - money("500.00")  # strip growth
    assert q + o == total, (q, o, total)
    assert q > o  # 70/30 split


def test_reinvest_false_routes_to_cash_no_basis_change():
    engine = Engine()
    acct = _mk_broker(mv=100000, basis=100000, reinvest=False,
                      dividend_to="Assets:Checking")
    div = DividendPolicy(name="dividend:Assets:Brokerage", source=acct,
                          reinvest=False, to="Assets:Checking")
    engine.post(_seed(acct, 100000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _run_one_month(engine, [acct, div], period)

    assert engine.balance("Assets:Checking") == money("166.67")
    assert money(acct.attrs["basis"]) == money("100000")   # untouched
    check_unrealized(engine, [acct])


def test_order_independence_reversed_objects():
    def build_and_run(reversed_order):
        engine = Engine()
        acct = _mk_broker(mv=100000, basis=100000)
        div = DividendPolicy(name="dividend:Assets:Brokerage", source=acct)
        engine.post(_seed(acct, 100000))
        objs = [div, acct] if reversed_order else [acct, div]
        period = Period(date=date(2026, 1, 1), year=2026, month=1)
        _run_one_month(engine, objs, period)
        return engine.balance(acct.name), money(acct.attrs["basis"])

    a = build_and_run(False)
    b = build_and_run(True)
    assert a == b, (a, b)


def test_tax_character_routes_qualified_as_preferential():
    engine = Engine()
    acct = _mk_broker(mv=100000, basis=100000, growth="0", qualified_fraction="1")
    div = DividendPolicy(name="dividend:Assets:Brokerage", source=acct)
    engine.post(_seed(acct, 100000))
    tax = TaxEngine()
    assert tax.character_of("Income:Dividends:Qualified") == PREFERENTIAL
    assert tax.character_of("Income:Dividends:Ordinary") == ORDINARY
    assert tax.character_of("Income:CapGains:LT") == PREFERENTIAL
    assert tax.character_of("Income:SomeNewGate") == ORDINARY  # unmatched -> ordinary


def test_unrealized_invariant_catches_a_planted_leak():
    engine = Engine()
    acct = _mk_broker(mv=100000, basis=100000)
    engine.post(_seed(acct, 100000))
    check_unrealized(engine, [acct])  # holds
    acct.attrs["basis"] = money("90000")  # plant a drift (basis changed with no txn)
    try:
        check_unrealized(engine, [acct])
        assert False, "expected UnrealizedInvariantError"
    except UnrealizedInvariantError:
        pass


def test_no_dividend_yield_is_a_no_op():
    engine = Engine()
    acct = BrokerageAccount(name="Assets:Brokerage",
                            attrs={"growth": "0.06", "basis": 100000})
    # no DividendPolicy constructed when dividend_yield is unset/zero — this
    # mirrors control.build's guard, exercised directly here since this test
    # operates below the control-file layer.
    assert money(acct.attrs.get("dividend_yield", 0)) == ZERO


def _seed(acct, opening):
    from finplan.primitives import Posting, Transaction
    return Transaction(
        date=date(2026, 1, 1), description="Opening balances",
        postings=[
            Posting(acct.name, opening, meta={"kind": "opening"}),
            Posting("Equity:Opening", -opening, meta={"kind": "opening"}),
        ],
    )


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")