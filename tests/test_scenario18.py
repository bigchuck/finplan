"""Scenario 18 checks -- percentage-mode Shock (the market-crash case):
Shock's existing 'amount'/'frm'/'to' shape gains a 'pct' alternative that
sizes the transfer off the source account's live balance instead of a
declared dollar figure, plus a 'mode' switch for how it interacts with that
account's own growth the same period ("compound" nets additively with
growth; "override" cancels growth so pct is the only change that month).
Also covers the brokerage-specific fallout: a pct shock is a plain
transfer, not a sale, so it leaves basis untouched and would otherwise trip
the year-end UnrealizedGains invariant -- accounts.unrealized_gap now nets
out Shock-tagged drain to account for it.

Run directly:  python tests/test_scenario18.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.control import build
from finplan.primitives import money


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _shock_txn(engine, name):
    return next(t for t in engine.journal
                if t.description == f"{name} (shock)")


# A single-period run (start month == December, years=1) isolates the shock
# month from any further growth, so the resulting balance is exact hand-math.
def _one_month_control(pct, mode, acct_type, growth="0.07", extra_attrs=None):
    acct = {"type": acct_type, "name": "Assets:Target", "owner": "chuck",
            "opening": "100000.00", "growth": growth}
    if acct_type == "brokerage":
        acct["basis"] = "50000.00"
    if extra_attrs:
        acct.update(extra_attrs)
    return {
        "start": "2026-12-01", "years": 1,
        "accounts": [acct],
        "shocks": [
            {"name": "crash", "from": "Assets:Target",
             "to": "Expenses:BigKahunaInTheSky",
             "pct": pct, "when": "2026-12-01", "mode": mode, "owner": "chuck"},
        ],
    }


# --- A: compound mode nets additively with that month's own growth ----------

def test_compound_mode_nets_with_growth_on_brokerage():
    # growth: 100000 * 0.07/12 = 583.33 ; shock: 100000 * 0.30 = 30000.00
    # net: 100000 + 583.33 - 30000.00 = 70583.33
    engine, sim = _run(_one_month_control("0.30", "compound", "brokerage"))
    assert engine.balance("Assets:Target") == money("70583.33")


def test_compound_mode_same_math_on_ira_and_roth():
    for acct_type in ("traditional_ira", "roth"):
        engine, sim = _run(_one_month_control("0.30", "compound", acct_type))
        assert engine.balance("Assets:Target") == money("70583.33"), acct_type


# --- B: override mode cancels growth -- pct is the only change this month ---

def test_override_mode_ignores_growth_on_brokerage():
    engine, sim = _run(_one_month_control("0.30", "override", "brokerage"))
    assert engine.balance("Assets:Target") == money("70000.00")


def test_override_mode_same_math_on_ira_and_roth():
    for acct_type in ("traditional_ira", "roth"):
        engine, sim = _run(_one_month_control("0.30", "override", acct_type))
        assert engine.balance("Assets:Target") == money("70000.00"), acct_type


# --- C: it's a plain transfer -- destination is credited, basis untouched ---

def test_shock_amount_lands_on_the_named_destination():
    engine, sim = _run(_one_month_control("0.30", "compound", "brokerage"))
    txn = _shock_txn(engine, "crash")
    postings = {p.account: p.amount for p in txn.postings}
    assert postings["Expenses:BigKahunaInTheSky"] == money("30000.00")
    assert postings["Assets:Target"] == money("-30000.00")


def test_brokerage_basis_is_untouched_by_a_crash():
    engine, sim = _run(_one_month_control("0.30", "compound", "brokerage"))
    acct = next(o for o in sim.objects
               if getattr(o, "name", None) == "Assets:Target")
    assert money(acct.attrs["basis"]) == money("50000.00")


# --- D: a brokerage crash keeps the UnrealizedGains invariant across years --

MULTI_YEAR_CRASH = {
    "start": "2026-01-01", "years": 5,
    "accounts": [
        {"type": "brokerage", "name": "Assets:Brokerage", "owner": "chuck",
         "opening": "100000.00", "basis": "50000.00", "growth": "0.07"},
    ],
    "shocks": [
        {"name": "crash_2027", "from": "Assets:Brokerage",
         "to": "Expenses:BigKahunaInTheSky", "pct": "0.30",
         "when": "2027-03-01", "mode": "compound", "owner": "chuck"},
        {"name": "crash_2029", "from": "Assets:Brokerage",
         "to": "Expenses:BigKahunaInTheSky", "pct": "0.15",
         "when": "2029-06-01", "mode": "override", "owner": "chuck"},
    ],
}


def test_multi_year_run_with_two_brokerage_crashes_holds_invariant():
    # UnrealizedInvariantError propagates out of sim.run() at whichever
    # year-end first sees drift, so simply completing the run is the
    # assertion -- accounts.check_unrealized fires at every year-end close.
    engine, sim = _run(MULTI_YEAR_CRASH)
    assert engine.balance("Assets:Brokerage") > money("0")


# --- E: build-time validation -------------------------------------------

def _shock_control(shock_spec):
    return {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "brokerage", "name": "Assets:Brokerage",
             "opening": "100000.00", "basis": "50000.00", "growth": "0.07"},
        ],
        "shocks": [shock_spec],
    }


def test_amount_and_pct_together_is_rejected():
    spec = {"name": "bad", "from": "Assets:Brokerage", "to": "Expenses:X",
            "amount": "1000.00", "pct": "0.10", "when": "2026-01-01"}
    try:
        build(_shock_control(spec))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "'amount' or 'pct'" in str(e)


def test_neither_amount_nor_pct_is_rejected():
    spec = {"name": "bad", "from": "Assets:Brokerage", "to": "Expenses:X",
            "when": "2026-01-01"}
    try:
        build(_shock_control(spec))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "'amount' or 'pct'" in str(e)


def test_pct_shock_from_an_undeclared_account_is_rejected():
    spec = {"name": "bad", "from": "Assets:Nope", "to": "Expenses:X",
            "pct": "0.10", "when": "2026-01-01"}
    try:
        build(_shock_control(spec))
        assert False, "expected KeyError"
    except KeyError as e:
        assert "Assets:Nope" in str(e)


def test_unknown_mode_is_rejected():
    spec = {"name": "bad", "from": "Assets:Brokerage", "to": "Expenses:X",
            "pct": "0.10", "mode": "sideways", "when": "2026-01-01"}
    try:
        build(_shock_control(spec))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "sideways" in str(e)


# --- F: existing dollar-amount shocks are unaffected -------------------

def test_dollar_amount_shock_still_works_unchanged():
    control = {
        "start": "2026-01-01", "years": 1,
        "accounts": [
            {"type": "asset", "name": "Assets:Checking", "opening": "5000.00"},
        ],
        "shocks": [
            {"name": "medical", "from": "Assets:Checking",
             "to": "Expenses:Medical", "amount": "1200.00",
             "when": "2026-02-01", "owner": "chuck"},
        ],
    }
    engine, sim = _run(control)
    assert engine.balance("Assets:Checking") == money("3800.00")


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
