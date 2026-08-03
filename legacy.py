"""Estate: the death-and-legacy actor (Scenario 11).

Death is not a flow. It emits no transaction of its own — it MUTATES the
object graph, and on the terminal death it halts the run and prices what is
left. That is why Estate is a reactive actor alongside CashManager and
TaxEngine rather than a Generator: a Generator's entire contract is to
manufacture postings, and this manufactures none.

It runs in a fourth phase, ``_transition``, at the TOP of the tick, ahead of
accrue. The ordering is the whole point: a benefit that stops in June has to
be stopped before June's accrue emits it, not after.

WHAT A DEATH DOES
-----------------
1. Social Security, CROSS-STREAM. The survivor keeps the LARGER of the two
   benefits; the smaller one stops. No per-stream fraction can express that,
   because the rule compares two streams to each other, so SS is special-
   cased: the largest SS stream is retitled to the survivor and every other
   SS stream is zeroed.

2. Everything else the decedent owned, PER-STREAM. A pension or annuity
   continues at its own ``survivorship`` fraction — 1.0 for a full joint-and-
   survivor annuity, 0.5 for a half, 0 for single-life. Zero is the DEFAULT,
   deliberately the harshest of the three: a survivorship that quietly
   defaulted to full continuation would flatter every plan that forgot to
   state one.

3. Filing status, the FOLLOWING tax year. A widow(er) may still file jointly
   for the year of the death and files Single from the next one, so the
   switch is scheduled for January of year+1 rather than applied on the spot.
   This is the largest cash effect of a first death — the same real income
   meeting narrower bands against a smaller standard deduction — and it
   arrives a year late, which is exactly the sort of thing a model should get
   right and a spreadsheet gets wrong.

THE TERMINAL DEATH
------------------
A death that names a ``survivor`` continues the plan; a death that names none
ENDS it, and the run halts with the estate valued on the ledger as it stands
at the top of that tick. The rule is read off data the control file already
carries rather than inferred from how many deaths were declared — inferring
it ("halt once every declared death has fired") makes a one-death file mean
two opposite things depending on what the author had in mind, and picks the
wrong one about half the time. Halting rather than running on with everything
zeroed is honest about what the model knows: past the last death there is no
plan left to project, only an estate to distribute.

DEBT DOES NOT DIE
-----------------
A HELOC balance is a first claim on the estate, so the report states assets
and debts separately and nets them, and flags a negative net as INSOLVENT.
Collapsing the two into one "bequest" figure would hide the case where the
heirs inherit nothing at all.

NET TO HEIRS
------------
A dollar in a traditional IRA is not a dollar to an heir: it carries the
income tax that was deferred, and no step-up erases it (income in respect of
a decedent). A dollar of appreciated brokerage IS a full dollar, because
basis steps up to market value and the embedded gain is forgiven outright. A
Roth dollar is a full dollar. So the report gives gross AND a net figure that
charges ``heir_rate`` against the tax-deferred balances only. Gross alone
would make an IRA and a brokerage account of equal size look equally good to
leave behind — precisely the lever basis tracking exists to expose.

DEFERRED (named): account retitling and the step-up POSTING (the report
prices the step-up, the ledger does not yet post it), household expense
reduction on a first death, qualifying-surviving-spouse status where a
dependent is at home, estate tax, and probate costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .accounts import TraditionalIRAAccount, rate
from .engine import Engine
from .generators import Stream
from .primitives import Decimal, ZERO, money

SS_GATE = "Income:SS"


@dataclass
class Death:
    owner: str
    when: date
    survivor: str | None = None
    fired: bool = False


@dataclass
class BequestReport:
    """Priced at the top of the terminal tick, before that month accrues."""
    date: date
    assets: Decimal
    debts: Decimal          # negative, per the sign convention
    net: Decimal
    deferred: Decimal       # balances that carry deferred income tax to heirs
    heir_rate: Decimal
    heir_tax: Decimal
    net_to_heirs: Decimal
    insolvent: bool


class Estate:
    def __init__(self, deaths: list[Death], objects: list, tax_engine=None,
                 heir_rate=0, single: dict | None = None):
        self.deaths = sorted(deaths, key=lambda d: (d.when.year, d.when.month))
        self.objects = objects
        self.tax_engine = tax_engine
        self.heir_rate = rate(heir_rate)
        self.single = single
        self.log: list[str] = []
        self.bequest: BequestReport | None = None
        self.halted_at: date | None = None
        self._status_change_year: int | None = None
        # Streams whose survivorship has already been applied. A fraction is
        # a ONE-SHOT rule at the death of the original annuitant: once the
        # stream is retitled to the survivor, a later death of that survivor
        # must STOP it, not multiply the fraction in a second time.
        self._adjusted: set[int] = set()

    # --- the transition phase ----------------------------------------------

    def transition(self, period, engine: Engine) -> bool:
        """Top-of-tick phase. Returns True if the run should halt here."""
        if (self._status_change_year is not None
                and period.year >= self._status_change_year):
            self._change_filing_status(period)

        for d in self.deaths:
            if d.fired:
                continue
            if (period.year, period.month) < (d.when.year, d.when.month):
                continue
            d.fired = True
            self._apply(d, period)
            if d.survivor is None:
                self._value(period, engine)
                self.halted_at = period.date
                return True
        return False

    def _apply(self, d: Death, period) -> None:
        streams = [o for o in self.objects if isinstance(o, Stream)]
        ss = [s for s in streams if s.income_account.startswith(SS_GATE)]
        ss_ids = {id(s) for s in ss}

        # (1) SS: keep the larger benefit, retitled to the survivor.
        if ss and any(s.attrs.get("owner") == d.owner for s in ss):
            keeper = max(ss, key=lambda s: s.amount)
            if d.survivor:
                keeper.attrs["owner"] = d.survivor
            for s in ss:
                if s is keeper:
                    continue
                if s.amount != ZERO:
                    self.log.append(
                        f"{period.date}  {d.owner} died: SS '{s.name}' "
                        f"{s.amount:,.2f} -> 0.00 (survivor keeps the larger "
                        f"benefit, '{keeper.name}' {keeper.amount:,.2f})")
                s.amount = ZERO

        # (2) Everything else the decedent owned: per-stream survivorship.
        for s in streams:
            if id(s) in ss_ids or s.attrs.get("owner") != d.owner:
                continue
            before = s.amount
            if id(s) in self._adjusted:
                # Already reduced once and retitled to this holder; there is
                # no further survivor to continue for.
                s.amount = ZERO
                note = "survivorship already applied"
            else:
                frac = rate(s.attrs.get("survivorship", 0))
                s.amount = money(s.amount * frac)
                self._adjusted.add(id(s))
                note = f"survivorship {frac}"
            if d.survivor:
                s.attrs["owner"] = d.survivor
            self.log.append(
                f"{period.date}  {d.owner} died: '{s.name}' {before:,.2f} -> "
                f"{s.amount:,.2f} ({note})")

        # (3) Filing status flips NEXT tax year, not this one.
        if self.tax_engine is not None:
            self._status_change_year = d.when.year + 1

    def _change_filing_status(self, period) -> None:
        year = self._status_change_year
        self._status_change_year = None
        if self.tax_engine is None:
            return
        self.tax_engine.set_filing_status("single", **(self.single or {}))
        self.log.append(
            f"{period.date}  filing status MFJ -> Single for TY{year} onward "
            f"(standard deduction now {self.tax_engine.std_deduction:,.2f})")

    # --- valuation ---------------------------------------------------------

    def _value(self, period, engine: Engine) -> None:
        assets = sum(engine.balances("Assets:").values(), ZERO)
        debts = sum(engine.balances("Liabilities:").values(), ZERO)
        net = money(assets + debts)
        deferred = ZERO
        for o in self.objects:
            if isinstance(o, TraditionalIRAAccount):
                deferred = money(deferred + engine.balance(o.name))
        heir_tax = money(deferred * self.heir_rate)
        self.bequest = BequestReport(
            date=period.date, assets=money(assets), debts=money(debts),
            net=net, deferred=deferred, heir_rate=self.heir_rate,
            heir_tax=heir_tax, net_to_heirs=money(net - heir_tax),
            insolvent=net < ZERO,
        )

    # --- reporting ---------------------------------------------------------

    def report(self) -> str:
        lines = ["Death and legacy:"]
        if self.log:
            lines.extend(f"  {entry}" for entry in self.log)
        else:
            lines.append("  No death fired during the run.")
        b = self.bequest
        if b is None:
            lines.append("  Run reached its horizon with a survivor still "
                         "living; no estate valued.")
            return "\n".join(lines)
        lines.append(f"  {self.halted_at}  RUN HALTED: last declared death.")
        lines.append(f"  Estate at {b.date}:")
        lines.append(f"      assets            {b.assets:>16,.2f}")
        lines.append(f"      debts             {b.debts:>16,.2f}")
        lines.append(f"      net estate        {b.net:>16,.2f}")
        lines.append(f"      tax-deferred      {b.deferred:>16,.2f}"
                     f"   (IRD, no step-up)")
        lines.append(f"      heir tax @ {b.heir_rate}  {b.heir_tax:>16,.2f}")
        lines.append(f"      NET TO HEIRS      {b.net_to_heirs:>16,.2f}")
        if b.insolvent:
            lines.append("      *** INSOLVENT: debts exceed assets; the "
                         "heirs inherit nothing. ***")
        return "\n".join(lines)
