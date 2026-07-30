"""CashManager: the first reactive actor.

Not a Generator. Generators run in the *accrue* phase and manufacture the
month's planned flows. The CashManager runs in the *fund* phase — deliberately
AFTER accrual — so it reads the net cash position for the month before deciding
whether the plan is short, then posts corrections. Reading-state-and-reacting
is what makes it an actor rather than a scheduled generator.

Three separable decisions, each a dial you can sweep independently:

  eligibility : may this source be tapped at all this period? (predicate)
  order       : the JSON waterfall — brokerage -> IRA -> Roth (break-glass last)
  per-source floor : capacity = balance - source_floor, NOT balance-to-zero

Gross-up (the silent-bug trap): a withholding source (IRA ~20%) must pull
GROSS = need_net / (1 - w) to DELIVER need_net net. We always subtract the NET
delivered from the shortfall — never the gross — or every funded month quietly
under-funds.

Severity: after the waterfall runs, if cash is back at/above the floor it's a
soft reserve-breach (the plan dipped and recovered); if the waterfall ran dry
and cash is still below the floor it's a hard reserve-exhausted / insolvent
month — a plan-failure signal, not just a log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .accounts import AssetAccount, Withdrawal, ONE
from .engine import Engine
from .primitives import Decimal, ZERO, money

SOFT = "reserve-breach"
HARD = "reserve-exhausted"


@dataclass
class Source:
    """One rung of the waterfall: an account object and its own floor."""
    account: AssetAccount
    floor: Decimal = ZERO

    def eligible(self, period) -> bool:
        # Placeholder predicate. A real one might gate an IRA before 59½, or a
        # source by calendar. Kept trivially true until a scenario needs more.
        return True


@dataclass
class FundingEvent:
    """One month in which a shortfall triggered forced funding."""
    date: date
    cash_before: Decimal
    need_net: Decimal
    pulls: list[Withdrawal]
    cash_after: Decimal
    shortfall_remaining: Decimal
    severity: str


class CashManager:
    """Refill ``cash_account`` to ``target`` whenever it drops below ``floor``,
    pulling from the ordered ``waterfall`` with per-source floors and gross-up.
    """

    def __init__(self, cash_account: str, floor, target,
                 waterfall: list[Source], trigger: str = "cash-floor"):
        self.cash_account = cash_account
        self.floor = money(floor)
        self.target = money(target)
        self.waterfall = waterfall
        self.trigger = trigger
        self.events: list[FundingEvent] = []

    def cover_shortfall(self, period, engine: Engine) -> None:
        cash = engine.balance(self.cash_account)
        if cash >= self.floor:
            return  # floor intact; hysteresis means we do nothing above it

        cash_before = cash
        need_net = money(self.target - cash)   # refill goal is the TARGET
        remaining = need_net
        pulls: list[Withdrawal] = []

        for src in self.waterfall:
            if remaining <= ZERO:
                break
            if not src.eligible(period):
                continue
            w = src.account.withholding_rate
            capacity = src.account.capacity(src.floor, engine)   # gross
            if capacity <= ZERO or w >= ONE:
                continue

            gross_wanted = money(remaining / (ONE - w))
            gross = capacity if capacity < gross_wanted else gross_wanted
            if gross <= ZERO:
                continue

            meta = {"forced": True, "trigger": self.trigger,
                    "source": src.account.name}
            wd = src.account.fund_from(gross, self.cash_account, period,
                                       engine, meta)
            for txn in wd.txns:
                engine.post(txn)
            remaining = money(remaining - wd.net)   # subtract NET, never gross
            pulls.append(wd)

        cash_after = engine.balance(self.cash_account)
        severity = SOFT if cash_after >= self.floor else HARD
        self.events.append(FundingEvent(
            date=period.date,
            cash_before=cash_before,
            need_net=need_net,
            pulls=pulls,
            cash_after=cash_after,
            shortfall_remaining=money(max(ZERO, self.target - cash_after)),
            severity=severity,
        ))

    # --- reporting ---------------------------------------------------------

    def report(self) -> str:
        """Render the forced-withdrawal report. Empty string if nothing fired."""
        if not self.events:
            return "No forced withdrawals: the cash floor held every month."
        lines = ["Forced-withdrawal report:"]
        for e in self.events:
            flag = "HARD" if e.severity == HARD else "soft"
            lines.append(
                f"  {e.date}  [{flag}] {e.severity}: "
                f"cash {e.cash_before:,.2f} -> {e.cash_after:,.2f} "
                f"(needed {e.need_net:,.2f} to target)"
            )
            for wd in e.pulls:
                bits = [f"gross {wd.gross:,.2f}", f"net {wd.net:,.2f}"]
                if wd.withheld > ZERO:
                    bits.append(f"withheld {wd.withheld:,.2f}")
                if wd.gain > ZERO:
                    bits.append(f"gain {wd.gain:,.2f} ({wd.character})")
                elif wd.character:
                    bits.append(wd.character)
                lines.append(f"      from {wd.source}: " + ", ".join(bits))
            if e.shortfall_remaining > ZERO:
                lines.append(
                    f"      still short of target by {e.shortfall_remaining:,.2f}")
        hard = sum(1 for e in self.events if e.severity == HARD)
        lines.append(
            f"  ({len(self.events)} funded month(s), {hard} hard / insolvent)")
        return "\n".join(lines)