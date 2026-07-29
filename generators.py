"""Generator subtree: flows that manufacture Transactions forward.

Generator is an ABC that forces ``emit()``. You never enter transactions
directly — you declare generators, and the engine manufactures balanced
postings every period they fire.

Only the classes we actually implement live here. Per the design's aversion
to NotImplementedError scaffolding, the other planned subtypes are NOT stubbed:

  Stream    (scheduled external inflow: SS, pension, annuity)  -> Scenario 2
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


class InterestPolicy(Generator):
    """State-driven monthly interest on a cash asset account.

    Reads the *current* balance of ``source`` each month (state-driven), applies
    the monthly rate (annual attrs['apr'] / 12), and recognizes it as income:
        Assets:<source>   += interest
        Income:<income>   -= interest
    The gate accrues all year and is swept to RetainedEarnings at close.
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