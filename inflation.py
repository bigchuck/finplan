"""Inflation: a global nominal/real switch with year-keyed, carry-forward
CPI rates.

Mirrors the sticky-until-superseded semantics of TaxLawChange in
taxengine.py, but continuously rather than as a one-time mutation: the
annual rate in force for a given calendar year is the value at the largest
declared year <= that year, and it governs every month of that year until a
later declared year supersedes it.

Scope is deliberately narrow: this affects only DECLARED DOLLAR AMOUNTS on
Stream, Schedule (fixed/roth_conversion modes), and Shock -- money entered
as "today's dollars" that should escalate with inflation before hitting the
ledger. It does NOT touch apr/growth/dividend_yield, which are already
conventionally quoted as nominal return assumptions, nor Schedule's `rmd`
mode, which is balance-driven and already nominal by construction.

mode="nominal" (default) makes the whole feature inert: Period.inflation is
1 for every period of the run (see simulation.py's _months), and a "rates"
block, if present, is parsed but never consulted -- a scenario can declare
rates for later use without switching mode and see no behavior change.

mode="real" requires the declared rates to cover the simulation's start
year: the largest-year-<=-target lookup has nothing to fall back to before
its earliest key, and a silent zero-rate default there would understate
escalation without telling anyone. That's a hard error, caught once at
build time by check_covers, not a runtime condition discovered mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

MODES = ("nominal", "real")


class InflationError(ValueError):
    """Bad mode, empty rates under 'real', or a rates schedule that doesn't
    reach back to the simulation's start year."""


@dataclass
class Inflation:
    mode: str = "nominal"
    rates: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in MODES:
            raise InflationError(
                f"inflation mode {self.mode!r} not in {MODES}")
        # Normalize keys to int, values to Decimal, regardless of mode -- a
        # "nominal" scenario may still declare rates for later use, and they
        # should be well-formed even though they're never consulted.
        self.rates = {int(y): Decimal(str(r)) for y, r in self.rates.items()}

    def check_covers(self, start_year: int) -> None:
        """Hard-fail if mode is "real" and the rates schedule doesn't reach
        back to the simulation's start year. Called once at build time, not
        during the run -- a coverage gap is a control-file error, not a
        runtime condition."""
        if self.mode != "real":
            return
        if not self.rates:
            raise InflationError(
                "inflation mode 'real' requires a non-empty 'rates' schedule")
        if min(self.rates) > start_year:
            raise InflationError(
                f"inflation rates start at {min(self.rates)}, which is "
                f"after the simulation start year {start_year}; the "
                f"schedule must cover the start year")

    def rate_for(self, year: int) -> Decimal:
        """The annual CPI rate in force for `year`: the rate at the largest
        declared year <= `year`, carried forward. Only called under mode
        "real", after check_covers has confirmed coverage at build time --
        still guarded here rather than trusted, since nothing prevents a
        caller from constructing an Inflation directly and skipping
        check_covers."""
        eligible = [y for y in self.rates if y <= year]
        if not eligible:
            raise InflationError(
                f"no inflation rate declared at or before {year}")
        return self.rates[max(eligible)]


def build_inflation(spec: dict | None) -> Inflation:
    spec = spec or {}
    return Inflation(mode=spec.get("mode", "nominal"),
                     rates=spec.get("rates", {}))
