"""Generator subtree: flows that manufacture Transactions forward.

Generator is an ABC that forces ``emit()``. You never enter transactions
directly — you declare generators, and the engine manufactures balanced
postings every period they fire.

Only the classes we actually implement live here. Per the design's aversion
to NotImplementedError scaffolding, the other planned subtypes are NOT stubbed:

  Schedule  (forced/planned: RMD, Roth conversion, withdrawal) -> later
  Shock     (one-off unmodeled event)                          -> later
  OneOff    (degenerate Schedule, verbatim postings)           -> later

The intermediate ``Policy`` grouping node (state-driven category) is likewise
deferred until a second policy type (dividends) exists to justify the
abstraction; for now InterestPolicy inherits Generator directly.
"""

from __future__ import annotations

from abc import abstractmethod
from decimal import Decimal

from .accounts import AssetAccount
from .engine import Engine
from .primitives import Posting, Transaction, ZERO, money
from .simobject import SimObject


class Generator(SimObject):
    @abstractmethod
    def emit(self, period, engine: Engine) -> list[Transaction]:
        """Return balanced transactions for this period (may be empty)."""
        raise NotImplementedError

    def step(self, period, engine: Engine) -> list[Transaction]:
        return self.emit(period, engine)


class Stream(Generator):
    """Scheduled EXTERNAL inflow: Social Security, pension, annuity.

    The money originates outside your accounts, so this is the simple
    single-pair shape — the Income gate legitimately IS the source leg:

        Assets:<to>       += amount
        Income:<gate>     -= amount

    Contrast with a funding withdrawal (money already yours), which needs a
    transfer pair PLUS a separate recognition pair. A Stream reads no ledger
    state; it emits a fixed monthly amount, gated by a start (claiming) month
    and an optional end month. Comparison is month-granular, consistent with
    the sim's "date precision beyond month is not modeled" stance: the day of
    ``start``/``end`` is ignored — only its (year, month) matters.

    The distinctly-named income gate (Income:SS, Income:Pension, ...) is what
    lets the future TaxEngine apply per-source rules (e.g. the ~85% SS
    taxability cap) without Stream itself knowing anything about tax.
    """

    def __init__(self, name: str, to: str, income_account: str, amount,
                 start=None, end=None, attrs=None):
        super().__init__(name, attrs)
        self.to = to
        self.income_account = income_account
        self.amount = money(amount)
        self.start = start   # datetime.date or None
        self.end = end       # datetime.date or None
        # ``owner`` is cross-cutting metadata, not structural wiring, so it
        # lives in attrs and is read at emit time — exactly as InterestPolicy
        # reads it off its source account. Constructor fields are reserved for
        # the wiring the Stream needs to function (to / income / amount / span).

    def _active(self, period) -> bool:
        pm = (period.year, period.month)
        if self.start is not None and pm < (self.start.year, self.start.month):
            return False
        if self.end is not None and pm > (self.end.year, self.end.month):
            return False
        return True

    def emit(self, period, engine: Engine) -> list[Transaction]:
        if self.amount == ZERO or not self._active(period):
            return []
        owner = self.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"{self.name} income",
                postings=[
                    Posting(self.to, self.amount, owner=owner,
                            meta={"kind": "stream", "stream": self.name}),
                    Posting(self.income_account, -self.amount, owner=owner,
                            meta={"kind": "stream", "stream": self.name}),
                ],
            )
        ]


class InterestPolicy(Generator):
    """State-driven monthly interest on a cash asset account.

    Reads the *current* balance of ``source`` each month (state-driven), applies
    the monthly rate (annual attrs['apr'] / 12), and recognizes it as income:
        Assets:<source>   += interest
        Income:<income>   -= interest
    The gate accrues all year and is swept to RetainedEarnings at close.

    "Current" now means "as of tick open": the accrue phase collects every
    object's emissions before posting any of them, so this reads a snapshot
    that excludes same-tick inflows (e.g. this month's Stream deposit).
    """

    def __init__(self, name: str, source: AssetAccount,
                 income_account: str = "Income:Interest", attrs=None):
        super().__init__(name, attrs)
        self.source = source
        self.income_account = income_account

    def _monthly_rate(self) -> Decimal:
        apr = money(self.source.attrs.get("apr", 0))
        return apr / Decimal(12)

    def emit(self, period, engine: Engine) -> list[Transaction]:
        balance = engine.balance(self.source.name)
        interest = money(balance * self._monthly_rate())
        if interest == ZERO:
            return []
        owner = self.source.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"Monthly interest @ {self.source.attrs.get('apr')} APR",
                postings=[
                    Posting(self.source.name, interest, owner=owner,
                            meta={"kind": "interest"}),
                    Posting(self.income_account, -interest, owner=owner,
                            meta={"kind": "interest"}),
                ],
            )
        ]


class Shock(Generator):
    """One-off unmodeled event: a single dated transaction that fires exactly
    once, in a given (year, month). The escape hatch for things outside any
    recurring rule — a new roof, a car, a medical bill, an inheritance.

    Shape is one balanced pair, ``to`` up and ``from`` down:

        <to>    += amount
        <from>  -= amount

    Semantics fall out of the accounts chosen, not from any flag:
      spending : from=Assets:Checking, to=Expenses:Roof   (asset down, expense
                 up -> swept to RetainedEarnings at close, net worth falls)
      windfall : from=Income:Inheritance, to=Assets:Checking (income gate down,
                 asset up -> net worth rises)

    A cash-draining Shock is also what makes the CashManager fire mid-run: it
    breaches the floor in one month instead of only at opening. Month-granular,
    like Stream: only the (year, month) of ``when`` matters, never the day.
    """

    def __init__(self, name: str, frm: str, to: str, amount, when, attrs=None):
        super().__init__(name, attrs)
        self.frm = frm
        self.to = to
        self.amount = money(amount)
        self.when = when   # datetime.date

    def emit(self, period, engine: Engine) -> list[Transaction]:
        if self.amount == ZERO:
            return []
        if (period.year, period.month) != (self.when.year, self.when.month):
            return []
        owner = self.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"{self.name} (shock)",
                postings=[
                    Posting(self.to, self.amount, owner=owner,
                            meta={"kind": "shock", "shock": self.name}),
                    Posting(self.frm, -self.amount, owner=owner,
                            meta={"kind": "shock", "shock": self.name}),
                ],
            )
        ]