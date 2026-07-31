"""Generator subtree: flows that manufacture Transactions forward.

Generator is an ABC that forces ``emit()``. You never enter transactions
directly — you declare generators, and the engine manufactures balanced
postings every period they fire.

  Stream    (scheduled EXTERNAL inflow: SS, pension, annuity)   -- done
  Shock     (one-off unmodeled event)                           -- done
  Schedule  (forced/planned: RMD, withdrawal)                   -- this file
  OneOff    (degenerate Schedule, verbatim postings)            -> later

The intermediate ``Policy`` grouping node (state-driven category) is likewise
deferred until a second policy type (dividends) exists to justify the
abstraction; for now InterestPolicy inherits Generator directly.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .accounts import AssetAccount, TraditionalIRAAccount, Withdrawal
from .engine import Engine
from .primitives import Posting, Transaction, ZERO, money
from .simobject import SimObject


class Generator(SimObject):
    @abstractmethod
    def emit(self, period, engine: Engine) -> list[Transaction]:
        """Return balanced transactions for this period (may be empty)."""
        raise NotImplementedError

    def step(self, period, engine: Engine) -> list[Transaction]:
        return self.emit(period, engine)


class Stream(Generator):
    """Scheduled EXTERNAL inflow: Social Security, pension, annuity.

    The money originates outside your accounts, so this is the simple
    single-pair shape — the Income gate legitimately IS the source leg.
    """

    def __init__(self, name: str, to: str, income_account: str, amount,
                 start=None, end=None, attrs=None):
        super().__init__(name, attrs)
        self.to = to
        self.income_account = income_account
        self.amount = money(amount)
        self.start = start
        self.end = end

    def _active(self, period) -> bool:
        pm = (period.year, period.month)
        if self.start is not None and pm < (self.start.year, self.start.month):
            return False
        if self.end is not None and pm > (self.end.year, self.end.month):
            return False
        return True

    def emit(self, period, engine: Engine) -> list[Transaction]:
        if self.amount == ZERO or not self._active(period):
            return []
        owner = self.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"{self.name} income",
                postings=[
                    Posting(self.to, self.amount, owner=owner,
                            meta={"kind": "stream", "stream": self.name}),
                    Posting(self.income_account, -self.amount, owner=owner,
                            meta={"kind": "stream", "stream": self.name}),
                ],
            )
        ]


class InterestPolicy(Generator):
    """State-driven monthly interest on a cash asset account."""

    def __init__(self, name: str, source: AssetAccount,
                 income_account: str = "Income:Interest", attrs=None):
        super().__init__(name, attrs)
        self.source = source
        self.income_account = income_account

    def _monthly_rate(self) -> Decimal:
        apr = money(self.source.attrs.get("apr", 0))
        return apr / Decimal(12)

    def emit(self, period, engine: Engine) -> list[Transaction]:
        balance = engine.balance(self.source.name)
        interest = money(balance * self._monthly_rate())
        if interest == ZERO:
            return []
        owner = self.source.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"Monthly interest @ {self.source.attrs.get('apr')} APR",
                postings=[
                    Posting(self.source.name, interest, owner=owner,
                            meta={"kind": "interest"}),
                    Posting(self.income_account, -interest, owner=owner,
                            meta={"kind": "interest"}),
                ],
            )
        ]


class Shock(Generator):
    """One-off unmodeled event: fires exactly once, in a given (year, month)."""

    def __init__(self, name: str, frm: str, to: str, amount, when, attrs=None):
        super().__init__(name, attrs)
        self.frm = frm
        self.to = to
        self.amount = money(amount)
        self.when = when

    def emit(self, period, engine: Engine) -> list[Transaction]:
        if self.amount == ZERO:
            return []
        if (period.year, period.month) != (self.when.year, self.when.month):
            return []
        owner = self.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"{self.name} (shock)",
                postings=[
                    Posting(self.to, self.amount, owner=owner,
                            meta={"kind": "shock", "shock": self.name}),
                    Posting(self.frm, -self.amount, owner=owner,
                            meta={"kind": "shock", "shock": self.name}),
                ],
            )
        ]


# --- Schedule: forced/planned withdrawals (RMD, fixed) ---------------------

# 2022 IRS Uniform Lifetime Table (age -> distribution period). This is a
# swappable ASSUMPTION, not a structural constant: a control-file "divisors"
# map overrides/extends it per scenario, the same way tax brackets are
# overridable rather than hardcoded truth. Ages beyond the table use the last
# entry (the table effectively flatlines at the oldest ages in practice).
DEFAULT_RMD_DIVISORS: dict[int, Decimal] = {
    72: Decimal("27.4"), 73: Decimal("26.5"), 74: Decimal("25.5"),
    75: Decimal("24.6"), 76: Decimal("23.7"), 77: Decimal("22.9"),
    78: Decimal("22.0"), 79: Decimal("21.1"), 80: Decimal("20.2"),
    81: Decimal("19.4"), 82: Decimal("18.5"), 83: Decimal("17.7"),
    84: Decimal("16.8"), 85: Decimal("16.0"), 86: Decimal("15.2"),
    87: Decimal("14.4"), 88: Decimal("13.7"), 89: Decimal("12.9"),
    90: Decimal("12.2"), 91: Decimal("11.5"), 92: Decimal("10.8"),
    93: Decimal("10.1"), 94: Decimal("9.5"), 95: Decimal("8.9"),
    96: Decimal("8.4"), 97: Decimal("7.8"), 98: Decimal("7.3"),
    99: Decimal("6.8"), 100: Decimal("6.4"),
}


@dataclass
class ScheduleEvent:
    """One firing of a Schedule — the planned-withdrawal analog of
    CashManager's FundingEvent. ``requested`` vs ``gross`` mirrors
    CashManager's need_net vs delivered: they differ only when the source's
    own capacity capped the pull short of what the plan called for.
    """
    date: date
    mode: str
    requested: Decimal
    gross: Decimal
    net: Decimal
    withheld: Decimal = ZERO
    gain: Decimal = ZERO
    character: str | None = None
    source: str = ""
    to: str = ""


class Schedule(Generator):
    """A forced/planned withdrawal from a source account, on a fixed monthly
    trigger — the generator-side counterpart to CashManager's REACTIVE pulls.

    Three modes, chosen by ``mode``:

      "fixed"          — a declared GROSS amount, fired every ``month`` of
                         every active year. ``amount`` is required.

      "rmd"            — gross = balance(tick-open) / divisor(age), fired
                         once a year in ``month`` (default January), active
                         once ``owner_birth_year`` puts the owner at or past
                         ``rmd_start_age`` (default 73). ``owner_birth_year``
                         is required; ``divisors`` optionally overrides/
                         extends DEFAULT_RMD_DIVISORS by age.

      "roth_conversion" — a declared GROSS amount, fired every ``month`` of
                         every active year, like "fixed" — but dispatches to
                         the source's ``convert()`` rather than ``fund_from()``:
                         the FULL amount lands in ``to`` with NO withholding
                         leg (conventional for a conversion — the tax is
                         assumed paid from outside funds). ``source`` MUST be
                         a TraditionalIRAAccount (that's the only account type
                         a "conversion" is a coherent operation on); ``amount``
                         is required.

    ``start``/``end`` optionally bound ALL modes to a window of active years
    (month-granular, same semantics as Stream) — e.g. a conversion campaign
    run only for a handful of years before RMDs or SS begin. Unset means
    always active, exactly like Stream's default.

    Firing RMD in JANUARY (not December) is deliberate: under snapshot
    semantics the Jan-1 tick-open balance IS the prior Dec-31 balance — the
    exact numerator the IRS RMD formula wants. Firing in December would read
    the CURRENT year's still-growing balance, a year too early.

    Whatever the source account's own fund_from/convert shape is (IRA 3-leg +
    Income:Ordinary recognition, brokerage transfer + gain-slice recognition,
    Roth pure transfer, IRA conversion transfer + full recognition, no
    withholding leg) is exactly what a Schedule reuses — it never reimplements
    gross-up, withholding, or recognition; it only decides HOW MUCH and WHEN,
    then delegates. This keeps the tax-character logic in exactly one place.

    Money moves source -> ``to``, tagged with ``{"scheduled": True,
    "trigger": self.name}`` (the planned-withdrawal analog of CashManager's
    ``{"forced": True, ...}`` tag) so the journal or a report can distinguish
    scheduled distributions from CashManager's reactive ones. Every firing is
    also recorded in ``self.events`` for the forced-withdrawal-style
    ``report()`` below.
    """

    _MODES = ("fixed", "rmd", "roth_conversion")

    def __init__(self, name: str, source: AssetAccount, to: str, mode: str,
                 amount=None, month: int = 1, owner_birth_year: int | None = None,
                 rmd_start_age: int = 73, divisors: dict | None = None,
                 start=None, end=None, attrs=None):
        super().__init__(name, attrs)
        self.source = source
        self.to = to
        self.mode = mode
        self.amount = money(amount) if amount is not None else None
        self.month = int(month)
        self.owner_birth_year = owner_birth_year
        self.rmd_start_age = int(rmd_start_age)
        self.start = start   # datetime.date or None
        self.end = end       # datetime.date or None
        self.events: list[ScheduleEvent] = []
        # Per-schedule override/extension of the default table, keyed by age.
        merged = dict(DEFAULT_RMD_DIVISORS)
        if divisors:
            merged.update({int(k): Decimal(str(v)) for k, v in divisors.items()})
        self.divisors = merged

        if self.mode not in self._MODES:
            raise ValueError(f"Schedule {name!r}: unknown mode {mode!r}")
        if self.mode in ("fixed", "roth_conversion") and self.amount is None:
            raise ValueError(
                f"Schedule {name!r}: mode {mode!r} requires 'amount'")
        if self.mode == "rmd" and self.owner_birth_year is None:
            raise ValueError(
                f"Schedule {name!r}: mode 'rmd' requires 'owner_birth_year'")
        if self.mode == "roth_conversion" and not isinstance(
                source, TraditionalIRAAccount):
            raise ValueError(
                f"Schedule {name!r}: mode 'roth_conversion' requires source "
                f"to be a TraditionalIRAAccount, got {type(source).__name__}")

    # --- active-window gating (mirrors Stream._active) ----------------------

    def _active(self, period) -> bool:
        pm = (period.year, period.month)
        if self.start is not None and pm < (self.start.year, self.start.month):
            return False
        if self.end is not None and pm > (self.end.year, self.end.month):
            return False
        return True

    # --- gross-amount decision, split by mode -------------------------------

    def _age_in(self, year: int) -> int:
        return year - self.owner_birth_year

    def _divisor(self, age: int) -> Decimal:
        if age in self.divisors:
            return self.divisors[age]
        # Beyond the table's top age: hold at the last known divisor rather
        # than raise, so a multi-decade sim doesn't die at age 101.
        table_top = max(self.divisors)
        return self.divisors[table_top] if age > table_top else self.divisors[
            min(a for a in self.divisors if a >= age)
        ]

    def _gross_due(self, period, engine: Engine) -> Decimal:
        if self.mode in ("fixed", "roth_conversion"):
            return self.amount if period.month == self.month else ZERO

        # mode == "rmd"
        if period.month != self.month:
            return ZERO
        age = self._age_in(period.year)
        if age < self.rmd_start_age:
            return ZERO
        balance = engine.balance(self.source.name)   # tick-open snapshot
        if balance <= ZERO:
            return ZERO
        divisor = self._divisor(age)
        return money(balance / divisor)

    def emit(self, period, engine: Engine) -> list[Transaction]:
        if not self._active(period):
            return []
        if self.mode in ("fixed", "roth_conversion") and period.month != self.month:
            return []

        requested = self._gross_due(period, engine)
        if requested <= ZERO:
            return []

        capacity = self.source.capacity(ZERO, engine)   # don't go negative
        gross = capacity if capacity < requested else requested
        if gross <= ZERO:
            return []

        meta = {"scheduled": True, "trigger": self.name, "source": self.source.name}
        if self.mode == "roth_conversion":
            wd: Withdrawal = self.source.convert(gross, self.to, period, engine, meta)
        else:
            wd = self.source.fund_from(gross, self.to, period, engine, meta)

        self.events.append(ScheduleEvent(
            date=period.date, mode=self.mode, requested=requested,
            gross=wd.gross, net=wd.net, withheld=wd.withheld, gain=wd.gain,
            character=wd.character, source=self.source.name, to=self.to,
        ))
        return wd.txns

    # --- reporting -----------------------------------------------------------

    def report(self) -> str:
        """The forced-withdrawal-style report for THIS schedule. Empty-events
        case still names the schedule so an aggregate report can say plainly
        that a declared schedule never actually fired (e.g. owner never
        reached rmd_start_age within the simulated horizon)."""
        if not self.events:
            return f"  {self.name} ({self.mode}): never fired."
        lines = [f"  {self.name} ({self.mode}) — {len(self.events)} distribution(s):"]
        for e in self.events:
            bits = [f"gross {e.gross:,.2f}", f"net {e.net:,.2f}"]
            if e.gross < e.requested:
                bits.append(f"requested {e.requested:,.2f} (capped by capacity)")
            if e.withheld > ZERO:
                bits.append(f"withheld {e.withheld:,.2f}")
            if e.gain > ZERO:
                bits.append(f"gain {e.gain:,.2f} ({e.character})")
            elif e.character:
                bits.append(e.character)
            lines.append(f"    {e.date}  {e.source} -> {e.to}: " + ", ".join(bits))
        return "\n".join(lines)


def schedule_report(schedules: list[Schedule]) -> str:
    """Aggregate report across every declared Schedule (there is no single
    ScheduleManager instance the way there's one CashManager — each JSON
    ``schedules`` entry is its own Schedule object — so this is a module-level
    helper the CLI/report layer calls over sim.objects, the same way it reads
    sim.cash_manager.report() and sim.tax_engine.report()).
    """
    if not schedules:
        return "No schedules declared."
    lines = ["Scheduled-withdrawal report:"]
    for s in schedules:
        lines.append(s.report())
    return "\n".join(lines)