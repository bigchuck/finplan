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

from .engine import Engine
from .primitives import Decimal, Posting, Transaction, ZERO, money
from .simobject import SimObject

ONE = money(1)
RECOGNIZED = "Equity:Recognized"
UNREALIZED = "Equity:UnrealizedGains"


@dataclass
class Withdrawal:
    """Result of one ``fund_from`` call: the transactions plus the numbers the
    CashManager needs (to subtract NET from the shortfall) and the report wants.
    """
    txns: list[Transaction]
    gross: Decimal
    net: Decimal                 # cash actually delivered to the cash account
    withheld: Decimal = ZERO     # tax withheld to PrepaidTax (IRA only)
    gain: Decimal = ZERO         # taxable gain recognized (brokerage only)
    character: str | None = None  # 'ltcg' | 'ordinary' | None (tax-free)
    source: str = ""


class Account(SimObject):
    """A stock. Holds a balance in the ledger and must define apply_growth()."""

    @abstractmethod
    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        """Emit transactions for this account's own market-value appreciation."""
        raise NotImplementedError

    def step(self, period, engine: Engine) -> list[Transaction]:
        return self.apply_growth(period, engine)


class AssetAccount(Account):
    """Permanent asset (Assets:*). Rides across year boundaries.

    Also the base *fundable source*: its default ``fund_from`` is the simplest
    shape — a pure transfer pair, no tax recognition, no withholding. Plain
    cash/savings (and Roth, below) use exactly this.
    """

    # --- growth -------------------------------------------------------------

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        # Cash/checking: no unrealized appreciation. Interest is income, emitted
        # by an InterestPolicy against an Income gate — not modeled here.
        # Brokerage market-value drift (which feeds basis) lands in Brokerage.
        return []

    # --- funding source interface ------------------------------------------

    @property
    def withholding_rate(self) -> Decimal:
        """Fraction withheld to PrepaidTax on a pull. Zero unless overridden."""
        return money(self.attrs.get("withholding", 0))

    def capacity(self, source_floor, engine: Engine) -> Decimal:
        """GROSS amount pullable without breaching this source's own floor."""
        avail = money(engine.balance(self.name) - money(source_floor))
        return avail if avail > ZERO else ZERO

    def fund_from(self, gross: Decimal, cash_account: str, period,
                  engine: Engine, meta: dict) -> Withdrawal:
        """Default shape: pure transfer, money already yours, no tax event."""
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
    """Roth (Assets:Roth...). Qualified distributions are tax-free, so the
    shape is exactly the base pure transfer — no recognition, no withholding.
    Named as its own type so the waterfall/report/tax layers can see that a
    pull came from tax-free money (and so 'break-glass last' reads clearly in
    the JSON waterfall). Behaviourally identical to the base transfer.
    """


class TraditionalIRAAccount(AssetAccount):
    """Pre-tax IRA (Assets:IRA...). A distribution is fully ordinary income and
    withholds a fraction (~20%) to PrepaidTax:TYn on the way out.
    """

    @property
    def withholding_rate(self) -> Decimal:
        return money(self.attrs.get("withholding", "0.20"))

    def fund_from(self, gross: Decimal, cash_account: str, period,
                  engine: Engine, meta: dict) -> Withdrawal:
        w = self.withholding_rate
        withheld = money(gross * w)
        net = money(gross - withheld)          # what actually reaches cash
        owner = self.attrs.get("owner")
        prepaid = f"Assets:PrepaidTax:TY{period.year}"

        # 3-leg transfer: all assets, sums to zero, net-worth neutral. The
        # withheld portion becomes a prepaid-tax asset, not an expense (the
        # expense is booked later when the TaxEngine settles the liability).
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
        # Recognition: the FULL pull is ordinary income. Contra to Equity:
        # Recognized cancels the phantom net-worth bump.
        recognition = _recognize("Income:Ordinary", gross, period, owner,
                                 character="ordinary")
        return Withdrawal(txns=[transfer, recognition], gross=gross, net=net,
                          withheld=withheld, character="ordinary",
                          source=self.name)


class BrokerageAccount(AssetAccount):
    """Taxable brokerage (Assets:Brokerage...). A sale realizes a capital gain
    on the GAIN SLICE only; basis is tracked (pooled average cost) and
    decremented proportionally so the gain fraction stays constant across
    sales. No withholding — cap-gains tax is handled later via estimates.

    Market-value drift (apply_growth) raises ``mv`` monthly and leaves basis
    put, so the taxable fraction ``(mv - basis)/mv`` ratchets UP over the
    decades. Appreciation is UNREALIZED — it hits no Income gate — so its
    balancing leg is the permanent ``Equity:UnrealizedGains`` bucket, which by
    construction always equals ``-(mv - basis)``. A sale then UNWINDS the
    realized slice out of that bucket (rather than minting a fresh contra):
    the gain moves from unrealized -> realized (taxed), the bucket rises by the
    slice, and the invariant survives the sale untouched.
    """

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        mv = engine.balance(self.name)
        growth = money(self.attrs.get("growth", 0))
        g = money(mv * (growth / Decimal(12)))
        if g == ZERO:
            return []
        owner = self.attrs.get("owner")
        # Asset up, UnrealizedGains down: net worth rises, but NO income gate is
        # touched (the gain is unrealized). basis is deliberately left alone.
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
        # basis returned by this sale = gross - gain (keeps gain+basis == gross
        # exactly, immune to rounding); basis drifts down proportionally.
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
            # Unwind the realized slice from UnrealizedGains (not a fresh
            # Recognized contra): the gain was already booked as unrealized, so
            # realizing it is a transfer between equity buckets, net-worth
            # neutral. This keeps UnrealizedGains == -(mv - basis) through sales.
            txns.append(_recognize("Income:CapGains:LT", gain, period, owner,
                                   character="ltcg", contra=UNREALIZED))
        return Withdrawal(txns=txns, gross=gross, net=gross, gain=gain,
                          character="ltcg", source=self.name)


def _recognize(income_gate: str, amount: Decimal, period, owner,
               character: str, contra: str = RECOGNIZED) -> Transaction:
    """A recognition pair: credit an Income gate, debit an equity contra.

    Sums to zero on its own and is net-worth neutral (the Income gate would
    otherwise push RetainedEarnings; the contra cancels it). It exists to
    record taxable character at a gate the TaxEngine can read.

    ``contra`` selects which equity bucket absorbs the mirror:
      Equity:Recognized       — for income with no prior booked gain (IRA
                                principal becoming taxable on distribution).
      Equity:UnrealizedGains  — for a brokerage sale, which merely REALIZES a
                                gain already booked by apply_growth; the leg
                                drains that bucket rather than minting new equity.
    """
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
    """Permanent liability (Liabilities:*)."""

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []


class IncomeAccount(Account):
    """A nominal income gate (Income:*).

    Not a wallet — a labeled gate on your own financial wall that accumulates
    tax-character information across the year and is swept to zero each
    December by the closing entries. It holds a balance (credit-normal, so a
    negative number as income accrues) but appreciates nothing of its own.
    """

    def apply_growth(self, period, engine: Engine) -> list[Transaction]:
        return []