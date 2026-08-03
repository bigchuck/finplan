"""The period loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .accounts import BrokerageAccount, check_unrealized
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
            yield Period(date=date(y, m, 1), year=y, month=m)


class Simulation:
    def __init__(self, engine: Engine, objects: list[SimObject],
                 start: date, cash_manager=None, tax_engine=None):
        self.engine = engine
        self.objects = objects
        self.start = start
        self.cash_manager = cash_manager
        self.tax_engine = tax_engine

    def run(self, n_years: int) -> Engine:
        for period in _months(self.start, n_years):
            self._accrue(period)
            self._fund(period)
            self._assess(period)
            if period.is_year_end:
                self._close(period)
        return self.engine

    def _accrue(self, period: Period) -> None:
        pending: list[Transaction] = []
        for obj in self.objects:
            pending.extend(obj.step(period, self.engine))
        for txn in pending:
            self.engine.post(txn)
        # Third pass: runs after every one of this tick's transactions is
        # posted, so anything settle_effects reads (a posted balance) is
        # final. Order-independence survives because step()'s output already
        # fixed the ledger before any settle_effects runs — no object's
        # settle_effects can see a same-tick sibling's step() output before
        # it does, and none of them can change what any step() saw.
        for obj in self.objects:
            obj.settle_effects(period, self.engine)

    def _fund(self, period: Period) -> None:
        # Estimated tax goes out FIRST. It is a known, dated obligation, so
        # the CashManager has to see the ledger with that cash already gone
        # when it decides whether the month breached the floor. Reversing
        # these two leaves every estimate unfunded until the next tick.
        if self.tax_engine is not None:
            self.tax_engine.pay_estimates(period, self.engine)
        if self.cash_manager is not None:
            self.cash_manager.cover_shortfall(period, self.engine)

    def _assess(self, period: Period) -> None:
        if self.tax_engine is not None:
            self.tax_engine.settle(period, self.engine)

    def _close(self, period: Period) -> None:
        for prefix in ("Income:", "Expenses:"):
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
        # Year-end check that Equity:UnrealizedGains still equals
        # -(mv - basis) summed across every brokerage account. A drift means
        # a bug crept into growth, a sale, or a dividend reinvestment; catch
        # it at the boundary rather than let it compound silently for decades.
        brokerages = [o for o in self.objects if isinstance(o, BrokerageAccount)]
        if brokerages:
            check_unrealized(self.engine, brokerages)