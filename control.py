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
annuity). Income gates (Income:Interest, Income:SS, ...) are not declared as
accounts; they materialize in the ledger the moment income first posts.
"""

from __future__ import annotations

import json
from datetime import date

from .accounts import AssetAccount, LiabilityAccount
from .engine import Engine
from .generators import InterestPolicy, Stream
from .primitives import Posting, Transaction, ZERO, money
from .scenarios import resolve
from .simobject import SimObject
from .simulation import Simulation

OPENING = "Equity:Opening"

_ACCOUNT_TYPES = {
    "asset": AssetAccount,
    "liability": LiabilityAccount,
}

# Keys consumed directly by the Stream constructor; anything else on a stream
# spec (owner, and any future cross-cutting tags) falls through into its attrs
# dict, keeping the schema open and matching how accounts carry owner.
_STREAM_KEYS = ("name", "to", "income", "amount", "start", "end")


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


def build(control: dict) -> tuple[Engine, Simulation, int]:
    """Build (engine, simulation, years) from a parsed control dict."""
    engine = Engine()
    objects: list[SimObject] = []
    opening_legs: list[Posting] = []

    for spec in control.get("accounts", []):
        cls = _ACCOUNT_TYPES[spec["type"]]
        attrs = {k: v for k, v in spec.items()
                 if k not in ("type", "name", "opening")}
        account = cls(name=spec["name"], attrs=attrs)
        objects.append(account)

        opening = money(spec.get("opening", 0))
        if opening != ZERO:
            opening_legs.append(
                Posting(account.name, opening, owner=attrs.get("owner"),
                        meta={"kind": "opening"})
            )

        if isinstance(account, AssetAccount) and money(attrs.get("apr", 0)) != ZERO:
            objects.append(
                InterestPolicy(name=f"interest:{account.name}", source=account)
            )

    for spec in control.get("streams", []):
        objects.append(_build_stream(spec))

    # One balanced opening transaction: assets in, Equity:Opening as the
    # contra so the day-zero snapshot itself sums to zero.
    if opening_legs:
        contra = -sum((p.amount for p in opening_legs), ZERO)
        opening_legs.append(Posting(OPENING, contra, meta={"kind": "opening"}))
        engine.post(
            Transaction(
                date=_parse_date(control["start"]),
                description="Opening balances",
                postings=opening_legs,
            )
        )

    sim = Simulation(engine=engine, objects=objects,
                     start=_parse_date(control["start"]))
    return engine, sim, int(control.get("years", 1))


def build_scenario(root: dict, name: str) -> tuple[Engine, Simulation, int]:
    """Resolve ``name``'s extends chain into a flat scenario, then build it."""
    return build(resolve(root.get("scenarios", {}), name))


def load(path: str, scenario: str) -> tuple[Engine, Simulation, int]:
    """Load all_scenarios.json and build the named scenario."""
    with open(path) as fh:
        return build_scenario(json.load(fh), scenario)