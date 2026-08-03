"""TaxEngine: the reactive tax actor.

Two phases, deliberately:

  _assess (December / settle_month) — the ANNUAL jobs. Accrue year Y's tax
    from the Income gates, and each spring settle TY(Y-1) by offsetting the
    prepaid buckets against the payable.

  _fund (Apr / Jun / Sep, and the following Jan) — quarterly ESTIMATED
    payments. These live in the fund phase, ahead of cover_shortfall,
    because an estimate is a KNOWN, DATED obligation rather than a reaction
    to year-end state: the cash has to leave before the CashManager decides
    whether the month breached its floor, or every estimate goes unfunded
    for a month and shows up as a phantom transient breach.

PREPAID SUB-ACCOUNTS
--------------------
Prepayments split by kind under the per-tax-year prefix:

    Assets:PrepaidTax:TY{y}             withholding (posted by IRA pulls)
    Assets:PrepaidTax:TY{y}:Estimated   quarterly estimates (posted here)

They are separate because the safe-harbor sizing rule NETS prior-year
withholding out of the target, so withholding has to be a balance you can
read rather than a journal you have to scan. Settlement sums the whole
prefix, so splitting the withholding leg into its own ``:Withheld`` leaf
later needs no change in this module.

DEFERRED (named): underpayment penalties, the annualized-income installment
method (Form 2210 Sch. AI), state estimates, and deriving the 110% AGI test
rather than taking ``safe_harbor_multiple`` on faith.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal

from .engine import Engine
from .primitives import Posting, Transaction, ZERO, money

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
DEFAULT_SAFE_HARBOR = Decimal("1.10")

# IRS estimated-payment calendar for tax year n: Q1/Q2/Q3 fall inside year n,
# but Q4 falls in JANUARY of year n+1. The cash date and the tax year come
# apart — which is precisely why the prepaid buckets carry a TY tag.
ESTIMATE_MONTHS = {4: 1, 6: 2, 9: 3}   # month of year n -> quarter of TY n
ESTIMATE_JAN_QUARTER = 4               # month 1 of year n+1 -> Q4 of TY n

# Tax character buckets. PREFERENTIAL covers everything taxed on the LTCG
# stacked-bracket schedule — long-term cap gains AND qualified dividends —
# since the IRS treats them identically once stacked on top of ordinary
# income. EXEMPT means "not taxable to the recipient" (e.g. an inheritance).
ORDINARY = "ordinary"
PREFERENTIAL = "preferential"
SS = "ss"
EXEMPT = "exempt"

# Exact-match gate -> character. Explicit over substring-matching so a new
# gate can never silently mis-route by accidentally containing a keyword.
DEFAULT_GATE_CHARACTER: dict[str, str] = {
    "Income:SS": SS,
    "Income:Inheritance": EXEMPT,
    "Income:CapGains:LT": PREFERENTIAL,
    "Income:Dividends:Qualified": PREFERENTIAL,
    "Income:Dividends:Ordinary": ORDINARY,
    "Income:Ordinary": ORDINARY,
    "Income:Interest": ORDINARY,
}

# Ordered prefix fallback for any Income: gate NOT in the exact table above
# (e.g. a scenario-declared custom gate) — first matching prefix wins.
# Unmatched gates default to ORDINARY (see character_of).
DEFAULT_GATE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Income:CapGains", PREFERENTIAL),
    ("Income:Dividends:Qualified", PREFERENTIAL),
    ("Income:Dividends", ORDINARY),
    ("Income:SS", SS),
    ("Income:Inheritance", EXEMPT),
)


def _bracket_tax(income, brackets) -> Decimal:
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
    preferential: Decimal   # LTCG + qualified dividends, same stacked schedule
    ordinary_tax: Decimal
    preferential_tax: Decimal
    tax: Decimal
    # Withholding on the books at December accrual. Snapshotted because the
    # spring settlement zeroes the prepaid buckets, and next year's Q2/Q3
    # estimates are sized off this number months AFTER that wipe.
    withheld: Decimal = ZERO


@dataclass
class SettlementEvent:
    year: int
    payable: Decimal
    prepaid: Decimal
    paid: Decimal
    withheld: Decimal = ZERO
    estimated: Decimal = ZERO


@dataclass
class EstimateEvent:
    year: int          # the TAX year, not the calendar year of the payment
    quarter: int
    date: _date
    amount: Decimal


class TaxEngine:
    def __init__(self, cash_account="Assets:Checking", brackets=None,
                 ltcg_brackets=None, std_deduction=DEFAULT_STD,
                 ss_inclusion=DEFAULT_SS_INCLUSION, settle_month=4,
                 gate_character=None, gate_prefixes=None,
                 estimates=False,
                 safe_harbor_multiple=DEFAULT_SAFE_HARBOR,
                 credit_withholding=True, prior_year_tax=0,
                 prior_year_withholding=0):
        self.cash_account = cash_account
        self.brackets = [(money(c), Decimal(str(r)))
                         for c, r in (brackets or DEFAULT_BRACKETS)]
        self.ltcg_brackets = [(money(c), Decimal(str(r)))
                              for c, r in (ltcg_brackets or DEFAULT_LTCG)]
        self.std_deduction = money(std_deduction)
        self.ss_inclusion = Decimal(str(ss_inclusion))
        self.settle_month = int(settle_month)
        # Exact-match table wins over the ordered prefix fallback; a caller
        # supplying gate_character only overrides/extends the defaults
        # rather than replacing the whole table.
        self.gate_character = dict(DEFAULT_GATE_CHARACTER)
        if gate_character:
            self.gate_character.update(gate_character)
        self.gate_prefixes = tuple(gate_prefixes) if gate_prefixes else DEFAULT_GATE_PREFIXES
        # Estimates default OFF. Switching them on changes the cash path of
        # every scenario that declares a "tax" block, so it has to be an
        # opt-in declaration rather than a silent behaviour change under
        # control files written before S9.
        self.estimates = bool(estimates)
        self.safe_harbor_multiple = Decimal(str(safe_harbor_multiple))
        self.credit_withholding = bool(credit_withholding)
        # Seeds for the first simulated tax year, whose safe-harbor basis is
        # a year the simulation never ran.
        self.prior_year_tax = money(prior_year_tax)
        self.prior_year_withholding = money(prior_year_withholding)
        self.results: dict[int, TaxYearResult] = {}
        self.events: list[SettlementEvent] = []
        self.estimate_events: list[EstimateEvent] = []

    def character_of(self, account: str) -> str:
        """Exact match first, then ordered prefix fallback, else ORDINARY —
        an unrecognized gate is taxed as ordinary income rather than silently
        dropped, since dropping it would understate tax owed."""
        if account in self.gate_character:
            return self.gate_character[account]
        for prefix, character in self.gate_prefixes:
            if account.startswith(prefix):
                return character
        return ORDINARY

    # --- prepaid buckets ---------------------------------------------------

    def withheld_account(self, year: int) -> str:
        return f"Assets:PrepaidTax:TY{year}"

    def estimated_account(self, year: int) -> str:
        return f"Assets:PrepaidTax:TY{year}:Estimated"

    def prepaid_prefix(self, year: int) -> str:
        return f"Assets:PrepaidTax:TY{year}"

    # --- quarterly estimates (fund phase) ----------------------------------

    def quarterly_amount(self, tax_year: int) -> Decimal:
        """Safe harbor: a multiple of the PRIOR year's total tax, net of that
        year's withholding. Prior-year figures only — the 90%-of-current rule
        would require peeking at an answer no taxpayer can have on April 15,
        and modelling the cash-flow lever honestly is the point of S9.
        """
        prior = self.results.get(tax_year - 1)
        if prior is None:
            tax, withheld = self.prior_year_tax, self.prior_year_withholding
        else:
            tax, withheld = prior.tax, prior.withheld
        target = money(tax * self.safe_harbor_multiple)
        if self.credit_withholding:
            target = money(target - withheld)
        if target <= ZERO:
            return ZERO
        return money(target / 4)

    def pay_estimates(self, period, engine: Engine) -> None:
        """Fund-phase entry point. Fires only on the IRS calendar."""
        if not self.estimates:
            return
        if period.month in ESTIMATE_MONTHS:
            tax_year, quarter = period.year, ESTIMATE_MONTHS[period.month]
        elif period.month == 1:
            tax_year, quarter = period.year - 1, ESTIMATE_JAN_QUARTER
            # Q4 lands in January of the FOLLOWING year, so the first
            # simulated January points at a tax year the run never modelled.
            # Paying it would mint a prepaid balance with no payable behind
            # it and hand back a refund out of nothing.
            if tax_year not in self.results:
                return
        else:
            return

        amount = self.quarterly_amount(tax_year)
        if amount <= ZERO:
            return

        meta = {"kind": "tax-estimate", "tax_year": tax_year,
                "quarter": quarter}
        engine.post(Transaction(
            date=period.date,
            description=f"TY{tax_year} Q{quarter} estimated tax payment",
            postings=[
                Posting(self.cash_account, -amount, meta=dict(meta)),
                Posting(self.estimated_account(tax_year), amount,
                        meta=dict(meta)),
            ],
            meta=dict(meta),
        ))
        self.estimate_events.append(EstimateEvent(
            year=tax_year, quarter=quarter, date=period.date, amount=amount))

    def settle(self, period, engine: Engine) -> None:
        if period.month == 12:
            self._accrue(period, engine)
        if period.month == self.settle_month:
            self._settle_prior(period, engine)

    def _gather(self, engine: Engine):
        ordinary = ss = preferential = ZERO
        for acct in engine.accounts("Income:"):
            amt = -engine.balance(acct)
            if amt == ZERO:
                continue
            character = self.character_of(acct)
            if character == EXEMPT:
                continue
            elif character == SS:
                ss = money(ss + amt)
            elif character == PREFERENTIAL:
                preferential = money(preferential + amt)
            else:
                ordinary = money(ordinary + amt)
        return ordinary, ss, preferential

    def _accrue(self, period, engine: Engine) -> None:
        year = period.year
        ordinary, ss, preferential = self._gather(engine)
        taxable_ss = money(ss * self.ss_inclusion)
        ord_taxable = money(max(ZERO, ordinary + taxable_ss - self.std_deduction))
        ord_tax = _bracket_tax(ord_taxable, self.brackets)
        pref_tax = _ltcg_tax(preferential, ord_taxable, self.ltcg_brackets)
        tax = money(ord_tax + pref_tax)
        # Snapshot before the spring settlement zeroes the bucket: next
        # year's safe-harbor target is netted against this figure, and Q2/Q3
        # are computed long after the wipe.
        withheld = money(engine.balance(self.withheld_account(year)))

        self.results[year] = TaxYearResult(
            year=year, ordinary=ordinary, taxable_ss=taxable_ss,
            preferential=preferential, ordinary_tax=ord_tax,
            preferential_tax=pref_tax, tax=tax, withheld=withheld)
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

    def _settle_prior(self, period, engine: Engine) -> None:
        y = period.year - 1
        payable_acct = f"Liabilities:TaxPayable:TY{y}"
        # Sum the WHOLE prepaid prefix so every prepayment kind is consumed,
        # not just the withholding leaf.
        prepaid_bals = {a: money(engine.balance(a))
                        for a in engine.accounts(self.prepaid_prefix(y))}
        withheld = prepaid_bals.get(self.withheld_account(y), ZERO)
        estimated = prepaid_bals.get(self.estimated_account(y), ZERO)
        payable = money(-engine.balance(payable_acct))
        prepaid = money(sum(prepaid_bals.values(), ZERO))
        if payable == ZERO and prepaid == ZERO:
            return

        net = money(payable - prepaid)
        meta = {"kind": "tax-settle", "tax_year": y}
        postings = []
        if payable != ZERO:
            postings.append(Posting(payable_acct, payable, meta=dict(meta)))
        for acct, bal in sorted(prepaid_bals.items()):
            if bal != ZERO:
                postings.append(Posting(acct, -bal, meta=dict(meta)))
        if net != ZERO:
            postings.append(Posting(self.cash_account, -net, meta=dict(meta)))
        engine.post(Transaction(
            date=period.date,
            description=f"Settle TY{y} tax (prepaid offsets payable)",
            postings=postings,
            meta=dict(meta),
        ))
        self.events.append(SettlementEvent(year=y, payable=payable,
                                            prepaid=prepaid, paid=net,
                                            withheld=withheld,
                                            estimated=estimated))

    def report(self) -> str:
        if not self.results:
            return "No tax years assessed."
        lines = ["Tax report:"]
        for y in sorted(self.results):
            r = self.results[y]
            lines.append(
                f"  TY{y}: ordinary {r.ordinary:,.2f} (+SS {r.taxable_ss:,.2f}) "
                f"preferential {r.preferential:,.2f} -> tax {r.tax:,.2f} "
                f"[ord {r.ordinary_tax:,.2f} + pref {r.preferential_tax:,.2f}]")
        for e in self.estimate_events:
            lines.append(
                f"  {e.date}  TY{e.year} Q{e.quarter} estimate "
                f"{e.amount:,.2f}")
        for e in self.events:
            verb = "refund" if e.paid < ZERO else "paid"
            lines.append(
                f"  settled TY{e.year}: payable {e.payable:,.2f}, "
                f"prepaid {e.prepaid:,.2f} (withheld {e.withheld:,.2f} "
                f"+ estimated {e.estimated:,.2f}), "
                f"{verb} {abs(e.paid):,.2f}")
        return "\n".join(lines)