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

STATE TAX AND SALT
------------------
State rides inside this engine as a parallel accrual, not a second engine:
its own brackets, its own standard deduction, its own gate->character table
(states routinely exempt SS outright, or tax capital gains as ordinary).
Its accounts sit deliberately OUTSIDE the federal prepaid prefix --

    Liabilities:StateTaxPayable:TY{y}
    Expenses:Tax:State

-- because ``_settle_prior`` sums the WHOLE ``Assets:PrepaidTax:TY{y}``
prefix, and anything parked under it would be swallowed by the federal
settlement.

State tax is deductible federally in the year PAID, not the year accrued.
With state estimates out of scope, the entire state liability leaves as cash
at the spring settlement, so TY(Y) state tax accrued in December Y is paid in
April Y+1 and deducted on the federal TY(Y+1) return. The December federal
accrual therefore reads a state payment posted eight months earlier: no
circularity, no fixed point to iterate.

``state_paid[calendar_year]`` is the SALT running total, incremented where
the state settlement posts rather than recovered by scanning the journal.
The federal deduction is then max(standard, min(SALT, cap) + other itemized).

DEFERRED (named): underpayment penalties, the annualized-income installment
method (Form 2210 Sch. AI), state estimates, state withholding, state
preferential rates, and deriving the 110% AGI test rather than taking
``safe_harbor_multiple`` on faith.
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
# Single-filer tables, same year as the MFJ figures above. A surviving spouse
# files Single from the year AFTER the death. The bands are NOT half the joint
# ones — that asymmetry is the survivor's penalty, and it is the reason a
# first death has to move the whole schedule rather than scale one input.
DEFAULT_SINGLE_BRACKETS = [
    (money(11600), Decimal("0.10")), (money(47150), Decimal("0.12")),
    (money(100525), Decimal("0.22")), (money(191950), Decimal("0.24")),
    (money(243725), Decimal("0.32")), (money(609350), Decimal("0.35")),
    (_INF, Decimal("0.37")),
]
DEFAULT_SINGLE_LTCG = [
    (money(47025), Decimal("0.00")), (money(518900), Decimal("0.15")),
    (_INF, Decimal("0.20")),
]
DEFAULT_SINGLE_STD = money(14600)
DEFAULT_SS_INCLUSION = Decimal("0.85")
DEFAULT_SAFE_HARBOR = Decimal("1.10")
DEFAULT_SALT_CAP = money(10000)

# Interest posted to this gate flows into the itemized bucket; interest on a
# non-qualifying loan goes to a plain Expenses:Interest:* account and never
# reaches the return. Routing by ACCOUNT rather than by a flag the TaxEngine
# has to be told about keeps the same read-the-ledger shape as _gather.
DEDUCTIBLE_INTEREST = "Expenses:Interest:Deductible"

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
    # The federal deduction actually taken (standard or itemized, whichever
    # won) and the parallel state accrual for the same year.
    deduction: Decimal = ZERO
    state_tax: Decimal = ZERO


@dataclass
class SettlementEvent:
    year: int
    payable: Decimal
    prepaid: Decimal
    paid: Decimal
    withheld: Decimal = ZERO
    estimated: Decimal = ZERO
    jurisdiction: str = "federal"


@dataclass
class EstimateEvent:
    year: int          # the TAX year, not the calendar year of the payment
    quarter: int
    date: _date
    amount: Decimal


@dataclass
class TaxLawChange:
    """A dated tax-law change, applied at the January of the targeted tax
    year — ahead of that year's December accrual, the same lead time
    Estate.set_filing_status uses. Each field is independently optional so a
    scenario can move one lever (say, the standard deduction) without
    restating the whole schedule. No sunset: once applied, a change stands
    until a later TaxLawChange supersedes the same field.
    """
    year: int
    brackets: list | None = None
    ltcg_brackets: list | None = None
    std_deduction: object = None
    ss_inclusion: object = None
    safe_harbor_multiple: object = None
    salt_cap: object = None
    other_itemized: object = None
    fired: bool = False


class TaxEngine:
    def __init__(self, cash_account="Assets:Checking", brackets=None,
                 ltcg_brackets=None, std_deduction=DEFAULT_STD,
                 ss_inclusion=DEFAULT_SS_INCLUSION, settle_month=4,
                 gate_character=None, gate_prefixes=None,
                 estimates=False,
                 safe_harbor_multiple=DEFAULT_SAFE_HARBOR,
                 credit_withholding=True, prior_year_tax=0,
                 prior_year_withholding=0, state=None, deductions=None,
                 law_changes=None):
        self.cash_account = cash_account
        self.brackets = [(money(c), Decimal(str(r)))
                         for c, r in (brackets or DEFAULT_BRACKETS)]
        self.ltcg_brackets = [(money(c), Decimal(str(r)))
                              for c, r in (ltcg_brackets or DEFAULT_LTCG)]
        self.std_deduction = money(std_deduction)
        self.ss_inclusion = Decimal(str(ss_inclusion))
        self.settle_month = int(settle_month)
        # Mutable: Estate flips this to "single" the year after a death.
        self.filing_status = "mfj"
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
        # State is opt-in the same way estimates are: no "state" block means
        # no state accrual, so every pre-S9.5 control file keeps its cash
        # path untouched. A flat "rate" is sugar for a one-tier bracket list,
        # so both shapes go through the one _bracket_tax path.
        self.state = dict(state) if state else None
        if self.state is not None:
            brackets_ = self.state.get("brackets") or [
                (_INF, self.state.get("rate", 0))]
            self.state_brackets = [(money(c), Decimal(str(r)))
                                   for c, r in brackets_]
            self.state_std = money(self.state.get("std_deduction", 0))
            self.state_gate_character = dict(DEFAULT_GATE_CHARACTER)
            self.state_gate_character.update(
                self.state.get("gate_character") or {})
            self.state_gate_prefixes = (
                tuple(self.state["gate_prefixes"])
                if self.state.get("gate_prefixes") else DEFAULT_GATE_PREFIXES)
        ded = deductions or {}
        self.salt_cap = money(ded.get("salt_cap", DEFAULT_SALT_CAP))
        self.other_itemized = money(ded.get("other_itemized", 0))
        # SALT is tracked by CALENDAR year of payment, not tax year accrued.
        self.state_paid: dict[int, Decimal] = {}
        self.results: dict[int, TaxYearResult] = {}
        self.events: list[SettlementEvent] = []
        self.estimate_events: list[EstimateEvent] = []
        # Sorted so a scan always meets already-fired entries first; not
        # required for correctness (each entry checks its own year) but
        # keeps the list in the order a report reads naturally.
        self.law_changes: list[TaxLawChange] = sorted(
            (law_changes or []), key=lambda c: c.year)
        self.law_change_log: list[str] = []

    def character_of(self, account: str, state: bool = False) -> str:
        """Exact match first, then ordered prefix fallback, else ORDINARY —
        an unrecognized gate is taxed as ordinary income rather than silently
        dropped, since dropping it would understate tax owed."""
        table = self.state_gate_character if state else self.gate_character
        prefixes = self.state_gate_prefixes if state else self.gate_prefixes
        if account in table:
            return table[account]
        for prefix, character in prefixes:
            if account.startswith(prefix):
                return character
        return ORDINARY

    def set_filing_status(self, status: str = "single", brackets=None,
                          ltcg_brackets=None, std_deduction=None) -> None:
        """Swap the whole rate schedule. Called by Estate, never by the loop.

        Replacing brackets/std_deduction IN PLACE, rather than keying every
        lookup on a status string, leaves _accrue and quarterly_amount
        untouched: they already read self.brackets. A filing-status change is
        then a mutation of this object, which is what a death is everywhere
        else in the model too.
        """
        self.filing_status = status
        if status == "single":
            brackets = brackets or DEFAULT_SINGLE_BRACKETS
            ltcg_brackets = ltcg_brackets or DEFAULT_SINGLE_LTCG
            if std_deduction is None:
                std_deduction = DEFAULT_SINGLE_STD
        if brackets is not None:
            self.brackets = [(money(c), Decimal(str(r))) for c, r in brackets]
        if ltcg_brackets is not None:
            self.ltcg_brackets = [(money(c), Decimal(str(r)))
                                  for c, r in ltcg_brackets]
        if std_deduction is not None:
            self.std_deduction = money(std_deduction)

    # --- prepaid buckets ---------------------------------------------------

    def withheld_account(self, year: int) -> str:
        return f"Assets:PrepaidTax:TY{year}"

    def estimated_account(self, year: int) -> str:
        return f"Assets:PrepaidTax:TY{year}:Estimated"

    def prepaid_prefix(self, year: int) -> str:
        return f"Assets:PrepaidTax:TY{year}"

    def state_payable_account(self, year: int) -> str:
        """Outside the prepaid prefix on purpose — see the module docstring."""
        return f"Liabilities:StateTaxPayable:TY{year}"

    # --- the deduction choice ----------------------------------------------

    def itemized_for(self, year: int, engine: Engine | None = None) -> Decimal:
        """SALT paid during CALENDAR ``year``, capped, plus other itemized,
        plus deductible interest accrued so far this year.

        The interest term is read from the ledger rather than registered by
        whatever posted it, so a new deductible-interest source needs no wiring
        here — it just posts to DEDUCTIBLE_INTEREST. ``engine`` is optional so
        the SALT-only arithmetic stays unit-testable without a ledger.
        """
        salt = money(self.state_paid.get(year, ZERO))
        if salt > self.salt_cap:
            salt = self.salt_cap
        interest = (money(engine.balance(DEDUCTIBLE_INTEREST))
                    if engine is not None else ZERO)
        return money(salt + self.other_itemized + interest)

    def deduction_for(self, year: int, engine: Engine | None = None) -> Decimal:
        itemized = self.itemized_for(year, engine)
        return itemized if itemized > self.std_deduction else self.std_deduction

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
        if period.month == 1:
            self._apply_law_changes(period)
        if period.month == 12:
            self._accrue(period, engine)
            if self.state is not None:
                self._accrue_state(period, engine)
        if period.month == self.settle_month:
            self._settle_prior(period, engine)
            if self.state is not None:
                self._settle_state_prior(period, engine)

    def _apply_law_changes(self, period) -> None:
        """Tax-year-boundary check: any not-yet-fired change whose year has
        arrived is applied now, in January, well ahead of the December
        accrual it must be in force for — the same lead time
        Estate._change_filing_status relies on."""
        for chg in self.law_changes:
            if not chg.fired and period.year >= chg.year:
                self._apply_law_change(chg, period)

    def _apply_law_change(self, chg: TaxLawChange, period) -> None:
        chg.fired = True
        if chg.brackets is not None:
            self.brackets = [(money(c), Decimal(str(r)))
                             for c, r in chg.brackets]
        if chg.ltcg_brackets is not None:
            self.ltcg_brackets = [(money(c), Decimal(str(r)))
                                  for c, r in chg.ltcg_brackets]
        if chg.std_deduction is not None:
            self.std_deduction = money(chg.std_deduction)
        if chg.ss_inclusion is not None:
            self.ss_inclusion = Decimal(str(chg.ss_inclusion))
        if chg.safe_harbor_multiple is not None:
            self.safe_harbor_multiple = Decimal(str(chg.safe_harbor_multiple))
        if chg.salt_cap is not None:
            self.salt_cap = money(chg.salt_cap)
        if chg.other_itemized is not None:
            self.other_itemized = money(chg.other_itemized)
        self.law_change_log.append(
            f"{period.date}  TY{chg.year} tax law change applied")

    def _gather(self, engine: Engine, state: bool = False):
        ordinary = ss = preferential = ZERO
        for acct in engine.accounts("Income:"):
            amt = -engine.balance(acct)
            if amt == ZERO:
                continue
            character = self.character_of(acct, state=state)
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
        # SALT paid THIS calendar year, not the year being accrued.
        deduction = self.deduction_for(year, engine)
        ord_taxable = money(max(ZERO, ordinary + taxable_ss - deduction))
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
            preferential_tax=pref_tax, tax=tax, withheld=withheld,
            deduction=deduction)
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

    def _accrue_state(self, period, engine: Engine) -> None:
        """Parallel December accrual on the state's own schedule.

        Everything the state includes is taxed on one bracket ladder:
        preferential income is folded into the base rather than given its own
        stacked schedule, because states that grant a preferential rate are
        out of scope. A state that exempts a gate outright says so in
        ``gate_character``, and the gate never reaches this arithmetic.
        """
        year = period.year
        ordinary, ss, preferential = self._gather(engine, state=True)
        taxable_ss = money(ss * self.ss_inclusion)
        base = money(max(ZERO, ordinary + taxable_ss + preferential
                         - self.state_std))
        tax = _bracket_tax(base, self.state_brackets)
        result = self.results.get(year)
        if result is not None:
            result.state_tax = tax
        if tax == ZERO:
            return
        payable = self.state_payable_account(year)
        meta = {"kind": "state-tax-accrual", "tax_year": year}
        engine.post(Transaction(
            date=period.date,
            description=f"Accrue {year} state income tax",
            postings=[
                Posting("Expenses:Tax:State", tax, meta=dict(meta)),
                Posting(payable, -tax, meta=dict(meta)),
            ],
            meta=dict(meta),
        ))

    def _settle_state_prior(self, period, engine: Engine) -> None:
        """The whole state liability leaves as cash — no state estimates and
        no state withholding, so there is nothing to offset. The payment is
        booked into ``state_paid`` under the CALENDAR year the cash moved,
        which is what the federal deduction reads."""
        y = period.year - 1
        acct = self.state_payable_account(y)
        payable = money(-engine.balance(acct))
        if payable == ZERO:
            return
        meta = {"kind": "state-tax-settle", "tax_year": y}
        engine.post(Transaction(
            date=period.date,
            description=f"Settle TY{y} state income tax",
            postings=[
                Posting(acct, payable, meta=dict(meta)),
                Posting(self.cash_account, -payable, meta=dict(meta)),
            ],
            meta=dict(meta),
        ))
        self.state_paid[period.year] = money(
            self.state_paid.get(period.year, ZERO) + payable)
        self.events.append(SettlementEvent(
            year=y, payable=payable, prepaid=ZERO, paid=payable,
            jurisdiction="state"))

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
            if r.state_tax != ZERO:
                lines.append(
                    f"    deduction {r.deduction:,.2f}, "
                    f"state tax {r.state_tax:,.2f}")
        for e in self.estimate_events:
            lines.append(
                f"  {e.date}  TY{e.year} Q{e.quarter} estimate "
                f"{e.amount:,.2f}")
        for e in self.events:
            verb = "refund" if e.paid < ZERO else "paid"
            if e.jurisdiction != "federal":
                lines.append(
                    f"  settled TY{e.year} {e.jurisdiction}: "
                    f"payable {e.payable:,.2f}, {verb} {abs(e.paid):,.2f}")
                continue
            lines.append(
                f"  settled TY{e.year}: payable {e.payable:,.2f}, "
                f"prepaid {e.prepaid:,.2f} (withheld {e.withheld:,.2f} "
                f"+ estimated {e.estimated:,.2f}), "
                f"{verb} {abs(e.paid):,.2f}")
        for msg in self.law_change_log:
            lines.append(f"  {msg}")
        return "\n".join(lines)