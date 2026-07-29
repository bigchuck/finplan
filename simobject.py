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
        # Data lives in a hierarchical attrs dict, not fixed fields, so new
        # levers never force a schema change.
        self.attrs: dict = attrs or {}

    @abstractmethod
    def step(self, period, engine: Engine) -> list[Transaction]:
        """Return transactions this object produces for the given period."""
        raise NotImplementedError