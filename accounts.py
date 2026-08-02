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

Fundable sources (Scenario 3)
-----------------------------
An asset account can also be a *source* the CashManager pulls from to cover a
cash shortfall. Pulling money you already own between your own accounts is a
TRANSFER (asset -> asset, no net-worth change), optionally paired with a
RECOGNITION of taxable character. The recognition pair posts an Income gate
against the permanent ``Equity:Recognized`` contra so that recognizing a gain
adds tax character WITHOUT inflating net worth — the phantom "gain" the Income
gate would otherwise push into RetainedEarnings is cancelled by the contra.

Three shapes, one signature (``fund_from``), dispatched by the concrete class:

  AssetAccount / RothAccount : pure transfer pair, no recognition, no tax.
  BrokerageAccount           : transfer pair + recognition of the GAIN SLICE
                               only (Income:CapGains:LT); basis tracked and
                               decremented proportionally.
  TraditionalIRAAccount      : 3-leg transfer (IRA out gross, cash in net,
                               PrepaidTax:TYn withheld) + recognition of the
                               FULL pull as Income:Ordinary.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP

from .engine import Engine
from .primitives import Decimal, Posting, Transaction, ZERO, money
from .simobject import SimObject

ONE = money(1)
RECOGNIZED = "Equity:Recognized"
UNREALIZED = "Equity:UnrealizedGains"

# Rates and fractions (apr, growth, dividend_yield, qualified_fraction) are
# NEVER balances — cent-quantizing a rate like 0.0175 with `money()` silently
# corrupts it. `rate()` keeps six decimal places instead, plenty for a rate
# expressed to basis points, while still normalizing str/int/Decimal input.
RATE_PRECISION = Decimal("0.000001")


def rate(value) -> Decimal:
    """Coerce to a Decimal at rate precision (6 dp) — for rates/fractions,
    never for money. See RATE_PRECISION."""
    return Decimal(str(value)).quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)


@dataclass
class Withdrawal:
    txns: list[Transaction]
    gross: Decimal
    net: Decimal
    withheld: Decimal = ZERO
    gain: Decimal = ZERO
    character: str | None = None
    source: str = ""


class Account(SimObject):
    @abstractmethod
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        raise NotImplementedError

    def step(self, period, engine: Engine) -> list[Transaction]:
        return self.apply_growth(period, engine)


class AssetAccount(Account):
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []

    @property
    def withholding_rate(self) -> Decimal:
        return money(self.attrs.get("withholding", 0))

    def capacity(self, source_floor, engine: Engine) -> Decimal:
        avail = money(engine.balance(self.name) - money(source_floor))
        return avail if avail > ZERO else ZERO

    def fund_from(self, gross: Decimal, cash_account: str, period,
                  engine: Engine, meta: dict) -> Withdrawal:
        leg = dict(meta)
        transfer = Transaction(
            date=period.date,
            description=f"Fund {cash_account} from {self.name}",
            postings=[
                Posting(cash_account, gross, owner=self.attrs.get("owner"),
                        meta=leg),
                Posting(self.name, -gross, owner=self.attrs.get("owner"),
                        meta=leg),
            ],
            meta=dict(meta),
        )
        return Withdrawal(txns=[transfer], gross=gross, net=gross,
                          source=self.name)


class RothAccount(AssetAccount):
    pass


class TraditionalIRAAccount(AssetAccount):
    @property
    def withholding_rate(self) -> Decimal:
        return money(self.attrs.get("withholding", "0.20"))

    def fund_from(self, gross: Decimal, cash_account: str, period,
                  engine: Engine, meta: dict) -> Withdrawal:
        w = self.withholding_rate
        withheld = money(gross * w)
        net = money(gross - withheld)
        owner = self.attrs.get("owner")
        prepaid = f"Assets:PrepaidTax:TY{period.year}"

        transfer = Transaction(
            date=period.date,
            description=f"Fund {cash_account} from {self.name} (IRA distribution)",
            postings=[
                Posting(self.name, -gross, owner=owner, meta=dict(meta)),
                Posting(cash_account, net, owner=owner, meta=dict(meta)),
                Posting(prepaid, withheld, owner=owner,
                        meta={**meta, "kind": "withholding"}),
            ],
            meta=dict(meta),
        )
        recognition = _recognize("Income:Ordinary", gross, period, owner,
                                 character="ordinary")
        return Withdrawal(txns=[transfer, recognition], gross=gross, net=net,
                          withheld=withheld, character="ordinary",
                          source=self.name)

    def convert(self, gross: Decimal, destination: str, period,
                engine: Engine, meta: dict) -> Withdrawal:
        owner = self.attrs.get("owner")
        transfer = Transaction(
            date=period.date,
            description=f"Convert {self.name} -> {destination} (Roth conversion)",
            postings=[
                Posting(self.name, -gross, owner=owner, meta=dict(meta)),
                Posting(destination, gross, owner=owner, meta=dict(meta)),
            ],
            meta=dict(meta),
        )
        recognition = _recognize("Income:Ordinary", gross, period, owner,
                                 character="ordinary")
        return Withdrawal(txns=[transfer, recognition], gross=gross, net=gross,
                          character="ordinary", source=self.name)


class BrokerageAccount(AssetAccount):
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        mv = engine.balance(self.name)
        growth = rate(self.attrs.get("growth", 0))
        g = money(mv * (growth / Decimal(12)))
        if g == ZERO:
            return []
        owner = self.attrs.get("owner")
        return [
            Transaction(
                date=period.date,
                description=f"{self.name} appreciation @ {growth} annual",
                postings=[
                    Posting(self.name, g, owner=owner, meta={"kind": "growth"}),
                    Posting(UNREALIZED, -g, owner=owner, meta={"kind": "growth"}),
                ],
            )
        ]

    def fund_from(self, gross: Decimal, cash_account: str, period,
                  engine: Engine, meta: dict) -> Withdrawal:
        owner = self.attrs.get("owner")
        mv = engine.balance(self.name)
        basis = money(self.attrs.get("basis", 0))

        if mv > ZERO:
            gain = money(gross * (mv - basis) / mv)
        else:
            gain = ZERO
        basis_sold = money(gross - gain)
        self.attrs["basis"] = money(basis - basis_sold)

        transfer = Transaction(
            date=period.date,
            description=f"Fund {cash_account} from {self.name} (sale)",
            postings=[
                Posting(cash_account, gross, owner=owner, meta=dict(meta)),
                Posting(self.name, -gross, owner=owner, meta=dict(meta)),
            ],
            meta=dict(meta),
        )
        txns = [transfer]
        if gain != ZERO:
            txns.append(_recognize("Income:CapGains:LT", gain, period, owner,
                                   character="ltcg", contra=UNREALIZED))
        return Withdrawal(txns=txns, gross=gross, net=gross, gain=gain,
                          character="ltcg", source=self.name)

    def credit_basis(self, amount: Decimal) -> None:
        """Increase basis by ``amount`` — for already-taxed money (a
        reinvested dividend) landing back in this account. This does NOT
        touch the ledger; the mv leg is posted separately by whatever
        transaction delivered the cash. Omitting this call after such a
        transaction causes double-taxation: the reinvested amount would
        still count as embedded gain on a later sale, even though it was
        already taxed as income when it arrived.
        """
        self.attrs["basis"] = money(money(self.attrs.get("basis", 0)) + amount)


def _recognize(income_gate: str, amount: Decimal, period, owner,
               character: str, contra: str = RECOGNIZED) -> Transaction:
    return Transaction(
        date=period.date,
        description=f"Recognize {amount} to {income_gate}",
        postings=[
            Posting(income_gate, -amount, owner=owner,
                    meta={"kind": "recognition", "character": character}),
            Posting(contra, amount, owner=owner,
                    meta={"kind": "recognition", "character": character}),
        ],
        meta={"kind": "recognition"},
    )


class LiabilityAccount(Account):
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []


class IncomeAccount(Account):
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []


class UnrealizedInvariantError(AssertionError):
    """Raised when Equity:UnrealizedGains drifts from -(mv - basis) summed
    across every brokerage account. A drift means a bug crept into growth,
    a sale, or a dividend reinvestment — this is the check-at-the-boundary
    catch, the brokerage analog of Engine's whole-ledger zero-sum assertion.
    """


def unrealized_gap(engine: Engine, brokerages: list["BrokerageAccount"]) -> Decimal:
    """Return Equity:UnrealizedGains minus the expected -(mv - basis) total
    across ``brokerages``. Zero means the invariant holds."""
    expected = ZERO
    for acct in brokerages:
        mv = engine.balance(acct.name)
        basis = money(acct.attrs.get("basis", 0))
        expected = money(expected - (mv - basis))
    actual = engine.balance(UNREALIZED)
    return money(actual - expected)


def check_unrealized(engine: Engine, brokerages: list["BrokerageAccount"]) -> None:
    """Assert the invariant holds; raise UnrealizedInvariantError if not."""
    gap = unrealized_gap(engine, brokerages)
    if gap != ZERO:
        raise UnrealizedInvariantError(
            f"Equity:UnrealizedGains drifted by {gap}: expected "
            f"-(mv - basis) summed across {[a.name for a in brokerages]}"
        )