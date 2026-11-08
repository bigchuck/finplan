"""Market segments: time-varying growth by asset class.

An account's own ``growth`` attr is its BASELINE rate. A scenario's
top-level ``market`` list holds named *segments*, each aimed at one
``asset_class``, that override that baseline for every account of the class
while the segment runs:

    "market": [
      {"name": "crash_2027", "asset_class": "equity",
       "start": "2027-05", "months": 5, "drawdown": "0.20"},
      {"name": "new_normal", "asset_class": "equity",
       "start": "2027-10", "growth": "0.035"}
    ]

A segment is exactly one of:

  drawdown -- a TOTAL decline over ``months`` (required). Compounded
              geometrically: the monthly rate is (1 - d)^(1/months) - 1, so an
              account with no money moving in or out ends exactly ``d`` down
              (to the cent-rounding of each month's posting), and one being
              drawn from falls in proportion to what it holds each month.
  growth   -- an annual rate, applied as growth/12 per month -- the same
              convention as an account's own ``growth``. ``months`` is
              optional; without it the segment runs until replaced.

Resolution, per account per month: among the segments of the account's class
that are running this month, the one with the LATEST start wins; if none is
running, the account's own ``growth`` applies. So a finite segment that ends
uncovers whatever is still running beneath it (a dip inside a longer regime
hands back to that regime), and an open-ended one is a permanent new normal
until something later starts. Two segments of one class starting the same
month have no winner and are rejected at build time.

Because ``market`` is a list of named entries, mixins add to it (or replace
an entry by name) under the ordinary scenario merge rules, so several dip or
regime mixins stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .accounts import rate

_SEGMENT_KEYS = ("name", "asset_class", "start", "months", "drawdown",
                 "growth")
_START_RE = re.compile(r"^(\d{4})-(\d{2})$")
TWELVE = Decimal(12)


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


@dataclass(frozen=True)
class Segment:
    name: str
    asset_class: str
    start: int                  # month index, see _month_index
    months: int | None          # None = open-ended
    monthly_rate: Decimal       # full precision -- never cent-quantized
    label: str                  # human-readable, for journal descriptions

    def running(self, idx: int) -> bool:
        if idx < self.start:
            return False
        return self.months is None or idx < self.start + self.months


def _drawdown_rate(d: Decimal, months: int) -> Decimal:
    """(1 - d)^(1/months) - 1 at high precision. A fractional Decimal power
    is inexact; 30 significant digits keeps the per-month error far below a
    cent on any realistic balance."""
    with localcontext() as ctx:
        ctx.prec = 30
        return (Decimal(1) - d) ** (Decimal(1) / Decimal(months)) - Decimal(1)


def build_segment(spec: dict) -> Segment:
    name = spec.get("name")
    if not name:
        raise ValueError(f"market segment {spec!r}: 'name' is required")
    where = f"market segment {name!r}"

    unknown = set(spec) - set(_SEGMENT_KEYS)
    if unknown:
        # A typo (e.g. "drawdwon") would otherwise silently mean no segment.
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}")

    cls = spec.get("asset_class")
    if not isinstance(cls, str) or not cls:
        raise ValueError(f"{where}: 'asset_class' must be a non-empty string")

    start = spec.get("start")
    m = _START_RE.match(start) if isinstance(start, str) else None
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(
            f"{where}: 'start' must be a year-month like \"2027-05\", "
            f"not {start!r}")
    start_idx = _month_index(int(m.group(1)), int(m.group(2)))

    months = spec.get("months")
    if months is not None:
        # bool is an int subclass; JSON true must not mean "1 month".
        if isinstance(months, bool) or not isinstance(months, int) or months < 1:
            raise ValueError(
                f"{where}: 'months' must be a whole number >= 1, "
                f"not {months!r}")

    has_dd = spec.get("drawdown") is not None
    has_g = spec.get("growth") is not None
    if has_dd == has_g:
        raise ValueError(
            f"{where}: exactly one of 'drawdown' or 'growth' is required")

    if has_dd:
        if months is None:
            raise ValueError(
                f"{where}: a 'drawdown' needs 'months' to spread it over")
        d = Decimal(str(spec["drawdown"]))
        if not Decimal(0) < d < Decimal(1):
            raise ValueError(
                f"{where}: 'drawdown' is a fraction between 0 and 1 "
                f"(0.20 = down 20%), not {spec['drawdown']!r}")
        monthly = _drawdown_rate(d, months)
        label = f"-{d} over {months} mo"
    else:
        g = rate(spec["growth"])
        monthly = g / TWELVE
        label = f"{g} annual"

    return Segment(name=name, asset_class=cls, start=start_idx,
                   months=months, monthly_rate=monthly, label=label)


class Market:
    """All segments of a scenario, grouped by asset class."""

    def __init__(self, segments: list[Segment]):
        self.by_class: dict[str, list[Segment]] = {}
        for seg in segments:
            self.by_class.setdefault(seg.asset_class, []).append(seg)
        for cls, segs in self.by_class.items():
            seen = {}
            for seg in segs:
                if seg.start in seen:
                    raise ValueError(
                        f"market segments {seen[seg.start]!r} and "
                        f"{seg.name!r} both start the same month for "
                        f"asset_class {cls!r}; there is no rule for which "
                        f"wins -- move one, or merge them")
                seen[seg.start] = seg.name

    def classes(self) -> set[str]:
        return set(self.by_class)

    def segment_for(self, asset_class: str | None, period) -> Segment | None:
        """The segment governing ``asset_class`` this period, or None when
        the account's own growth applies."""
        if asset_class is None:
            return None
        idx = _month_index(period.year, period.month)
        best = None
        for seg in self.by_class.get(asset_class, ()):
            if seg.running(idx) and (best is None or seg.start > best.start):
                best = seg
        return best


def build_market(specs: list) -> Market:
    return Market([build_segment(s) for s in specs])
