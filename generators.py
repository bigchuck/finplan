"""Generator subtree: flows that manufacture Transactions forward.

Generator is an ABC that forces ``emit()``. You never enter transactions
directly — you declare generators, and the engine manufactures balanced
postings every period they fire.

  Stream    (scheduled EXTERNAL inflow: SS, pension, annuity)   -- done
  Policy    (state-driven recurring rate: interest, dividends)  -- this file
  Shock     (one-off unmodeled event)                           -- done
  Schedule  (forced/planned: RMD, withdrawal)                   -- this file
  OneOff    (degenerate Schedule, verbatim postings)            -> later

The ``Policy`` grouping node was deferred until a second policy type existed
to justify the abstraction; DividendPolicy is that second type, so it's
introduced here and InterestPolicy is re-parented under it.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .accounts import (
    AssetAccount, BrokerageAccount, HELOCAccount, TraditionalIRAAccount,
    Withdrawal, rate,
)
from .engine import Engine
from .primitives import Posting, Transaction, ZERO, money
from .simobject import SimObject


class Generator(SimObject):
    @abstractmethod
    def emit(self, period, engine: Engine) -> list[Transaction]:
        raise NotImplementedError

    def step(self, period, engine: Engine) -> list[Transaction]:
        return self.emit(period, engine)


class Stream(Generator):
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


class Policy(Generator):
    """State-driven recurring-rate generator: reads a source account's
    balance and an annual rate attr (``rate_attr``), emits monthly. This is
    the shared shape across any passive recurring cash flow driven by a
    balance and a rate — interest and dividends today, and a real category
    for things like bond coupons or rental yield later — even though what
    each concrete Policy emits (which gate(s), one leg or several, whether a
    reinvestment mutates basis) diverges completely below this point.
    """

    def __init__(self, name: str, source: AssetAccount, rate_attr: str,
                 attrs=None):
        super().__init__(name, attrs)
        self.source = source
        self.rate_attr = rate_attr

    def _monthly_rate(self) -> Decimal:
        return rate(self.source.attrs.get(self.rate_attr, 0)) / Decimal(12)

    def monthly_amount(self, engine: Engine) -> Decimal:
        balance = engine.balance(self.source.name)
        return money(balance * self._monthly_rate())


class InterestPolicy(Policy):
    def __init__(self, name: str, source: AssetAccount,
                 income_account: str = "Income:Interest", attrs=None):
        super().__init__(name, source, rate_attr="apr", attrs=attrs)
        self.income_account = income_account

    def emit(self, period, engine: Engine) -> list[Transaction]:
        interest = self.monthly_amount(engine)
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


class HELOCInterestPolicy(Policy):
    """Interest CHARGED on a credit line — the mirror of InterestPolicy.

    Same Policy shape (balance x rate, monthly), opposite direction: the
    source balance is negative, so ``monthly_amount`` returns a negative
    figure and the charge is its magnitude. The offsetting leg capitalizes
    into the liability rather than paying out as cash, which is what makes
    HELOCPayment a pure transfer.

    The gate comes from the account (``interest_account``), so deductible and
    non-deductible lines route themselves and no tax logic lands here.
    """

    def __init__(self, name: str, source: HELOCAccount, attrs=None):
        super().__init__(name, source, rate_attr="rate", attrs=attrs)

    def emit(self, period, engine: Engine) -> list[Transaction]:
        charge = money(-self.monthly_amount(engine))
        if charge <= ZERO:
            return []
        owner = self.source.attrs.get("owner")
        gate = self.source.interest_account
        return [
            Transaction(
                date=period.date,
                description=f"Interest on {self.source.name} @ "
                            f"{self.source.attrs.get('rate')} APR",
                postings=[
                    Posting(gate, charge, owner=owner,
                            meta={"kind": "heloc-interest"}),
                    Posting(self.source.name, -charge, owner=owner,
                            meta={"kind": "heloc-interest"}),
                ],
            )
        ]


class DividendPolicy(Policy):
    """State-driven monthly dividend on a brokerage account.

    ``growth`` on BrokerageAccount.apply_growth is PRICE RETURN;
    ``dividend_yield`` here is ADDITIVE on top — total return =
    growth + dividend_yield. This keeps mv appreciation (unrealized, no
    gate) and dividend cash flow (realized income, a gate) as two
    independent, separately-attributable levers, matching how return
    assumptions are conventionally quoted ("7% growth, 2% yield") and
    avoiding the footgun of a total-return number silently double-counting
    against a separately-set growth rate.

    Split into qualified / ordinary by ``qualified_fraction`` (default 1.0
    — fully qualified, the common case for a diversified equity fund).
    Qualified dividends post to Income:Dividends:Qualified (PREFERENTIAL
    character in the TaxEngine's gate table, taxed on the LTCG schedule);
    the ordinary slice posts to Income:Dividends:Ordinary.

    reinvest=True (default): destination IS the source account itself (cash
    lands back in the brokerage, raising mv). Because that money was already
    taxed as income this same tick, basis must rise by the same amount —
    omitting this would double-tax it as embedded gain on a later sale. But
    the mv leg below is only a pending transaction at emit() time (accrue
    collects before posting), so mutating basis here would read/write ahead
    of the engine. The credit is deferred to settle_effects, which runs
    after this tick's transactions are actually posted — see
    SimObject.settle_effects and BrokerageAccount.credit_basis.

    reinvest=False: destination is ``to`` (a cash account); no basis credit,
    since the money leaves the brokerage account entirely.
    """

    def __init__(self, name: str, source: BrokerageAccount,
                 qualified_fraction=1, reinvest: bool = True,
                 to: str | None = None, attrs=None):
        super().__init__(name, source, rate_attr="dividend_yield", attrs=attrs)
        self.qualified_fraction = rate(qualified_fraction)
        self.reinvest = bool(reinvest)
        self.to = to
        # Basis delta held back out of the collection loop; see settle_effects.
        self._pending_basis = ZERO

        if not isinstance(source, BrokerageAccount):
            raise ValueError(
                f"DividendPolicy {name!r}: source must be a BrokerageAccount "
                f"(basis tracking and taxability are brokerage-specific), "
                f"got {type(source).__name__}")
        if not (ZERO <= self.qualified_fraction <= 1):
            raise ValueError(
                f"DividendPolicy {name!r}: qualified_fraction must be in "
                f"[0, 1], got {self.qualified_fraction}")
        if not self.reinvest and not self.to:
            raise ValueError(
                f"DividendPolicy {name!r}: reinvest=false requires a "
                f"'dividend_to' cash destination")

    @property
    def destination(self) -> str:
        return self.source.name if self.reinvest else self.to

    def emit(self, period, engine: Engine) -> list[Transaction]:
        self._pending_basis = ZERO   # never leave a stale delta from a prior tick
        div = self.monthly_amount(engine)
        if div == ZERO:
            return []

        owner = self.source.attrs.get("owner")
        qualified = money(div * self.qualified_fraction)
        # Subtract rather than multiply by (1 - fraction): the two gates then
        # sum to div EXACTLY, immune to the rounding a second multiply invites.
        ordinary = money(div - qualified)
        meta = {"kind": "dividend", "policy": self.name}

        postings = [Posting(self.destination, div, owner=owner, meta=dict(meta))]
        if qualified != ZERO:
            postings.append(Posting("Income:Dividends:Qualified", -qualified,
                                    owner=owner, meta=dict(meta)))
        if ordinary != ZERO:
            postings.append(Posting("Income:Dividends:Ordinary", -ordinary,
                                    owner=owner, meta=dict(meta)))

        if self.reinvest:
            self._pending_basis = div

        return [
            Transaction(
                date=period.date,
                description=f"{self.name} dividend",
                postings=postings,
            )
        ]

    def settle_effects(self, period, engine: Engine) -> None:
        if self._pending_basis != ZERO:
            self.source.credit_basis(self._pending_basis)
            self._pending_basis = ZERO


class HELOCPayment(Generator):
    """Payments against a credit line, plus the maturity balloon.

    Because interest capitalizes (see HELOCAccount), a payment is a PURE
    cash->liability transfer. There is no interest/principal split to
    compute: the split is an artifact of cash-basis presentation, and this
    model accrues. A fixed monthly payment amortizes the line correctly all
    by itself, purely through the balance evolution.

    ``interest_only`` needs no ordering hack. Every step() reads the
    tick-open ledger, so the figure this computes from ``owed * monthly
    rate`` is identical to the one HELOCInterestPolicy computed from the same
    snapshot, whichever runs first — the balance holds flat by construction.

    The balloon re-fires every month at or after maturity until the line is
    clear, so a month where cash could not cover it is retried rather than
    silently forgiven.
    """

    _MODES = ("none", "interest_only", "fixed")

    def __init__(self, name: str, source: HELOCAccount, frm: str,
                 mode: str = "none", amount=None, maturity=None, attrs=None):
        super().__init__(name, attrs)
        self.source = source
        self.frm = frm
        self.mode = mode
        self.amount = money(amount) if amount is not None else None
        self.maturity = maturity
        if self.mode not in self._MODES:
            raise ValueError(
                f"HELOCPayment {name!r}: unknown mode {mode!r}; "
                f"expected one of {self._MODES}")
        if self.mode == "fixed" and self.amount is None:
            raise ValueError(
                f"HELOCPayment {name!r}: mode 'fixed' requires 'amount'")

    def _monthly_rate(self) -> Decimal:
        return rate(self.source.attrs.get("rate", 0)) / Decimal(12)

    def _is_balloon(self, period) -> bool:
        return (self.maturity is not None
                and (period.year, period.month)
                >= (self.maturity.year, self.maturity.month))

    def emit(self, period, engine: Engine) -> list[Transaction]:
        owed = money(-engine.balance(self.source.name))
        if owed <= ZERO:
            return []

        balloon = self._is_balloon(period)
        if balloon:
            # Clear the line in ONE month. Pay the tick-open balance PLUS the
            # charge HELOCInterestPolicy is computing from that identical
            # snapshot this same tick — same inputs, same arithmetic, so this
            # lands exactly on zero instead of leaving a residual interest
            # tail to sweep over the following months.
            pay = money(owed + money(owed * self._monthly_rate()))
        elif self.mode == "none":
            return []
        elif self.mode == "interest_only":
            pay = money(owed * self._monthly_rate())
        else:
            pay = self.amount

        # Never overshoot into a positive (asset-shaped) liability balance.
        # Exempt the balloon: overshooting is exactly what it is doing, and
        # the interest leg lands in the same tick to meet it.
        if not balloon and pay > owed:
            pay = owed
        if pay <= ZERO:
            return []

        owner = self.source.attrs.get("owner")
        kind = "heloc-balloon" if balloon else "heloc-payment"
        label = "Balloon payoff of" if balloon else "Payment on"
        return [
            Transaction(
                date=period.date,
                description=f"{label} {self.source.name}",
                postings=[
                    Posting(self.source.name, pay, owner=owner,
                            meta={"kind": kind, "generator": self.name}),
                    Posting(self.frm, -pay, owner=owner,
                            meta={"kind": kind, "generator": self.name}),
                ],
            )
        ]


class Shock(Generator):
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
        self.start = start
        self.end = end
        self.events: list[ScheduleEvent] = []
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

    def _active(self, period) -> bool:
        pm = (period.year, period.month)
        if self.start is not None and pm < (self.start.year, self.start.month):
            return False
        if self.end is not None and pm > (self.end.year, self.end.month):
            return False
        return True

    def _age_in(self, year: int) -> int:
        return year - self.owner_birth_year

    def _divisor(self, age: int) -> Decimal:
        if age in self.divisors:
            return self.divisors[age]
        table_top = max(self.divisors)
        return self.divisors[table_top] if age > table_top else self.divisors[
            min(a for a in self.divisors if a >= age)
        ]

    def _gross_due(self, period, engine: Engine) -> Decimal:
        if self.mode in ("fixed", "roth_conversion"):
            return self.amount if period.month == self.month else ZERO

        if period.month != self.month:
            return ZERO
        age = self._age_in(period.year)
        if age < self.rmd_start_age:
            return ZERO
        balance = engine.balance(self.source.name)
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

        capacity = self.source.capacity(ZERO, engine)
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

    def report(self) -> str:
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
    if not schedules:
        return "No schedules declared."
    lines = ["Scheduled-withdrawal report:"]
    for s in schedules:
        lines.append(s.report())
    return "\n".join(lines)