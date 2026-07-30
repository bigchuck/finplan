"""The period loop.

Monthly tick. Three phases per tick, in a fixed order to keep stock/flow
ordering bugs from hiding:

  accrue -> every SimObject.step() runs against a tick-open SNAPSHOT of the
            ledger; all emitted transactions are collected, THEN posted.
  fund   -> CashManager.cover_shortfall (runs AFTER accrual so it sees net
            cash). No-op until Scenario 3.
  assess -> year-end only: TaxEngine.settle reads the Income gates.
            No-op until the tax milestone.

Separately, in December the *closing entries* sweep nominal accounts
(Income:*, later Expense:*) to Equity:RetainedEarnings, resetting the gates
for the next year. Assets/Liabilities/Equity ride across the boundary.

SNAPSHOT (simultaneity) semantics within accrue: every object reads balances
as of the start of the tick, because we collect all emissions before posting
any of them. No object can see another object's same-tick output, so the
result is independent of the order objects sit in the list. This is a
deliberate choice over "post as produced": order-independence is a property
we can trust over decades, and the cost — interest that ignores the current
month's own inflows — is both defensible and negligible. Do NOT reintroduce
posting inside the collection loop; that silently restores order dependence.
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
                 start: date, cash_manager=None):
        self.engine = engine
        self.objects = objects
        self.start = start
        # Reactive actor for the fund phase. None until Scenario 3 wires one;
        # when absent, _fund is a no-op and the loop is unchanged.
        self.cash_manager = cash_manager

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
        # Snapshot semantics: gather every object's emissions against the
        # tick-open ledger FIRST, then post. Nothing is posted mid-gather, so
        # each object sees the same starting balances regardless of list order.
        pending: list[Transaction] = []
        for obj in self.objects:
            pending.extend(obj.step(period, self.engine))
        for txn in pending:
            self.engine.post(txn)

    def _fund(self, period: Period) -> None:
        # After accrual, so the manager sees the month's net cash before
        # deciding whether to force a withdrawal. Sequential by design: it is
        # ONE actor draining sources in waterfall order, each pull reading the
        # balance the previous pull left — unlike the order-independent accrue.
        if self.cash_manager is not None:
            self.cash_manager.cover_shortfall(period, self.engine)

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