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
from .taxengine import DEDUCTIBLE_INTEREST

ONE = money(1)
RECOGNIZED = "Equity:Recognized"
UNREALIZED = "Equity:UnrealizedGains"
UNREALIZED_DEFERRED = "Equity:UnrealizedGains:Deferred"

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

    def eligible(self, period) -> bool:
        """May this account be tapped as a funding source this period?

        Lives on the account, not on the waterfall rung, because the
        constraint is a property of the instrument: a credit line that has
        not opened yet or has already matured, an IRA before 59½. Default
        yes; HELOCAccount is the first real override.
        """
        return True

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


class TaxDeferredGrowth:
    """Mixin: monthly compound growth with no gain recognition. Unlike
    BrokerageAccount, there's no basis to preserve and nothing is realized —
    an IRA withdrawal is taxed on the full amount regardless of how much of
    it is "growth," and a Roth withdrawal is never taxed at all. The
    offsetting leg goes to UNREALIZED_DEFERRED, not UNREALIZED, so this
    never touches the brokerage-only invariant.
    """

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
                description=f"{self.name} tax-deferred growth @ {growth} annual",
                postings=[
                    Posting(self.name, g, owner=owner, meta={"kind": "growth"}),
                    Posting(UNREALIZED_DEFERRED, -g, owner=owner,
                            meta={"kind": "growth"}),
                ],
            )
        ]


class RothAccount(TaxDeferredGrowth, AssetAccount):
    pass


class TraditionalIRAAccount(TaxDeferredGrowth, AssetAccount):
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


class HELOCAccount(LiabilityAccount):
    """A revolving credit line that can sit in the CashManager waterfall.

    This is the fourth ``fund_from`` shape and by far the simplest: drawing
    on a line of credit is BORROWING, not liquidating, so there is no
    recognition pair at all — borrowed money is not income. Cash goes up, the
    liability goes further negative, and that is the whole transaction.

    SIGN TRAP: a liability balance is NEGATIVE. ``AssetAccount.capacity`` is
    ``balance - floor``; here it is ``credit_limit - owed - floor``. Same
    method name, inverted arithmetic. The per-source floor reads as a
    borrowing reserve — headroom you decline to touch.

    Interest is NOT ``apply_growth``. It is a posted expense against a gate,
    which is a Policy emission, exactly as an asset's interest income is —
    the same split the module docstring draws for AssetAccount. Interest
    capitalizes into the balance, so a payment is a pure cash->liability
    transfer and the interest/principal split never needs computing: it falls
    out of the balance evolution instead.

    A capitalizing balance can exceed ``credit_limit``. Real lenders forbid
    that, but clamping it here would invent a repayment that never happened;
    instead ``capacity`` floors at zero (no further draws) and the balance is
    left to tell the truth.
    """

    @property
    def credit_limit(self) -> Decimal:
        return money(self.attrs.get("credit_limit", 0))

    @property
    def withholding_rate(self) -> Decimal:
        return ZERO   # nothing is withheld on borrowed money

    @property
    def interest_account(self) -> str:
        """Post-TCJA, only acquisition/improvement debt is deductible. The
        flag routes the expense to a gate the TaxEngine reads, or to one it
        ignores — no tax logic lives on this class."""
        return (DEDUCTIBLE_INTEREST if self.attrs.get("deductible")
                else "Expenses:Interest:HELOC")

    def eligible(self, period) -> bool:
        pm = (period.year, period.month)
        opened = self.attrs.get("opened")
        maturity = self.attrs.get("maturity")
        if opened is not None and pm < (opened.year, opened.month):
            return False
        # At maturity the line closes: the balance is still owed, but no new
        # draw can be taken against it.
        if maturity is not None and pm >= (maturity.year, maturity.month):
            return False
        return True

    def capacity(self, source_floor, engine: Engine) -> Decimal:
        owed = money(-engine.balance(self.name))
        avail = money(self.credit_limit - owed - money(source_floor))
        return avail if avail > ZERO else ZERO

    def fund_from(self, gross: Decimal, cash_account: str, period,
                  engine: Engine, meta: dict) -> Withdrawal:
        owner = self.attrs.get("owner")
        draw = Transaction(
            date=period.date,
            description=f"Draw on {self.name} to fund {cash_account}",
            postings=[
                Posting(cash_account, gross, owner=owner, meta=dict(meta)),
                Posting(self.name, -gross, owner=owner, meta=dict(meta)),
            ],
            meta=dict(meta),
        )
        # net == gross: no withholding, and no recognition transaction.
        return Withdrawal(txns=[draw], gross=gross, net=gross,
                          source=self.name)


class IncomeAccount(Account):
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []


class UnrealizedInvariantError(AssertionError):
    """Raised when Equity:UnrealizedGains drifts from -(mv - basis) summed
    across every brokerage account. A drift means a bug crept into growth,
    a sale, or a dividend reinvestment — this is the check-at-the-boundary
    catch, the brokerage analog of Engine's whole-ledger zero-sum assertion.
    """


def _shock_drain(engine: Engine, account: str) -> Decimal:
    """Net outflow ``account`` has taken via Shock-tagged postings (a plain
    transfer -- see generators.Shock) since the sim began. A Shock against a
    brokerage account is a deliberate, basis-neutral markdown -- most
    notably a pct-mode market-crash shock -- not a sale: nothing recognizes
    a gain/loss or adjusts basis to bring UNREALIZED back in step with the
    new mv, so the invariant below has to expect the drain instead of
    flagging it as drift.
    """
    total = ZERO
    for txn in engine.journal:
        for p in txn.postings:
            if p.account == account and p.meta.get("kind") == "shock":
                total = money(total - p.amount)
    return total


def unrealized_gap(engine: Engine, brokerages: list["BrokerageAccount"]) -> Decimal:
    """Return Equity:UnrealizedGains minus the expected -(mv - basis) total
    across ``brokerages``, net of any Shock-driven markdowns (see
    _shock_drain). Zero means the invariant holds."""
    expected = ZERO
    for acct in brokerages:
        mv = engine.balance(acct.name)
        basis = money(acct.attrs.get("basis", 0))
        expected = money(expected - (mv - basis) - _shock_drain(engine, acct.name))
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