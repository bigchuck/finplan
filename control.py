"""Declarative control file -> object graph."""

from __future__ import annotations

import json
from datetime import date

from .accounts import (
    AssetAccount, BrokerageAccount, LiabilityAccount, RothAccount,
    TraditionalIRAAccount, UNREALIZED,
)
from .cashmanager import CashManager, Source
from .taxengine import (
    TaxEngine, DEFAULT_SAFE_HARBOR, DEFAULT_STD, DEFAULT_SS_INCLUSION,
)
from .engine import Engine
from .generators import DividendPolicy, InterestPolicy, Schedule, Shock, Stream
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

_STREAM_KEYS = ("name", "to", "income", "amount", "start", "end")
_SHOCK_KEYS = ("name", "from", "to", "amount", "when")
_SCHEDULE_KEYS = ("name", "source", "to", "mode", "amount", "month",
                  "owner_birth_year", "rmd_start_age", "divisors",
                  "start", "end")


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _build_stream(spec: dict) -> Stream:
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


def _build_dividend_policy(account: BrokerageAccount, control: dict) -> DividendPolicy:
    """Derive a DividendPolicy from a brokerage account's own attrs, mirroring
    how InterestPolicy is derived from an asset account's ``apr``.

    ``reinvest`` defaults to true (the common accumulation-phase case). When
    false the cash needs somewhere to land: an explicit ``dividend_to`` wins,
    else the cash-management account, else it's an error — inventing a
    destination account would be exactly the kind of quiet wrongness this
    model exists to avoid.
    """
    attrs = account.attrs
    reinvest = attrs.get("reinvest", True)
    to = attrs.get("dividend_to") or control.get(
        "cash_management", {}).get("account")
    if not reinvest and not to:
        raise ValueError(
            f"account {account.name!r}: reinvest=false needs a 'dividend_to' "
            f"account (or a cash_management block to default to)")
    return DividendPolicy(
        name=f"dividend:{account.name}",
        source=account,
        qualified_fraction=attrs.get("qualified_fraction", 1),
        reinvest=bool(reinvest),
        to=to,
    )


def _build_cash_manager(spec: dict, registry: dict) -> CashManager:
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
    cash = spec.get("cash_account") or control.get(
        "cash_management", {}).get("account", "Assets:Checking")
    est = spec.get("estimates") or {}
    return TaxEngine(
        cash_account=cash,
        brackets=spec.get("brackets"),
        ltcg_brackets=spec.get("ltcg_brackets"),
        std_deduction=spec.get("std_deduction", DEFAULT_STD),
        ss_inclusion=spec.get("ss_inclusion", DEFAULT_SS_INCLUSION),
        settle_month=spec.get("settle_month", 4),
        gate_character=spec.get("gate_character"),
        gate_prefixes=[tuple(p) for p in spec["gate_prefixes"]]
                      if spec.get("gate_prefixes") else None,
        # Presence of the "estimates" block is the switch: absent means off,
        # so every pre-S9 control file keeps its exact cash path.
        estimates=bool(est),
        safe_harbor_multiple=est.get("safe_harbor_multiple",
                                     DEFAULT_SAFE_HARBOR),
        credit_withholding=est.get("credit_withholding", True),
        prior_year_tax=est.get("prior_year_tax", 0),
        prior_year_withholding=est.get("prior_year_withholding", 0),
        # Same opt-in discipline: absent "state" means no state accrual at
        # all, so pre-S9.5 control files keep their exact cash path.
        state=spec.get("state"),
        deductions=spec.get("deductions"),
    )


def build(control: dict) -> tuple[Engine, Simulation, int]:
    engine = Engine()
    objects: list[SimObject] = []
    opening_legs: list[Posting] = []
    gain_legs: list[Posting] = []
    registry: dict = {}

    for spec in control.get("accounts", []):
        cls = _ACCOUNT_TYPES[spec["type"]]
        attrs = {k: v for k, v in spec.items()
                 if k not in ("type", "name", "opening")}
        account = cls(name=spec["name"], attrs=attrs)
        objects.append(account)
        registry[account.name] = account

        opening = money(spec.get("opening", 0))

        if isinstance(account, BrokerageAccount) and "basis" not in account.attrs:
            account.attrs["basis"] = opening

        if opening != ZERO:
            opening_legs.append(
                Posting(account.name, opening, owner=attrs.get("owner"),
                        meta={"kind": "opening"})
            )
            if isinstance(account, BrokerageAccount):
                embedded = money(opening - money(account.attrs["basis"]))
                if embedded != ZERO:
                    gain_legs.append(
                        Posting(UNREALIZED, -embedded, owner=attrs.get("owner"),
                                meta={"kind": "opening"})
                    )

        if isinstance(account, BrokerageAccount) and money(attrs.get("dividend_yield", 0)) != ZERO:
            objects.append(_build_dividend_policy(account, control))
        elif isinstance(account, AssetAccount) and money(attrs.get("apr", 0)) != ZERO:
            objects.append(
                InterestPolicy(name=f"interest:{account.name}", source=account)
            )

    for spec in control.get("streams", []):
        objects.append(_build_stream(spec))

    for spec in control.get("shocks", []):
        objects.append(_build_shock(spec))

    for spec in control.get("schedules", []):
        objects.append(_build_schedule(spec, registry))

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
    return build(resolve(root.get("scenarios", {}), name))


def load(path: str, scenario: str) -> tuple[Engine, Simulation, int]:
    with open(path) as fh:
        return build_scenario(json.load(fh), scenario)