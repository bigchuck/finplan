"""TaxEngine: the year-end reactive actor — the _assess phase."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class SettlementEvent:
    year: int
    payable: Decimal
    prepaid: Decimal
    paid: Decimal


class TaxEngine:
    def __init__(self, cash_account="Assets:Checking", brackets=None,
                 ltcg_brackets=None, std_deduction=DEFAULT_STD,
                 ss_inclusion=DEFAULT_SS_INCLUSION, settle_month=4,
                 gate_character=None, gate_prefixes=None):
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
        self.results: dict[int, TaxYearResult] = {}
        self.events: list[SettlementEvent] = []

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

        self.results[year] = TaxYearResult(
            year=year, ordinary=ordinary, taxable_ss=taxable_ss,
            preferential=preferential, ordinary_tax=ord_tax,
            preferential_tax=pref_tax, tax=tax)
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
        prepaid_acct = f"Assets:PrepaidTax:TY{y}"
        payable = money(-engine.balance(payable_acct))
        prepaid = money(engine.balance(prepaid_acct))
        if payable == ZERO and prepaid == ZERO:
            return

        net = money(payable - prepaid)
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
        for e in self.events:
            verb = "refund" if e.paid < ZERO else "paid"
            lines.append(
                f"  settled TY{e.year}: payable {e.payable:,.2f}, "
                f"prepaid {e.prepaid:,.2f}, {verb} {abs(e.paid):,.2f}")
        return "\n".join(lines)