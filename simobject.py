"""SimObject: the root ABC of everything the simulation ticks.

Every SimObject must implement ``step()``. During the *accrue* phase the loop
calls ``step()`` on each object in order and collects the transactions it
returns. Accounts delegate step() to apply_growth(); Generators delegate to
emit(). The fund and assess phases are driven by reactive actors, not step().
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .engine import Engine
from .primitives import Transaction


class SimObject(ABC):
    def __init__(self, name: str, attrs: dict | None = None):
        self.name = name
        self.attrs: dict = attrs or {}

    @abstractmethod
    def step(self, period, engine: Engine) -> list[Transaction]:
        """Return transactions this object produces for the given period."""
        raise NotImplementedError

    def settle_effects(self, period, engine: Engine) -> None:
        """Optional third pass, run AFTER every object's step() output for
        this tick has been posted. Default no-op.

        Exists for a side effect that legitimately depends on a POSTED
        balance rather than the tick-open snapshot — e.g. crediting
        brokerage basis for a reinvested dividend only makes sense once the
        cash leg has actually landed. Doing that mutation inside step()
        would mean a generator's OWN not-yet-posted output leaking back into
        its own state, and — worse — invites the next generator added here
        to start reading state a sibling wrote mid-tick, which is exactly
        the order-dependence the snapshot/accrue split exists to prevent.
        settle_effects runs once the whole tick is already fixed, so nothing
        it does can change what any step() saw, and nothing it reads depends
        on object order.
        """
        return None