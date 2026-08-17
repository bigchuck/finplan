# finplan Primer

finplan is a forward-looking financial-plan simulator, not a bookkeeping
system. You don't hand it history — you declare accounts and cash flows in a
JSON **control file**, and it manufactures a monthly stream of transactions
for years or decades into the future, then reports what happened. Every
number it produces is a projection: "if these rules hold, here's the ledger
in 2045."

This primer is task-oriented: it's organized around the things you actually
configure, with a minimal working example for each, and pointers into the
test suite (`tests/test_scenarioN.py`) for deeper, verified examples — those
tests are hand-checked against expected numbers, so when you want to see
every edge case of a feature, that's the place to look.

## Running it

```
cd src
python -m finplan.cli <control-file.json>                              # lists scenarios in the file
python -m finplan.cli <control-file.json> <scenario-name>               # runs one
python -m finplan.cli <control-file.json> <scenario-name> --report <name>  # runs it with a named report
python -m finplan.cli <control-file.json> <scenario-name> --out output/run.txt  # writes output to a file instead of stdout
```

You must run this from the `src/` directory (or otherwise have it on
`PYTHONPATH`) so the `finplan` package resolves. `data/all_scenarios.json` in
this repo is a real example file — try:

```
python -m finplan.cli ../data/all_scenarios.json
```

to see the scenario list, then pick one by name to run it.

A control file always has this two-level shape — a `scenarios` map at the
top, one flat plan definition per entry:

```json
{
  "scenarios": {
    "base": {
      "start": "2026-01-01",
      "years": 1,
      "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00", "apr": "0.03"}
      ]
    }
  }
}
```

Run it: `python -m finplan.cli myfile.json base` prints the final ledger
after 1 year — one interest-bearing checking account, nothing else. That's
the entire minimum viable control file (`tests/test_scenario1.py`).

Two things to know about the CLI's behavior, since they're easy to trip
over:
- **Omitting the scenario name doesn't run anything** — it just lists the
  scenarios in the file and exits. If you meant to run one, you forgot the
  second argument.
- **The `report` argument only matters if the file has a top-level
  `"reports"` block** (see [Reports](#reports--output)). No `reports` block,
  or exactly one entry in it, and you never need to pass a report name.

## The mental model

Every month of the simulation runs the same five phases, in this order:

1. **Transition** — only relevant if a `legacy` (death) block is configured; stops streams that end this month, before anything else fires.
2. **Accrue** — every account and every flow (stream, shock, schedule, …) computes what it wants to post, **based on the ledger as it stood at the start of the month** — not on what any other object did this same month. Everything is collected, then posted together. This is why the order you list things in `accounts`/`streams`/`shocks`/etc. never changes the result: nothing can see a sibling's same-month output.
3. **Fund** — cash management sweeps run (see [Cash management](#cash-management)), pulling from a waterfall of accounts to keep your cash floor topped up. **This is the one place list order does matter** — if you have more than one `cash_management` block, they run in the order you declared them, and one block's forced withdrawal can push a different account below its own floor within the same tick.
4. **Assess** — taxes accrue/settle if a `tax` block is configured.
5. **Close** — only in December: zeroes out `Income:*`/`Expenses:*` accounts for the year, and double-checks that brokerage unrealized-gains bookkeeping still balances.

Two invariants are enforced continuously, not just at the end: every single
transaction must balance to zero (it's literally impossible to construct an
unbalanced one), and the whole ledger must sum to zero after every posting.
If you ever see a nonzero "(whole-ledger sum)" line in the output, that's a
bug, not a rounding artifact.

One sign-convention note that trips people up reading output: `Assets` and
`Expenses` balances increase as positive numbers; `Liabilities`, `Equity`,
and `Income` balances increase as *negative* numbers (double-entry
convention). So a healthy `Income:Interest` account shows as increasingly
negative, and a growing HELOC balance is negative too — that's correct, not
a bug.

## Scenarios: one file, many plans, `extends`

Every scenario is a flat dict of blocks (`accounts`, `streams`, `tax`, …).
Rather than copy-pasting a whole plan to model "what if we retire two years
later," a scenario can `extend` another and restate only what's different:

```json
{
  "scenarios": {
    "base": {
      "start": "2026-01-01", "years": 1,
      "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00", "apr": "0.03"}
      ]
    },
    "higher_rate": {
      "extends": "base",
      "accounts": [{"name": "Assets:Checking", "apr": "0.05"}]
    }
  }
}
```

`higher_rate` inherits everything from `base` and patches just the `apr`
field — `type`, `owner`, and `opening` all still come from the parent
untouched. `extends` chains can go arbitrarily deep (a scenario can extend a
scenario that extends another), and merging always happens root-to-leaf.

The merge rules, applied child-onto-parent:

- **Two dicts** merge key-by-key, recursively; new keys are added.
- **A list where every entry shares an identity field** (`"name"` for
  accounts/streams/shocks/recurring_expenses/schedules/contributions;
  `"account"` for `cash_management` blocks, which have no `"name"`) is
  merged **by that identity**: a child entry with a matching identity
  patches just the fields it restates (siblings stay inherited); a new
  identity is appended to the list; and `{"<id-key>": "X", "_remove": true}`
  **deletes** an inherited entry entirely.
- **Anything else** (plain scalars like `years`, or a list that isn't
  identity-keyed) — the child's value replaces the parent's wholesale.

So you can retune one field on one inherited entry, add a new entry, or
delete an inherited one, all without restating the rest:

```json
"drop_checking": {
  "extends": "two_acct",
  "accounts": [{"name": "Assets:Checking", "_remove": true}]
}
```

This applies to `cash_management` too — even though its blocks are keyed by
`"account"` instead of `"name"`:

```json
"cash_mgmt_retuned": {
  "extends": "cash_mgmt",
  "cash_management": [{"account": "Assets:Checking", "floor": "2000.00"}]
}
```

only patches that one block's `floor`; its `target`/`waterfall` and any
other `cash_management` blocks in the parent stay exactly as inherited.

`tests/test_scenarios.py` is the living, executable spec for all of this —
run `python tests/test_scenarios.py` to see it pass, or read it for more
worked examples (multi-level `extends`, cycle detection, the `_remove`
sentinel on both account and cash-management lists).

### Mixins: ad hoc combinations without pre-declaring every scenario

`extends` is for authoring a lineage of complete scenarios ahead of time.
For picking a combination on the fly — "the base plan, but with moderate
brokerage growth, plus cash sweeps" — without adding a new named scenario
for every combination you might want to study, use a **mixin** instead. A
mixin is a fragment under a separate top-level `"mixins"` map: it isn't a
full scenario (no `start`/`years` required) and can't be run on its own.

```json
{
  "scenarios": { "base": { "...": "..." } },
  "mixins": {
    "moderate_growth": {
      "accounts": [{"name": "Assets:Brokerage", "apr": "0.06"}]
    },
    "cash_sweeps": {
      "cash_management": [
        {"account": "Assets:Checking", "floor": "1000.00",
         "target": "5000.00", "waterfall": []}
      ]
    }
  }
}
```

Select mixins at run time with a repeatable `--mixin` flag, layered onto the
resolved base scenario left to right (later mixin wins on a shared field),
using the exact same merge rules as `extends`:

```
python -m finplan.cli all_scenarios.json base --mixin moderate_growth --mixin cash_sweeps
```

Running the file with no scenario name lists both scenarios and any
declared mixins.

## Accounts

Declared under `"accounts"`; each entry needs `type` and `name`, plus an
optional `opening` balance. Every other key becomes a free-form attribute —
there's no schema enforcement at this layer, so a misspelled attribute name
(e.g. `dividend_yeild`) is silently ignored rather than rejected. If a
feature you configured doesn't seem to be doing anything, check spelling
first.

| `type` | Class | Notable attrs |
|---|---|---|
| `asset` | `AssetAccount` | `apr` (nonzero → auto-attaches interest income), `withholding` |
| `liability` | `LiabilityAccount` | — |
| `heloc` | `HELOCAccount` | `credit_limit`, `rate`, `opened`, `maturity`, `origination_fee`, `payment` (mode/amount), `deductible`, `payment_from` |
| `brokerage` | `BrokerageAccount` | `growth`, `basis`, `dividend_yield` (nonzero → auto-attaches a dividend policy), `qualified_fraction`, `reinvest`, `dividend_to` |
| `traditional_ira` | `TraditionalIRAAccount` | `growth`, `withholding` (defaults to 0.20) |
| `roth` | `RothAccount` | `growth` |

A few things worth knowing per type:

- **Brokerage basis.** If you omit `basis`, it defaults to `opening` (no
  embedded gain at day one). If you set `basis` lower than `opening`, the
  difference posts as embedded unrealized gain at the start. Only the
  *gain slice* of a later sale is ever taxed — return of your own basis
  never is.
- **HELOC balances are negative** (liability convention), and its
  `capacity` is `credit_limit - owed - floor` — inverted from an asset
  account's `balance - floor`. A capitalizing HELOC (no payments, interest
  just adds to what's owed) can exceed `credit_limit` over time — nothing
  invents a repayment that didn't happen.
- **`owner` is worth tagging on every account** even if you don't need it
  yet — it drives survivorship and filing-status logic if you later add a
  `legacy` block.

## Flows

Flows are how money moves. Order never matters between flows (see
[mental model](#the-mental-model)) — list them in whatever order reads best.

| Block | Direction | Fires | Sizing |
|---|---|---|---|
| `streams` | external → your account | every active month | fixed monthly amount |
| `shocks` | account → account | once, on a specific month | fixed amount |
| `recurring_expenses` | account → account | every `interval` months | fixed amount |
| `contributions` | account → account | once a year | fixed / fraction of balance / excess over threshold |
| `schedules` | account → account | once a year (or monthly for RMD) | fixed / RMD table / conversion |

**Streams** model recurring *external* income — Social Security, a pension,
an annuity — money entering the system, not moving between your own
accounts:

```json
{"name": "SS", "to": "Assets:Checking", "income": "Income:SS",
 "amount": "3000.00", "start": "2026-01-01", "owner": "chuck"}
```

(`tests/test_scenario2.py`) `start`/`end` gate by month, not day — a
`2026-07-15` start still fires the whole of July.

**Shocks** are one-off, unmodeled events — a windfall, a big one-time
expense — firing exactly once in the month of `when`:

```json
{"name": "NewRoof", "from": "Assets:Checking", "to": "Expenses:Home",
 "amount": "25000.00", "when": "2026-06-01"}
```

(`tests/test_scenario5.py`) Point `from` at an `Income:` account instead to
model a windfall arriving rather than an expense leaving.

**Recurring expenses** are the general form of a Shock — a transfer that
repeats every `interval` months, anchored at `start` (so the anchor's actual
calendar month is preserved — starting in March with `interval: 6` fires
March and September, not January and July):

```json
{"name": "Mortgage", "from": "Assets:Checking", "to": "Expenses:Mortgage",
 "amount": "2400.00", "start": "2026-01-01", "interval": 1, "owner": "chuck"}
```

(`tests/test_scenario15.py`) Omitting `interval` defaults to monthly (`1`).

**Contributions** are recurring transfers *into* a declared account (a
savings/retirement contribution), fired once a year in calendar month
`month` (default December), sized one of three ways:

- `"mode": "fixed"` — a declared `amount`.
- `"mode": "fraction"` — `fraction` of the source account's *current*
  balance, read live each year (not inflation-scaled).
- `"mode": "excess"` — whatever the source balance exceeds `threshold` by
  (never negative, never inflation-scaled).

**Schedules** are forced or planned *withdrawals* from an account you
already declared, in one of three modes:

- `"mode": "fixed"` — a declared `amount` in a given `month`.
- `"mode": "rmd"` — Required Minimum Distribution: age- and balance-driven,
  using the built-in IRS Uniform Lifetime Table (overridable via
  `divisors`), gated by `owner_birth_year` and `rmd_start_age` (default 73).
  This always fires in January, off the *prior* year-end balance.
- `"mode": "roth_conversion"` — moves `amount` from a `traditional_ira`
  account into a Roth, recognized as full ordinary income, with **no
  withholding leg** (you fund the tax bill from elsewhere).

```json
{"name": "RMD", "source": "Assets:IRA", "to": "Assets:Checking",
 "mode": "rmd", "owner_birth_year": 1952}
```

(`tests/test_scenario7.py`) A Schedule (like the cash-management waterfall)
never overdraws — a fixed withdrawal that exceeds available capacity
silently caps at what's there instead of erroring or going negative.

**Gotchas common to all flows:**
- Dates are `"YYYY-MM-DD"` strings; day-of-month is parsed but ignored for
  scheduling — everything effectively fires "on the 1st" of its month.
- `mode` values are a closed, validated set per block — an unrecognized
  mode raises `ValueError` at build time, not silently. But a required
  companion field is enforced too (e.g. `fraction` mode needs `fraction`,
  `rmd` mode needs `owner_birth_year`) — the codebase fails loudly on these.
  A misspelled *account* or *attribute* name (not a mode) is what fails
  silently — see the accounts section above.
- Amounts should be written as quoted strings (`"3000.00"`), not bare
  floats — this keeps the exact-decimal math the whole ledger depends on.
- Zero-amount flows are a no-op, not an error — useful for "disabling"
  something in a scenario override without removing it (though `_remove`
  is more explicit for that — see [Scenarios](#scenarios-one-file-many-plans-extends)).
- Only declared dollar amounts (Stream, Shock, RecurringExpense, and
  Schedule's `fixed`/`roth_conversion` modes) get scaled by `inflation`
  under `"mode": "real"`. RMD and fraction/excess-based sizing are
  balance-driven and never inflation-scaled — they're already nominal.

## Cash management

`"cash_management"` is the "checking account autopilot": it refills a cash
account to `target` whenever it drops below `floor`, pulling from an
ordered `waterfall` of source accounts.

```json
{"account": "Assets:Checking", "floor": "10000.00", "target": "30000.00",
 "waterfall": [{"source": "Assets:Brokerage", "floor": "0.00"}]}
```

(`tests/test_scenario3.py`) Order the waterfall from most to least
preferred to draw from — conventionally taxable brokerage first, then a
traditional IRA, with Roth or a HELOC as "break glass last." Each rung has
its **own** `floor` too — a reserve on that specific source you decline to
draw below, separate from the cash account's own floor/target (easy to
conflate since both are called "floor").

**Gross-up matters.** Pulling from a withholding source (a traditional IRA,
20% by default) needs to pull *more* than the net you actually need, so
what lands in checking after withholding still covers the shortfall.
Refilling a $20,000 target from a $500,000 IRA pulls an **$18,750 gross**,
delivering **$15,000 net** after $3,750 withheld to
`Assets:PrepaidTax:TYn` — the waterfall does this math for you
automatically; you never need to gross up a `Schedule` or `Shock` amount
yourself, but if you're sizing a manual pull by hand, remember gross ≠ net.

Each fund shape differs by account type: a plain asset-to-asset pull is a
bare transfer (no tax); a brokerage sale recognizes only the *gain slice*
as income; an IRA pull recognizes the *full* amount and withholds; a HELOC
draw is borrowing, not income, with no recognition at all.

Every forced-funding month is logged with a **severity**: `SOFT`
("reserve-breach" — cash dipped below floor but the waterfall recovered it
to at least the floor) or `HARD` ("reserve-exhausted" — the whole waterfall
ran dry and cash is still short). HARD is a genuine plan-failure signal
worth watching for, not just a log line. If the shortfall is never
resolved, a fresh event logs every subsequent month it stays unresolved —
not just once.

**Multiple blocks.** `cash_management` can be a single dict (as above) or a
list of blocks — e.g. a separate managed cash account per owner
(`tests/test_scenario17.py`). Two things change once you have more than
one block:
- **Order matters** between blocks (see [mental model](#the-mental-model))
  — one block's forced withdrawal can push a different block's account
  below its floor within the same tick.
- **The implicit single default disappears.** With exactly one
  `cash_management` block, its `account` silently becomes the default
  target for dividends, HELOC payments, and tax cash wherever those aren't
  stated explicitly. With zero or multiple blocks, there's no ambiguity
  tolerated — you must set those explicitly (`dividend_to`,
  `payment_from`, `tax.cash_account`) or the build raises `ValueError`.
- If one block's managed account is a waterfall source feeding another
  block, and vice versa, that's a refill cycle — caught and rejected with
  `ValueError` at build time, naming the cycle, rather than ping-ponging
  forever at run time.

`trigger` (default `"cash-floor"`) is currently just a label copied onto
each forced transaction's metadata for later filtering — it isn't a
behavioral switch.

## Taxes

`"tax"` turns on annual accrual/settlement. Every field is optional
(current-law-ish federal brackets/deduction are the defaults), but two
sub-blocks are opt-in **by presence**, not by a flag inside them — an
absent `estimates` or `state` key means that mechanism is fully off:

```json
{"brackets": [[10000, "0.10"], [40000, "0.20"], [1000000000, "0.30"]],
 "ltcg_brackets": [[50000, "0.00"], [500000, "0.15"], [1000000000, "0.20"]],
 "std_deduction": "0.00", "ss_inclusion": "0.85", "settle_month": 4}
```

(`tests/test_scenario6.py`) Tax accrues once a year in December and settles
the following spring (`settle_month`, default April) against whatever was
prepaid via withholding or estimates — a negative settlement is a refund.
Social Security counts toward taxable income at the flat `ss_inclusion`
fraction (85% by default — this is a simplification, not the real tiered
IRS provisional-income formula). Long-term capital gains and qualified
dividends are taxed at preferential rates, **stacked on top of** ordinary
taxable income — so whether a dollar of gain is taxed at 0%, 15%, or 20%
depends on how much ordinary income already fills the brackets below it.

**Estimated payments** (`tax.estimates`), once present, size each quarter's
payment as safe harbor — `prior_year_tax * safe_harbor_multiple`, minus
prior withholding if `credit_withholding` is true — using **only prior-year
figures**, never a current-year peek. `prior_year_tax`/
`prior_year_withholding` seed the very first simulated year, which has no
real prior year to look back on. Payments land on the real IRS calendar
(Q1–Q3 within the year, Q4 in January of the *following* year while still
counting toward the earlier tax year).

**State tax** (`tax.state`) is a parallel accrual with its own brackets (or
a flat `rate`) and standard deduction. Its liability lives in a separate
account namespace from federal prepaid tax, specifically so it can't get
swept into the federal settlement by accident. State tax is federally
deductible in the year it's **paid**, not the year it accrued — so tax
year Y's state liability (accrued in December Y) reduces *federal* taxable
income for tax year Y+1, not Y. `tax.deductions` (`salt_cap`, default
$10,000; `other_itemized`) governs the itemized-vs-standard-deduction
comparison — the larger of the two is always used automatically.

**Law changes** (`tax.law_changes`) let a scenario move brackets,
deduction, SS inclusion, or other levers starting at a given future year,
with **no sunset** — once applied, a change persists until a later change
explicitly overrides the same field:

```json
{"year": 2028, "std_deduction": "40000.00"}
```

(`tests/test_scenario12.py`) Multiple law changes apply in year order, each
touching only the fields it declares.

**RMD note**: Required Minimum Distributions are not part of the tax
block at all — they're a `Schedule` with `"mode": "rmd"` (see
[Flows](#flows)), since they're really a withdrawal rule, not a tax rule
(though the withdrawal is, of course, taxable ordinary income once it
lands).

## Death and legacy

`"legacy"` declares one or more `deaths`, each with an `owner`, a `when`
date, and an optional `survivor`:

```json
{"heir_rate": "0.24",
 "deaths": [{"owner": "spouse", "when": "2028-06-01", "survivor": "chuck"}]}
```

(`tests/test_scenario11.py`) A death entry with **no `survivor`** halts the
simulation at that month and prices the estate as it stands — this is read
directly from whether you supplied `survivor`, not inferred from how many
deaths you listed, so a single-death file always means exactly what it
says.

What happens on a death:
- **Social Security is cross-stream:** the survivor keeps the *larger* of
  the household's SS streams; the smaller one stops. This can't be
  expressed as a per-stream fraction, since it compares two streams to
  each other.
- **Every other stream** the decedent owned continues at its own
  `survivorship` fraction — **defaulting to 0** (full stop). This is the
  harshest of the plausible defaults, deliberately, so a forgotten
  `survivorship` doesn't silently flatter the plan by assuming full
  continuation.
- **Filing status flips to Single the *following* tax year**, not
  immediately — a surviving spouse can still file jointly for the year of
  death itself. `tests/test_scenario11.py` shows this can actually
  *raise* tax even as household income falls, since the single-filer
  brackets aren't just half the joint ones.

If the run halts, the estate report separates gross assets from
net-to-heirs: `heir_rate` is charged only against tax-deferred (IRA)
balances, since that money still carries un-paid income tax to heirs —
brokerage assets get a full basis step-up (no gain tax at all), and Roth
money is already tax-free. Debt (a HELOC, say) survives the owner and is a
first claim against the estate — it can make an otherwise-healthy estate
net negative, which the report flags explicitly rather than folding into
one number.

## Inflation

`"inflation"` is opt-in — omit it entirely and the whole feature is inert
(mode `"nominal"`, meaning every declared dollar amount is used exactly as
written for the whole run). Under `"mode": "real"`, a year-keyed,
carry-forward `rates` schedule escalates dollar amounts:

```json
{"mode": "real", "rates": {"2026": "0.12", "2028": "0.06"}}
```

(`tests/test_scenario13.py`) A year with no declared rate inherits the most
recent prior year's rate — you only need entries where the rate changes.

Only **declared dollar amounts** escalate: `streams`, `shocks`,
`recurring_expenses`, and a `schedule`'s `fixed`/`roth_conversion` amounts.
It never touches `apr`, `growth`, or `dividend_yield` (already nominal
return assumptions by convention), and never touches RMD (already
balance-driven, hence already nominal). The `rates` schedule must reach
back to cover the simulation's start year, or the build fails immediately
— it deliberately never falls back to silently assuming zero escalation
for uncovered years.

## Reports & output

By default (no `reports` block), every run prints: the full final ledger
with a whole-ledger-sum sanity check, each cash manager's forced-withdrawal
report, the tax engine's report (if `tax` is configured), the estate report
(if a `legacy` block halted the run), and a schedule report listing every
`Schedule`'s firings — including any that were capped by available
capacity or never fired at all.

If you want to see the plan's trajectory *during* the run, not just the
final state, add a top-level `"reports"` block:

```json
{"reports": {"yearly-detail": {"mode": "detail", "frequency": "yearly"}}}
```

Each report entry supports: `mode` (`"summary"`, the default, or
`"detail"` for periodic snapshots), `show_start` (print a snapshot before
period 0), `timing` (`"before_sweep"`, `"after_sweep"`, or `"both"` —
relative to the year-end close), `frequency` (`"yearly"` default,
`"monthly"`, or an explicit list of `{"year":, "month":}` entries),
`include`/`exclude` (account-name prefix filters — these match by prefix,
so `"Assets:Checking"` also matches `"Assets:Checking:Sub"`), and
`transactions` (a list of prefixes to also dump matching journal entries
for). A report configuration only changes what gets *printed* — it never
changes what the simulation actually does, so switching between summary
and detail mode never changes the final numbers.

Selecting a report from the CLI: pass its name with `--report`. If the file
has no `reports` block, don't pass one. If it has exactly one entry, it's
used automatically. If it has more than one and you didn't name one, the
CLI lists the available names and exits — same pattern as omitting the
scenario name.

By default all of this prints to stdout. Pass `--out <path>` (relative to
the current directory, e.g. `--out output/base-detail.txt`) to write it to
a file instead — parent directories are created automatically, and the CLI
prints a one-line confirmation to the real stdout once the file is
written.

## Common gotchas

A consolidated list of the things most likely to bite you, pulled from
across every section above:

- **Typos in account attribute names fail silently.** There's no schema
  validation on `attrs` — a misspelled `dividend_yield` just means no
  dividend policy ever attaches, with no error. If a feature seems inert,
  check spelling first.
- **Typos in account/attribute *names you reference* fail loudly** —
  referencing an undeclared account in a `waterfall`, `schedule.source`,
  or `contribution.to` raises `KeyError` at build time.
- **`mode` fields are validated** — an unrecognized mode raises
  `ValueError` at build; so does a missing companion field for the mode
  you chose (e.g. `fraction` mode without `fraction`).
- **Dates are `"YYYY-MM-DD"` strings, day ignored.** Everything schedules
  at month granularity.
- **Amounts should be quoted strings**, not floats, to preserve exact
  decimal math.
- **`cash_management`'s implicit defaults only exist with exactly one
  block.** Add a second block (e.g. per owner) and every place that
  relied on the implicit default (`dividend_to`, HELOC `payment_from`,
  `tax.cash_account`) must be stated explicitly or the build raises.
- **Gross ≠ net on a withholding source.** The cash-management waterfall
  grosses up automatically; if you're hand-sizing a pull from an IRA,
  remember withholding eats into what lands in checking.
- **RMDs live under `schedules`, mode `"rmd"`** — not in the `tax` block,
  not as an account attribute.
- **`estimates` and `state` under `tax` are opt-in by key presence**, not
  by an `enabled` flag — omit the block to turn the feature off, don't
  set it to some falsy value inside.
- **SALT/state-tax deductibility is keyed by payment year, not accrual
  year** — a state tax bill accrued in year Y is paid (and thus federally
  deductible) in year Y+1.
- **`legacy.deaths` can't be an empty list** — omit the whole `legacy`
  block instead, or the build raises.
- **`survivorship` defaults to 0**, not full continuation — an inherited
  stream silently stops at the owner's death unless you say otherwise.
- **Inflation under `"mode": "real"` needs `rates` reaching back to the
  simulation's start year**, or the build fails immediately.
- **List order never matters** among accounts/streams/shocks/etc. — except
  **`cash_management` blocks, where order does matter** when there's more
  than one.

## Where to look for more

Every feature above has a corresponding test file with worked, hand-checked
examples — these are the authoritative spec when you need more detail than
this primer gives. Run any of them directly: `python tests/test_scenarioN.py`.

| Topic | Test file(s) |
|---|---|
| Minimal end-to-end scenario, interest, year-end close | `test_scenario1.py` |
| Streams (recurring external income) | `test_scenario2.py` |
| Cash management: floor/target/waterfall, insolvency | `test_scenario3.py` |
| Brokerage growth, basis, embedded gain, realized gain on sale | `test_scenario4.py` |
| Shocks (one-off events) | `test_scenario5.py` |
| Tax basics: accrual, settlement, LTCG stacking, SS inclusion | `test_scenario6.py` |
| Schedules: fixed / RMD / Roth conversion | `test_scenario7.py` |
| Brokerage + dividend policy, order independence | `test_scenario8.py` |
| Tax-deferred growth (IRA/Roth) | `test_scenario8_5.py` |
| Quarterly estimated tax payments | `test_scenario9.py` |
| State tax and SALT deduction | `test_scenario9_5.py` |
| HELOC accounts: draws, interest, payment modes, maturity | `test_scenario10.py` |
| Death, survivorship, estate valuation | `test_scenario11.py` |
| Tax law changes over time | `test_scenario12.py` |
| Inflation (nominal vs. real) | `test_scenario13.py` |
| Report-adjacent output: balances, schedule/cash-manager report text | `test_scenario14.py` |
| Recurring expenses | `test_scenario15.py` |
| The `reports` block / detail mode | `test_scenario16.py` |
| Multiple `cash_management` blocks, waterfall cycle detection | `test_scenario17.py` |
| Scenario inheritance: `extends`, override-by-name, `_remove`, mixins | `test_scenarios.py` |
| Ledger primitives: transactions, balance invariants, money rounding | `test_primitives.py` |
