"""Declarative control file -> object graph.

You declare accounts and generators; the loader wires them up, seeds opening
balances against the frozen Equity:Opening snapshot, and hands back a ready
Simulation. History is never backfilled — opening balances are the only
day-zero postings.

The file (all_scenarios.json) holds many named scenarios under "scenarios",
with `extends` inheritance resolved by the scenarios module. `build_scenario`
resolves a named scenario to one flat dict, then `build` turns that flat dict
into the object graph. A flat resolved scenario looks like:

    {
      "start": "2026-01-01",
      "years": 1,
      "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00", "apr": "0.03"}
      ],
      "streams": [
        {"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
         "amount": "3000.00", "start": "2026-01-01", "owner": "chuck"}
      ]
    }

An asset account with a non-zero "apr" automatically gets an InterestPolicy.
A ``streams`` entry becomes a Stream generator (external inflow: SS, pension,
annuity). A ``schedules`` entry becomes a Schedule generator (forced/planned
withdrawal: RMD or a fixed planned pull), resolving its ``source`` against
the account registry so it reuses that account's own ``fund_from`` shape.
Income gates (Income:Interest, Income:SS, ...) are not declared as accounts;
they materialize in the ledger the moment income first posts.
"""

from __future__ import annotations

import json
from datetime import date

from .accounts import (
    AssetAccount, BrokerageAccount, LiabilityAccount, RothAccount,
    TraditionalIRAAccount, UNREALIZED,
)
from .cashmanager import CashManager, Source
from .taxengine import TaxEngine, DEFAULT_STD, DEFAULT_SS_INCLUSION
from .engine import Engine
from .generators import InterestPolicy, Schedule, Shock, Stream
from .primitives import Posting, Transaction, ZERO, money
from .scenarios import resolve
from .simobject import SimObject
from .simulation import Simulation

OPENING = "Equity:Opening"

_ACCOUNT_TYPES = {
    "asset": AssetAccount,
    "liability": LiabilityAccount,
    "brokerage": BrokerageAccount,
    "traditional_ira": TraditionalIRAAccount,
    "roth": RothAccount,
}

# Keys consumed directly by the Stream constructor; anything else on a stream
# spec (owner, and any future cross-cutting tags) falls through into its attrs
# dict, keeping the schema open and matching how accounts carry owner.
_STREAM_KEYS = ("name", "to", "income", "amount", "start", "end")
# `from` is a Python keyword in the spec -> mapped to `frm` on the object.
_SHOCK_KEYS = ("name", "from", "to", "amount", "when")
# `source` is resolved through the registry (like a cash_management waterfall
# rung), not passed as a string, so it is consumed here rather than falling
# through to attrs.
_SCHEDULE_KEYS = ("name", "source", "to", "mode", "amount", "month",
                  "owner_birth_year", "rmd_start_age", "divisors",
                  "start", "end")


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _build_stream(spec: dict) -> Stream:
    """Turn one ``streams`` entry into a Stream generator."""
    attrs = {k: v for k, v in spec.items() if k not in _STREAM_KEYS}
    return Stream(
        name=spec["name"],
        to=spec["to"],
        income_account=spec["income"],
        amount=spec["amount"],
        start=_parse_date(spec["start"]) if spec.get("start") else None,
        end=_parse_date(spec["end"]) if spec.get("end") else None,
        attrs=attrs,
    )


def _build_cash_manager(spec: dict, registry: dict) -> CashManager:
    """Wire a CashManager from the 'cash_management' block, resolving each
    waterfall source NAME to the actual account object (so the manager can
    read basis / withholding and mutate basis on a sale).
    """
    waterfall = []
    for rung in spec.get("waterfall", []):
        name = rung["source"]
        if name not in registry:
            raise KeyError(f"waterfall source {name!r} is not a declared account")
        waterfall.append(Source(account=registry[name],
                                floor=money(rung.get("floor", 0))))
    return CashManager(
        cash_account=spec["account"],
        floor=spec["floor"],
        target=spec["target"],
        waterfall=waterfall,
        trigger=spec.get("trigger", "cash-floor"),
    )


def _build_shock(spec: dict) -> Shock:
    """Turn one ``shocks`` entry into a Shock generator (one-off event)."""
    attrs = {k: v for k, v in spec.items() if k not in _SHOCK_KEYS}
    return Shock(
        name=spec["name"],
        frm=spec["from"],
        to=spec["to"],
        amount=spec["amount"],
        when=_parse_date(spec["when"]),
        attrs=attrs,
    )


def _build_schedule(spec: dict, registry: dict) -> Schedule:
    """Turn one ``schedules`` entry into a Schedule generator, resolving
    ``source`` to the actual account object (so fund_from/basis/withholding
    are the live account state, same as the cash-management waterfall)."""
    name = spec["source"]
    if name not in registry:
        raise KeyError(f"schedule source {name!r} is not a declared account")
    attrs = {k: v for k, v in spec.items() if k not in _SCHEDULE_KEYS}
    return Schedule(
        name=spec["name"],
        source=registry[name],
        to=spec["to"],
        mode=spec["mode"],
        amount=spec.get("amount"),
        month=spec.get("month", 1),
        owner_birth_year=spec.get("owner_birth_year"),
        rmd_start_age=spec.get("rmd_start_age", 73),
        divisors=spec.get("divisors"),
        start=_parse_date(spec["start"]) if spec.get("start") else None,
        end=_parse_date(spec["end"]) if spec.get("end") else None,
        attrs=attrs,
    )


def _build_tax_engine(spec: dict, control: dict) -> TaxEngine:
    """Wire a TaxEngine from the 'tax' block. Cash for settlement defaults to
    the cash-management account, else Assets:Checking."""
    cash = spec.get("cash_account") or control.get(
        "cash_management", {}).get("account", "Assets:Checking")
    return TaxEngine(
        cash_account=cash,
        brackets=spec.get("brackets"),
        ltcg_brackets=spec.get("ltcg_brackets"),
        std_deduction=spec.get("std_deduction", DEFAULT_STD),
        ss_inclusion=spec.get("ss_inclusion", DEFAULT_SS_INCLUSION),
        settle_month=spec.get("settle_month", 4),
    )


def build(control: dict) -> tuple[Engine, Simulation, int]:
    """Build (engine, simulation, years) from a parsed control dict."""
    engine = Engine()
    objects: list[SimObject] = []
    opening_legs: list[Posting] = []       # asset legs (full market value)
    gain_legs: list[Posting] = []          # embedded unrealized-gain legs
    registry: dict = {}   # name -> account object, for the cash-manager wiring

    for spec in control.get("accounts", []):
        cls = _ACCOUNT_TYPES[spec["type"]]
        attrs = {k: v for k, v in spec.items()
                 if k not in ("type", "name", "opening")}
        account = cls(name=spec["name"], attrs=attrs)
        objects.append(account)
        registry[account.name] = account

        opening = money(spec.get("opening", 0))

        # A brokerage always carries an explicit basis so both the opening
        # split and fund_from read the same value; default it to the full
        # opening (a freshly bought lot with no embedded gain) when unset.
        if isinstance(account, BrokerageAccount) and "basis" not in account.attrs:
            account.attrs["basis"] = opening

        if opening != ZERO:
            opening_legs.append(
                Posting(account.name, opening, owner=attrs.get("owner"),
                        meta={"kind": "opening"})
            )
            # Embedded day-zero gain rides into Equity:UnrealizedGains so the
            # invariant UnrealizedGains == -(mv - basis) holds from t0; only the
            # basis portion lands against Equity:Opening (handled by the contra).
            if isinstance(account, BrokerageAccount):
                embedded = money(opening - money(account.attrs["basis"]))
                if embedded != ZERO:
                    gain_legs.append(
                        Posting(UNREALIZED, -embedded, owner=attrs.get("owner"),
                                meta={"kind": "opening"})
                    )

        if isinstance(account, AssetAccount) and money(attrs.get("apr", 0)) != ZERO:
            objects.append(
                InterestPolicy(name=f"interest:{account.name}", source=account)
            )

    for spec in control.get("streams", []):
        objects.append(_build_stream(spec))

    for spec in control.get("shocks", []):
        objects.append(_build_shock(spec))

    # Schedules resolve `source` against the registry, so this must run after
    # the accounts loop above has populated it (same ordering constraint as
    # the cash-management waterfall, below).
    for spec in control.get("schedules", []):
        objects.append(_build_schedule(spec, registry))

    # One balanced opening transaction. Equity:Opening absorbs BASIS (the asset
    # legs net of the embedded-gain legs); Equity:UnrealizedGains absorbs the
    # embedded gain. The whole thing still sums to zero on day zero.
    if opening_legs:
        legs = opening_legs + gain_legs
        contra = -sum((p.amount for p in legs), ZERO)
        legs.append(Posting(OPENING, contra, meta={"kind": "opening"}))
        engine.post(
            Transaction(
                date=_parse_date(control["start"]),
                description="Opening balances",
                postings=legs,
            )
        )

    cash_manager = None
    if "cash_management" in control:
        cash_manager = _build_cash_manager(control["cash_management"], registry)

    tax_engine = None
    if "tax" in control:
        tax_engine = _build_tax_engine(control["tax"], control)

    sim = Simulation(engine=engine, objects=objects,
                     start=_parse_date(control["start"]),
                     cash_manager=cash_manager, tax_engine=tax_engine)
    return engine, sim, int(control.get("years", 1))


def build_scenario(root: dict, name: str) -> tuple[Engine, Simulation, int]:
    """Resolve ``name``'s extends chain into a flat scenario, then build it."""
    return build(resolve(root.get("scenarios", {}), name))


def load(path: str, scenario: str) -> tuple[Engine, Simulation, int]:
    """Load all_scenarios.json and build the named scenario."""
    with open(path) as fh:
        return build_scenario(json.load(fh), scenario)