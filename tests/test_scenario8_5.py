"""Scenario 8.5 — growth on tax-deferred/tax-free accounts (IRA, Roth).

TaxDeferredGrowth is a mixin: monthly compound growth with no gain
recognition and no basis tracking, since nothing is realized inside the
wrapper. The offsetting leg goes to Equity:UnrealizedGains:Deferred, kept
separate from Equity:UnrealizedGains so check_unrealized() — scoped to
brokerages only — is never touched by IRA/Roth activity.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import date

from finplan.accounts import (
    BrokerageAccount, RothAccount, TraditionalIRAAccount,
    UNREALIZED, UNREALIZED_DEFERRED, check_unrealized,
)
from finplan.engine import Engine
from finplan.primitives import Posting, Transaction, ZERO, money
from finplan.simulation import Period


def _seed(acct, opening):
    return Transaction(
        date=date(2026, 1, 1), description="Opening balances",
        postings=[
            Posting(acct.name, opening, meta={"kind": "opening"}),
            Posting("Equity:Opening", -opening, meta={"kind": "opening"}),
        ],
    )


def _tick(engine, objects, period):
    pending = []
    for obj in objects:
        pending.extend(obj.step(period, engine))
    for txn in pending:
        engine.post(txn)
    for obj in objects:
        obj.settle_effects(period, engine)


def test_ira_growth_compounds_monthly():
    engine = Engine()
    ira = TraditionalIRAAccount(name="Assets:IRA", attrs={"growth": "0.06"})
    engine.post(_seed(ira, 100000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _tick(engine, [ira], period)

    # 100000 * 0.06 / 12 = 500.00
    assert engine.balance("Assets:IRA") == money("100500.00")


def test_roth_growth_same_mechanism():
    engine = Engine()
    roth = RothAccount(name="Assets:Roth", attrs={"growth": "0.07"})
    engine.post(_seed(roth, 50000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _tick(engine, [roth], period)

    # 50000 * 0.07 / 12 = 291.67
    assert engine.balance("Assets:Roth") == money("50291.67")


def test_growth_posts_to_deferred_contra_not_unrealized():
    engine = Engine()
    ira = TraditionalIRAAccount(name="Assets:IRA", attrs={"growth": "0.06"})
    engine.post(_seed(ira, 100000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _tick(engine, [ira], period)

    assert engine.balance(UNREALIZED_DEFERRED) == money("-500.00")
    assert engine.balance(UNREALIZED) == ZERO   # brokerage-only account untouched


def test_no_growth_attr_is_noop():
    engine = Engine()
    ira = TraditionalIRAAccount(name="Assets:IRA", attrs={})
    engine.post(_seed(ira, 100000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _tick(engine, [ira], period)

    assert engine.balance("Assets:IRA") == money("100000")
    assert engine.balance(UNREALIZED_DEFERRED) == ZERO


def test_check_unrealized_ignores_ira_roth_growth():
    engine = Engine()
    broker = BrokerageAccount(name="Assets:Brokerage",
                              attrs={"growth": "0.05", "basis": 200000})
    ira = TraditionalIRAAccount(name="Assets:IRA", attrs={"growth": "0.06"})
    roth = RothAccount(name="Assets:Roth", attrs={"growth": "0.07"})
    engine.post(_seed(broker, 200000))
    engine.post(_seed(ira, 100000))
    engine.post(_seed(roth, 50000))

    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _tick(engine, [broker, ira, roth], period)

    # Brokerage invariant holds even though IRA/Roth also grew this tick —
    # they post to a different contra entirely, so they can't leak in.
    check_unrealized(engine, [broker])


def test_ira_fund_from_still_taxes_full_pull_regardless_of_growth():
    engine = Engine()
    ira = TraditionalIRAAccount(name="Assets:IRA",
                                attrs={"growth": "0.06", "withholding": "0.20"})
    engine.post(_seed(ira, 100000))
    period = Period(date=date(2026, 1, 1), year=2026, month=1)
    _tick(engine, [ira], period)   # grow first: balance now 100500.00

    wd = ira.fund_from(money("10000"), "Assets:Checking", period, engine, {})
    for txn in wd.txns:
        engine.post(txn)

    # No gain/basis split exists for an IRA — the full gross pull is what's
    # recognized, growth or no growth.
    assert wd.gross == money("10000")
    assert wd.character == "ordinary"
    assert not hasattr(ira, "basis") and "basis" not in ira.attrs


def test_order_independence_growth_vs_other_objects():
    def build_and_run(reversed_order):
        engine = Engine()
        ira = TraditionalIRAAccount(name="Assets:IRA", attrs={"growth": "0.06"})
        roth = RothAccount(name="Assets:Roth", attrs={"growth": "0.07"})
        engine.post(_seed(ira, 100000))
        engine.post(_seed(roth, 50000))
        objs = [roth, ira] if reversed_order else [ira, roth]
        period = Period(date=date(2026, 1, 1), year=2026, month=1)
        _tick(engine, objs, period)
        return engine.balance("Assets:IRA"), engine.balance("Assets:Roth")

    assert build_and_run(False) == build_and_run(True)


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"PASS {_name}")
    print("ALL PASS")