"""The period loop.

Monthly tick. Three phases per tick, in a fixed order to keep stock/flow
ordering bugs from hiding:

  accrue -> every SimObject.step() runs; transactions are posted
  fund   -> CashManager.cover_shortfall (runs AFTER accrual so it sees net
            cash). No-op until Scenario 3.
  assess -> year-end only: TaxEngine.settle reads the Income gates.
            No-op until the tax milestone.

Separately, in December the *closing entries* sweep nominal accounts
(Income:*, later Expense:*) to Equity:RetainedEarnings, resetting the gates
for the next year. Assets/Liabilities/Equity ride across the boundary.

Within accrue, objects are processed in list order and each transaction is
posted as produced, so a later object reads balances reflecting earlier ones.
For Scenario 1 the ordering is inert (one generator); it is documented here so
the choice is explicit rather than accidental.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .engine import Engine
from .primitives import Posting, Transaction, ZERO
from .simobject import SimObject

RETAINED = "Equity:RetainedEarnings"


@dataclass(frozen=True)
class Period:
    date: date
    year: int
    month: int

    @property
    def is_year_end(self) -> bool:
        return self.month == 12


def _months(start: date, n_years: int):
    for y in range(start.year, start.year + n_years):
        for m in range(1, 13):
            # Day pinned to the 1st; date precision beyond month is not modeled.
            yield Period(date=date(y, m, 1), year=y, month=m)


class Simulation:
    def __init__(self, engine: Engine, objects: list[SimObject],
                 start: date):
        self.engine = engine
        self.objects = objects
        self.start = start

    def run(self, n_years: int) -> Engine:
        for period in _months(self.start, n_years):
            self._accrue(period)
            self._fund(period)
            self._assess(period)
            if period.is_year_end:
                self._close(period)
        return self.engine

    # --- phases -------------------------------------------------------------

    def _accrue(self, period: Period) -> None:
        for obj in self.objects:
            for txn in obj.step(period, self.engine):
                self.engine.post(txn)

    def _fund(self, period: Period) -> None:
        # CashManager.cover_shortfall runs here after accrual. Scenario 3.
        pass

    def _assess(self, period: Period) -> None:
        # TaxEngine.settle runs here at year end. Tax milestone.
        pass

    # --- year-end closing entries ------------------------------------------

    def _close(self, period: Period) -> None:
        """Sweep every nominal (Income:*, Expense:*) balance to RetainedEarnings."""
        for prefix in ("Income:", "Expense:"):
            for account in self.engine.accounts(prefix):
                bal = self.engine.balance(account)
                if bal == ZERO:
                    continue
                self.engine.post(
                    Transaction(
                        date=period.date,
                        description=f"Close {account} -> RetainedEarnings",
                        postings=[
                            Posting(account, -bal, meta={"kind": "close"}),
                            Posting(RETAINED, bal, meta={"kind": "close"}),
                        ],
                    )
                )