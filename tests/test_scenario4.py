"""Scenario 4 checks — brokerage market-value drift (apply_growth) under the
Option-B invariant:  Equity:UnrealizedGains == -(mv - basis)  at all times.

Run directly:  python tests/test_scenario4.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money, ZERO


def _run(control, years=None):
    engine, sim, yrs = build(control)
    sim.run(yrs if years is None else years)
    return engine, sim


def _find(sim, name):
    for o in sim.objects:
        if getattr(o, "name", None) == name:
            return o
    raise KeyError(name)


def _unrealized_matches(engine, sim, name="Assets:Brokerage") -> bool:
    """The Option-B invariant: the bucket mirrors current (mv - basis)."""
    mv = engine.balance(name)
    basis = money(_find(sim, name).attrs["basis"])
    return engine.balance("Equity:UnrealizedGains") == -(mv - basis)


def _zero(engine):
    return sum(engine.balances().values(), ZERO) == ZERO


# --- A: opening splits embedded gain into basis + UnrealizedGains ----------

OPEN = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "200000.00", "basis": "120000.00"},
    ],
}


def test_opening_splits_basis_from_embedded_gain():
    # run(0): no months tick, so we inspect the day-zero snapshot alone.
    engine, sim = _run(OPEN, years=0)
    assert engine.balance("Assets:Brokerage") == money("200000.00")
    assert engine.balance("Equity:Opening") == money("-120000.00")   # basis
    assert engine.balance("Equity:UnrealizedGains") == money("-80000.00")
    assert _unrealized_matches(engine, sim)
    assert _zero(engine)


# --- B: growth drifts mv up, freezes basis, hits no income gate ------------

GROWTH = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "120000.00", "basis": "120000.00", "growth": "0.12"},
    ],
}


def test_growth_raises_mv_and_freezes_basis():
    engine, sim = _run(GROWTH)
    mv = engine.balance("Assets:Brokerage")
    # 12% annual compounded monthly (~1%/mo) lifts 120k past 135k.
    assert money("135000") < mv < money("135300")
    # basis is untouched by appreciation.
    assert money(_find(sim, "Assets:Brokerage").attrs["basis"]) == money("120000.00")


def test_growth_is_unrealized_not_income():
    engine, _ = _run(GROWTH)
    # Appreciation touches NO income gate and nothing is swept to retained.
    assert engine.accounts("Income:") == []
    assert engine.balance("Equity:RetainedEarnings") == ZERO


def test_unrealized_bucket_tracks_growth():
    engine, sim = _run(GROWTH)
    assert _unrealized_matches(engine, sim)
    assert _zero(engine)


def test_taxable_fraction_climbs_from_zero():
    engine, _ = _run(GROWTH)
    mv = engine.balance("Assets:Brokerage")
    gain = -engine.balance("Equity:UnrealizedGains")   # == mv - basis
    fraction = gain / mv
    # Started at 0 (basis == opening); a year of drift pushes it above 11%.
    assert fraction > money("0.11")


# --- C: no growth attr -> apply_growth stays a genuine no-op ----------------

FLAT = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "100000.00", "basis": "100000.00"},
    ],
}


def test_no_growth_attr_is_noop():
    engine, _ = _run(FLAT)
    assert engine.balance("Assets:Brokerage") == money("100000.00")
    # No appreciation was ever posted, so the bucket never materializes.
    assert "Equity:UnrealizedGains" not in engine.accounts()
    assert _zero(engine)


# --- D: growth THEN a sale — invariant survives realization -----------------

DRIFT_THEN_SELL = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
        {"type": "brokerage", "name": "Assets:Brokerage",
         "opening": "100000.00", "basis": "100000.00", "growth": "0.12"},
    ],
    "cash_management": {
        "account": "Assets:Checking", "floor": "10000.00", "target": "30000.00",
        "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}],
    },
}


def test_sale_unwinds_from_unrealized_and_stays_neutral():
    engine, sim = _run(DRIFT_THEN_SELL)
    # The realized slice moved OUT of UnrealizedGains; the bucket still equals
    # -(mv - basis) after both growth and the sale.
    assert _unrealized_matches(engine, sim)
    # A brokerage sale never uses the Recognized contra (that's IRA-only).
    assert engine.balance("Equity:Recognized") == ZERO
    # Some gain was realized and swept to retained earnings.
    assert engine.balance("Equity:RetainedEarnings") < ZERO


def test_networth_identity_holds_with_growth_and_sale():
    engine, _ = _run(DRIFT_THEN_SELL)
    assets = (engine.balance("Assets:Checking")
              + engine.balance("Assets:Brokerage"))
    equity = (engine.balance("Equity:Opening")
              + engine.balance("Equity:RetainedEarnings")
              + engine.balance("Equity:Recognized")
              + engine.balance("Equity:UnrealizedGains"))
    assert assets + equity == ZERO
    assert _zero(engine)


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))