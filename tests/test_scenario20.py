"""Scenario 20 checks -- market segments: time-varying growth by asset class.

An account's ``growth`` is its baseline; named ``market`` segments aimed at
its ``asset_class`` override it while they run (latest start wins, falling
back to the baseline when none is running). A drawdown is a total decline
compounded geometrically over its months.

Run directly:  python tests/test_scenario20.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from decimal import Decimal

from finplan.accounts import UNREALIZED
from finplan.control import build, build_scenario
from finplan.primitives import ZERO, money


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine


def _growth(engine, account):
    """[(year, month, amount, description)] of growth postings on account."""
    out = []
    for txn in engine.journal:
        for p in txn.postings:
            if p.account == account and p.meta.get("kind") == "growth":
                out.append((txn.date.year, txn.date.month, p.amount,
                            txn.description))
    return out


def _segment_of(desc):
    """The market segment named in a growth description, or None."""
    if "(market: " not in desc:
        return None
    return desc.split("(market: ")[1].rstrip(")")


def _raises(control, fragment):
    try:
        build(control)
    except (ValueError, KeyError) as exc:
        assert fragment in str(exc), f"{fragment!r} not in {exc!r}"
        return
    raise AssertionError(f"expected an error mentioning {fragment!r}")


def _ira(growth="0.0", cls="equity", opening="100000.00",
         name="Assets:IRA"):
    acct = {"type": "traditional_ira", "name": name, "opening": opening,
            "growth": growth}
    if cls is not None:
        acct["asset_class"] = cls
    return acct


def _control(accounts, market, start="2026-01-01", years=1):
    return {"start": start, "years": years, "accounts": accounts,
            "market": market}


# --- drawdown arithmetic ----------------------------------------------------

def test_drawdown_compounds_to_exact_total():
    # 20% over 5 months, baseline 0: an untouched account ends 20% down,
    # to the cent-rounding of five monthly postings.
    engine = _run(_control(
        [_ira()],
        [{"name": "dip", "asset_class": "equity", "start": "2026-01",
          "months": 5, "drawdown": "0.20"}]))
    bal = engine.balance("Assets:IRA")
    assert abs(bal - money("80000.00")) <= money("0.05"), bal
    months = [(y, m) for y, m, _, _ in _growth(engine, "Assets:IRA")]
    assert months == [(2026, m) for m in range(1, 6)]


def test_drawdown_monthly_rate_is_geometric_not_linear():
    engine = _run(_control(
        [_ira()],
        [{"name": "dip", "asset_class": "equity", "start": "2026-01",
          "months": 5, "drawdown": "0.20"}]))
    first = _growth(engine, "Assets:IRA")[0][2]
    # (0.8)^(1/5) - 1 = -0.0436475... -> -4364.75 on 100000, not -4000.
    expected = money(Decimal(100000) * (Decimal("0.8") ** (Decimal(1) / 5) - 1))
    assert first == expected == money("-4364.75"), first


def test_growth_segment_then_baseline_hand_calc():
    # Jan under a 12% segment: 120000 * 0.12/12 = 1200.00 -> 121200.
    # Feb back on the 6% baseline: 121200 * 0.06/12 = 606.00.
    engine = _run(_control(
        [_ira(growth="0.06", opening="120000.00")],
        [{"name": "hot", "asset_class": "equity", "start": "2026-01",
          "months": 1, "growth": "0.12"}]))
    g = _growth(engine, "Assets:IRA")
    assert g[0][2] == money("1200.00")
    assert _segment_of(g[0][3]) == "hot"
    assert g[1][2] == money("606.00")
    assert _segment_of(g[1][3]) is None
    assert "0.060000 annual" in g[1][3]


# --- resolution: latest start wins, ended segments uncover what's beneath ---

def test_dip_inside_regime_hands_back_to_regime():
    engine = _run(_control(
        [_ira()],
        [{"name": "regime", "asset_class": "equity", "start": "2026-01",
          "months": 12, "growth": "0.12"},
         {"name": "dip", "asset_class": "equity", "start": "2026-03",
          "months": 2, "drawdown": "0.10"}]))
    seq = [_segment_of(d) for _, _, _, d in _growth(engine, "Assets:IRA")]
    assert seq == (["regime"] * 2 + ["dip"] * 2 + ["regime"] * 8), seq


def test_list_order_does_not_matter():
    a = {"name": "regime", "asset_class": "equity", "start": "2026-01",
         "months": 12, "growth": "0.12"}
    b = {"name": "dip", "asset_class": "equity", "start": "2026-03",
         "months": 2, "drawdown": "0.10"}
    e1 = _run(_control([_ira()], [a, b]))
    e2 = _run(_control([_ira()], [b, a]))
    assert e1.balance("Assets:IRA") == e2.balance("Assets:IRA")


def test_open_ended_segment_is_new_normal():
    engine = _run(_control(
        [_ira(growth="0.05")],
        [{"name": "slow", "asset_class": "equity", "start": "2026-06",
          "growth": "0.01"}], years=2))
    g = _growth(engine, "Assets:IRA")
    assert all(_segment_of(d) is None for y, m, _, d in g if (y, m) < (2026, 6))
    assert all(_segment_of(d) == "slow" for y, m, _, d in g if (y, m) >= (2026, 6))
    assert len(g) == 24


def test_later_open_ended_segment_replaces_earlier():
    engine = _run(_control(
        [_ira()],
        [{"name": "a", "asset_class": "equity", "start": "2026-01",
          "growth": "0.05"},
         {"name": "b", "asset_class": "equity", "start": "2026-07",
          "growth": "0.01"}]))
    seq = [_segment_of(d) for _, _, _, d in _growth(engine, "Assets:IRA")]
    assert seq == ["a"] * 6 + ["b"] * 6


def test_drawdown_crosses_year_boundary():
    engine = _run(_control(
        [_ira(growth="0.05")],
        [{"name": "dip", "asset_class": "equity", "start": "2026-11",
          "months": 4, "drawdown": "0.20"}], years=2))
    labelled = [(y, m) for y, m, _, d in _growth(engine, "Assets:IRA")
                if _segment_of(d) == "dip"]
    assert labelled == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]


def test_segment_started_before_sim_is_clipped():
    # Started 2026-05 for 5 months; the run starts 2026-07, so only Jul-Sep
    # remain, each at the full drawdown's monthly rate.
    engine = _run(_control(
        [_ira()],
        [{"name": "dip", "asset_class": "equity", "start": "2026-05",
          "months": 5, "drawdown": "0.20"}], start="2026-07-01"))
    g = _growth(engine, "Assets:IRA")
    assert [(y, m) for y, m, _, d in g if _segment_of(d) == "dip"] == [
        (2026, 7), (2026, 8), (2026, 9)]
    assert g[0][2] == money("-4364.75")


# --- scope ------------------------------------------------------------------

def test_other_class_and_unclassed_accounts_untouched():
    engine = _run(_control(
        [_ira(growth="0.06", name="Assets:IRA:Eq"),
         _ira(growth="0.06", cls="real_estate", name="Assets:House"),
         _ira(growth="0.06", cls=None, name="Assets:IRA:Plain")],
        [{"name": "dip", "asset_class": "equity", "start": "2026-01",
          "months": 3, "drawdown": "0.30"}]))
    assert engine.balance("Assets:IRA:Eq") < money("100000.00")
    for name in ("Assets:House", "Assets:IRA:Plain"):
        g = _growth(engine, name)
        assert all(_segment_of(d) is None for _, _, _, d in g)
        assert g[0][2] == money("500.00")


def test_brokerage_dip_keeps_unrealized_invariant():
    # A brokerage markdown runs through apply_growth: mv falls, the offset
    # lands in Equity:UnrealizedGains, basis is untouched, and the year-end
    # check_unrealized in Simulation._close passes without special-casing.
    engine = _run(_control(
        [{"type": "brokerage", "name": "Assets:Brk", "opening": "100000.00",
          "basis": "50000.00", "growth": "0.0", "asset_class": "equity"}],
        [{"name": "dip", "asset_class": "equity", "start": "2026-01",
          "months": 2, "drawdown": "0.25"}]))
    mv = engine.balance("Assets:Brk")
    assert abs(mv - money("75000.00")) <= money("0.02"), mv
    assert engine.balance(UNREALIZED) == -(mv - money("50000.00"))
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_override_shock_cancels_segment_growth():
    # mode="override" must cancel the rate apply_growth actually uses this
    # month -- the segment's, not the account's baseline attr.
    engine = _run(_control(
        [_ira(growth="0.06")],
        [{"name": "hot", "asset_class": "equity", "start": "2026-01",
          "months": 1, "growth": "0.24"}]) | {
        "years": 1, "shocks": [
            {"name": "cut", "from": "Assets:IRA", "to": "Expenses:Loss",
             "pct": "0.10", "mode": "override", "when": "2026-01-01"}]})
    jan = [t for t in engine.journal if t.date.month == 1
           and any(p.account == "Assets:IRA" for p in t.postings)
           and t.description != "Opening balances"]
    delta = sum((p.amount for t in jan for p in t.postings
                 if p.account == "Assets:IRA"), ZERO)
    assert delta == money("-10000.00"), delta


# --- mixins stack -----------------------------------------------------------

def test_mixins_stack_segments_by_name():
    root = {
        "scenarios": {"base": _control([_ira(growth="0.05")], [])},
        "mixins": {
            "dip_a": {"market": [{"name": "a", "asset_class": "equity",
                                  "start": "2026-02", "months": 1,
                                  "drawdown": "0.10"}]},
            "dip_b": {"market": [{"name": "b", "asset_class": "equity",
                                  "start": "2026-08", "months": 1,
                                  "drawdown": "0.05"}]},
        },
    }
    engine, sim, years = build_scenario(root, "base", ("dip_a", "dip_b"))
    sim.run(years)
    seq = {(m, _segment_of(d)) for _, m, _, d in _growth(engine, "Assets:IRA")
           if _segment_of(d)}
    assert seq == {(2, "a"), (8, "b")}


# --- validation -------------------------------------------------------------

def _seg(**kw):
    base = {"name": "s", "asset_class": "equity", "start": "2026-03"}
    base.update(kw)
    return base


def test_rejects_bad_segments():
    accts = [_ira()]
    _raises(_control(accts, [_seg(months=2, drawdwon="0.1")]), "unknown key")
    _raises(_control(accts, [_seg(months=2, drawdown="0.1", growth="0.02")]),
            "exactly one")
    _raises(_control(accts, [_seg(months=2)]), "exactly one")
    _raises(_control(accts, [_seg(drawdown="0.1")]), "needs 'months'")
    _raises(_control(accts, [_seg(months=2, drawdown="1.5")]), "between 0 and 1")
    _raises(_control(accts, [_seg(months=2, drawdown="-0.1")]), "between 0 and 1")
    _raises(_control(accts, [_seg(months=0, growth="0.02")]), "whole number")
    _raises(_control(accts, [_seg(months=True, growth="0.02")]), "whole number")
    _raises(_control(accts, [_seg(start="2026-03-01", growth="0.02")]),
            "year-month")
    _raises(_control(accts, [_seg(start="2026-13", growth="0.02")]),
            "year-month")


def test_rejects_unknown_asset_class():
    _raises(_control([_ira()], [_seg(asset_class="equitty", growth="0.02")]),
            "no account declares")


def test_rejects_asset_class_on_non_growing_account():
    _raises(_control(
        [{"type": "asset", "name": "Assets:Checking", "opening": "100.00",
          "asset_class": "equity"}], []), "only applies to")


def test_rejects_same_class_same_start():
    _raises(_control([_ira()], [
        _seg(name="x", growth="0.02"), _seg(name="y", growth="0.03")]),
        "same month")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
