"""Core value objects: money, Posting, Transaction.

These are deliberately small and standalone (not part of the SimObject tree).
A ``Posting`` is one signed leg against a named account. A ``Transaction`` is
a bundle of postings that MUST sum to zero — it validates itself on
construction, so an unbalanced transaction cannot exist in the first place.

Sign convention (Beancount-style, borrowed conceptually):
  Assets, Expenses     -> increase is POSITIVE
  Liabilities, Equity, Income -> increase is NEGATIVE
So earning $100 of interest is: Assets:Checking +100, Income:Interest -100.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

# Money is Decimal throughout. Float interest would break the exact zero-sum
# assertions the whole design leans on, so we never let a float near a balance.
CENTS = Decimal("0.01")


def money(value) -> Decimal:
    """Coerce to a cent-quantized Decimal. Accepts int, str, or Decimal."""
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


ZERO = money(0)


@dataclass(frozen=True)
class Posting:
    """One signed delta against a single account.

    ``account`` is the fully-qualified name (e.g. 'Assets:Checking').
    ``amount`` is a signed cent-quantized Decimal.
    ``owner`` is carried from day one to support a future filing-status change;
    it is not used by any logic yet, but tagging late is the trap we avoid.
    ``meta`` holds free-form tags (e.g. {'forced': True, 'trigger': ...}).
    """

    account: str
    amount: Decimal
    owner: str | None = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        # Normalize amount to a quantized Decimal without mutating a frozen field.
        object.__setattr__(self, "amount", money(self.amount))


class UnbalancedTransaction(ValueError):
    """Raised when a Transaction's postings do not sum to exactly zero."""


@dataclass
class Transaction:
    """A balanced bundle of postings. Sums to zero or it cannot be constructed."""

    date: object                       # a datetime.date; kept loose for now
    description: str
    postings: list[Posting]
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        total = sum((p.amount for p in self.postings), ZERO)
        if total != ZERO:
            raise UnbalancedTransaction(
                f"{self.description!r} on {self.date} sums to {total}, not 0: "
                f"{[(p.account, str(p.amount)) for p in self.postings]}"
            )

    def __repr__(self):
        legs = ", ".join(f"{p.account} {p.amount:+}" for p in self.postings)
        return f"Transaction({self.date} {self.description!r}: {legs})"