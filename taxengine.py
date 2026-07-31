"""TaxEngine: the year-end reactive actor — the _assess phase.

Not a Generator. Like the CashManager it reads ledger state and posts
corrections, but it acts on the ANNUAL boundary because the US code is
non-linear: you cannot tax a month in isolation. Two dated jobs, keyed on
the month:

  December (year Y) — ACCRUE year Y's tax. Read the Income:* gates (before the
    year-end close sweeps them), apply progressive brackets to ordinary income,
    preferential stacked brackets to LTCG, and the ~85% SS inclusion, then book
    it as an EXPENSE that creates a per-tax-year liability:
        Expenses:Tax                 += tax
        Liabilities:TaxPayable:TYY   -= tax
    The expense is swept to RetainedEarnings by that same December close, so the
    tax hit lands in net worth for year Y.

  settle_month (year Y+1) — SETTLE tax year Y. Offset the prepaid withholding
    already sitting in Assets:PrepaidTax:TYY against the payable and pay the
    remainder from cash (or take a refund). This RETIRES the liability and
    consumes the prepaid; it is NOT an expense (that was booked at accrual):
        Liabilities:TaxPayable:TYY  += payable        (-> 0)
        Assets:PrepaidTax:TYY       -= prepaid        (-> 0)
        Assets:<cash>               -= payable-prepaid (pay; negative == refund)

Two tax years therefore coexist each spring — Y+1 is accruing while Y settles —
which is exactly why the payable/prepaid buckets carry a per-TYn tag: it keeps
them from smearing together. Withholding (Scenario 3), accrual, and settlement
all key off the same TY tag, so they line up automatically.

DEFERRED (named): quarterly estimated payments on the IRS calendar (withholding
is the only prepayment modeled for now; estimates would slot into the same
PrepaidTax:TYn bucket and be sized off prior-year safe harbor), underpayment
penalties, SALT/state tax, and Roth-conversion planning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .engine import Engine
from .primitives import Posting, Transaction, ZERO, money

# Sensible MFJ-ish defaults (2024 order of magnitude). Every value is
# overridable from the control file's "tax" block. Brackets are ascending
# (ceiling, rate); the final ceiling is effectively infinite.
_INF = money("1e12")
DEFAULT_BRACKETS = [
    (money(23200), Decimal("0.10")), (money(94300), Decimal("0.12")),
    (money(201050), Decimal("0.22")), (money(383900), Decimal("0.24")),
    (money(487450), Decimal("0.32")), (money(731200), Decimal("0.35")),
    (_INF, Decimal("0.37")),
]
DEFAULT_LTCG = [
    (money(94050), Decimal("0.00")), (money(583750), Decimal("0.15")),
    (_INF, Decimal("0.20")),
]
DEFAULT_STD = money(29200)
DEFAULT_SS_INCLUSION = Decimal("0.85")


def _bracket_tax(income, brackets) -> Decimal:
    """Marginal tax on ``income`` over ascending (ceiling, rate) bands."""
    tax = ZERO
    lo = ZERO
    for ceiling, rate in brackets:
        if income <= lo:
            break
        top = income if income < ceiling else ceiling
        tax = money(tax + (top - lo) * rate)
        lo = ceiling
    return tax


def _ltcg_tax(gain, ordinary_taxable, ltcg_brackets) -> Decimal:
    """LTCG stacked ON TOP of ordinary income: each slice is taxed at the rate
    for its TOTAL-income level, so ordinary income fills the 0% room first."""
    tax = ZERO
    lo = ordinary_taxable
    hi = ordinary_taxable + gain
    for ceiling, rate in ltcg_brackets:
        if lo >= hi:
            break
        top = hi if hi < ceiling else ceiling
        if top > lo:
            tax = money(tax + (top - lo) * rate)
            lo = top
    return tax


@dataclass
class TaxYearResult:
    year: int
    ordinary: Decimal
    taxable_ss: Decimal
    ltcg: Decimal
    ordinary_tax: Decimal
    ltcg_tax: Decimal
    tax: Decimal


@dataclass
class SettlementEvent:
    year: int
    payable: Decimal
    prepaid: Decimal
    paid: Decimal        # cash out (negative == refund received)


class TaxEngine:
    def __init__(self, cash_account="Assets:Checking", brackets=None,
                 ltcg_brackets=None, std_deduction=DEFAULT_STD,
                 ss_inclusion=DEFAULT_SS_INCLUSION, settle_month=4):
        self.cash_account = cash_account
        self.brackets = [(money(c), Decimal(str(r)))
                         for c, r in (brackets or DEFAULT_BRACKETS)]
        self.ltcg_brackets = [(money(c), Decimal(str(r)))
                              for c, r in (ltcg_brackets or DEFAULT_LTCG)]
        self.std_deduction = money(std_deduction)
        self.ss_inclusion = Decimal(str(ss_inclusion))
        self.settle_month = int(settle_month)
        self.results: dict[int, TaxYearResult] = {}
        self.events: list[SettlementEvent] = []

    # --- dispatch ----------------------------------------------------------

    def settle(self, period, engine: Engine) -> None:
        if period.month == 12:
            self._accrue(period, engine)
        if period.month == self.settle_month:
            self._settle_prior(period, engine)

    # --- year-end accrual --------------------------------------------------

    def _gather(self, engine: Engine):
        """Sum the year's income gates by tax character (gates are credit-normal,
        so the taxable amount is the negated balance)."""
        ordinary = ss = ltcg = ZERO
        for acct in engine.accounts("Income:"):
            amt = -engine.balance(acct)
            if amt == ZERO:
                continue
            if "CapGains" in acct:
                ltcg = money(ltcg + amt)
            elif acct == "Income:SS":
                ss = money(ss + amt)
            elif acct == "Income:Inheritance":
                continue  # a bequest is not taxable income to the recipient
            else:
                ordinary = money(ordinary + amt)
        return ordinary, ss, ltcg

    def _accrue(self, period, engine: Engine) -> None:
        year = period.year
        ordinary, ss, ltcg = self._gather(engine)
        taxable_ss = money(ss * self.ss_inclusion)
        ord_taxable = money(max(ZERO, ordinary + taxable_ss - self.std_deduction))
        ord_tax = _bracket_tax(ord_taxable, self.brackets)
        cg_tax = _ltcg_tax(ltcg, ord_taxable, self.ltcg_brackets)
        tax = money(ord_tax + cg_tax)

        self.results[year] = TaxYearResult(
            year=year, ordinary=ordinary, taxable_ss=taxable_ss, ltcg=ltcg,
            ordinary_tax=ord_tax, ltcg_tax=cg_tax, tax=tax)
        if tax == ZERO:
            return
        payable = f"Liabilities:TaxPayable:TY{year}"
        meta = {"kind": "tax-accrual", "tax_year": year}
        engine.post(Transaction(
            date=period.date,
            description=f"Accrue {year} income tax",
            postings=[
                Posting("Expenses:Tax", tax, meta=dict(meta)),
                Posting(payable, -tax, meta=dict(meta)),
            ],
            meta=dict(meta),
        ))

    # --- spring settlement of the prior tax year ---------------------------

    def _settle_prior(self, period, engine: Engine) -> None:
        y = period.year - 1
        payable_acct = f"Liabilities:TaxPayable:TY{y}"
        prepaid_acct = f"Assets:PrepaidTax:TY{y}"
        payable = money(-engine.balance(payable_acct))   # positive owed
        prepaid = money(engine.balance(prepaid_acct))    # positive prepaid
        if payable == ZERO and prepaid == ZERO:
            return

        net = money(payable - prepaid)   # >0 pay from cash; <0 refund to cash
        meta = {"kind": "tax-settle", "tax_year": y}
        postings = []
        if payable != ZERO:
            postings.append(Posting(payable_acct, payable, meta=dict(meta)))
        if prepaid != ZERO:
            postings.append(Posting(prepaid_acct, -prepaid, meta=dict(meta)))
        if net != ZERO:
            postings.append(Posting(self.cash_account, -net, meta=dict(meta)))
        engine.post(Transaction(
            date=period.date,
            description=f"Settle TY{y} tax (prepaid offsets payable)",
            postings=postings,
            meta=dict(meta),
        ))
        self.events.append(SettlementEvent(year=y, payable=payable,
                                            prepaid=prepaid, paid=net))

    # --- reporting ---------------------------------------------------------

    def report(self) -> str:
        if not self.results:
            return "No tax years assessed."
        lines = ["Tax report:"]
        for y in sorted(self.results):
            r = self.results[y]
            lines.append(
                f"  TY{y}: ordinary {r.ordinary:,.2f} (+SS {r.taxable_ss:,.2f}) "
                f"LTCG {r.ltcg:,.2f} -> tax {r.tax:,.2f} "
                f"[ord {r.ordinary_tax:,.2f} + ltcg {r.ltcg_tax:,.2f}]")
        for e in self.events:
            verb = "refund" if e.paid < ZERO else "paid"
            lines.append(
                f"  settled TY{e.year}: payable {e.payable:,.2f}, "
                f"prepaid {e.prepaid:,.2f}, {verb} {abs(e.paid):,.2f}")
        return "\n".join(lines)