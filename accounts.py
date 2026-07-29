"""Account subtree: stocks that hold a balance across the sim.

Account is an ABC that forces ``apply_growth()`` — the monthly appreciation
hook. For a cash account that hook is a no-op: a checking account has no
unrealized market-value drift; its gains arrive as *posted interest income*
via an InterestPolicy (a Generator), which hits an Income gate. apply_growth
earns real work later for brokerage market-value appreciation (basis drift).

Keeping "market value drifts up silently" (apply_growth) separate from
"income is recognized at a gate" (a Policy emission) is the whole point of the
two subtrees, so we do not collapse them even though checking makes the
former a no-op.
"""

from __future__ import annotations

from abc import abstractmethod

from .engine import Engine
from .primitives import Transaction
from .simobject import SimObject


class Account(SimObject):
    """A stock. Holds a balance in the ledger and must define apply_growth()."""

    @abstractmethod
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        """Emit transactions for this account's own market-value appreciation."""
        raise NotImplementedError

    def step(self, period, engine: Engine) -> list[Transaction]:
        return self.apply_growth(period, engine)


class AssetAccount(Account):
    """Permanent asset (Assets:*). Rides across year boundaries."""

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        # Cash/checking: no unrealized appreciation. Interest is income, emitted
        # by an InterestPolicy against an Income gate — not modeled here.
        # Brokerage market-value drift (which feeds basis) lands here later.
        return []


class LiabilityAccount(Account):
    """Permanent liability (Liabilities:*)."""

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []


class IncomeAccount(Account):
    """A nominal income gate (Income:*).

    Not a wallet — a labeled gate on your own financial wall that accumulates
    tax-character information across the year and is swept to zero each
    December by the closing entries. It holds a balance (credit-normal, so a
    negative number as income accrues) but appreciates nothing of its own.
    """

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []