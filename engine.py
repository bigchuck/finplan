"""The Engine holds the ledger and enforces the second, independent invariant.

Two invariants, deliberately separate:
  1. Each Transaction sums to zero  -> enforced in primitives.Transaction.
  2. The WHOLE ledger sums to zero   -> enforced here, after every post().
The first catches a malformed event; the second catches a leak between
accounts. Collapsing them would lose that distinction.
"""

from __future__ import annotations

from collections import defaultdict

from .primitives import Decimal, Posting, Transaction, ZERO, money


class LedgerLeak(AssertionError):
    """Raised if the sum of all account balances ever drifts from zero."""


class Engine:
    def __init__(self):
        self._balances: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self.journal: list[Transaction] = []

    # --- mutation -----------------------------------------------------------

    def post(self, txn: Transaction) -> None:
        """Apply a (already self-balanced) transaction and re-assert the ledger."""
        for p in txn.postings:
            self._balances[p.account] = money(self._balances[p.account] + p.amount)
        self.journal.append(txn)
        self._assert_whole_ledger()

    def post_all(self, txns) -> None:
        for txn in txns:
            self.post(txn)

    # --- reads --------------------------------------------------------------

    def balance(self, account: str) -> Decimal:
        return self._balances[account]

    def accounts(self, prefix: str = "") -> list[str]:
        """All account names, optionally filtered by prefix (e.g. 'Income:')."""
        return sorted(name for name in self._balances if name.startswith(prefix))

    def balances(self, prefix: str = "") -> dict[str, Decimal]:
        return {name: self._balances[name] for name in self.accounts(prefix)}

    # --- invariant ----------------------------------------------------------

    def _assert_whole_ledger(self) -> None:
        total = sum(self._balances.values(), ZERO)
        if total != ZERO:
            raise LedgerLeak(f"whole-ledger sum drifted to {total}, expected 0")