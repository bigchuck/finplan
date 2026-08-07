"""CLI report configuration and the detail-mode period loop.

An optional top-level "reports" block in the control file lets a scenario be
run either as the pre-existing end-of-run SUMMARY (final ledger + whatever
reports apply -- the only thing cli.py has ever done), or as a DETAIL run
that also prints periodic ledger snapshots as the simulation ticks, on top
of that same summary at the end.

Kept out of cli.py on purpose: cli.py owns argument parsing and report-name
selection (arity: zero/one/many "reports" entries), this module owns what a
snapshot looks like and when it fires. Selection logic stays in cli.py
because "list names and exit" is a control-flow decision the caller makes,
not something this module should short-circuit on.

BACKWARD COMPATIBILITY: a control file with no "reports" block, or where the
caller passes no report name against a single declared entry whose mode is
"summary" (the default), produces output IDENTICAL to the pre-S16 cli.py --
print_summary() below is that exact original block, moved verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import Engine
from .generators import Schedule, schedule_report
from .simulation import Simulation, _months

DEFAULT_MODE = "summary"
DEFAULT_TIMING = "after_sweep"
DEFAULT_FREQUENCY = "yearly"
_MODES = ("summary", "detail")
_TIMINGS = ("before_sweep", "after_sweep", "both")
_FREQUENCIES = ("monthly", "yearly")


class ReportConfigError(ValueError):
    """Bad 'reports' entry: unknown mode/timing/frequency, or a malformed
    explicit frequency list."""


@dataclass
class ReportConfig:
    name: str
    mode: str = DEFAULT_MODE
    show_start: bool = False
    timing: str = DEFAULT_TIMING
    frequency: object = DEFAULT_FREQUENCY   # "monthly" | "yearly" | [(y, m), ...]
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    transactions: list = field(default_factory=list)

    def __post_init__(self):
        if self.mode not in _MODES:
            raise ReportConfigError(
                f"report {self.name!r}: unknown mode {self.mode!r}; "
                f"expected one of {_MODES}")
        if self.timing not in _TIMINGS:
            raise ReportConfigError(
                f"report {self.name!r}: unknown timing {self.timing!r}; "
                f"expected one of {_TIMINGS}")
        if isinstance(self.frequency, str) and self.frequency not in _FREQUENCIES:
            raise ReportConfigError(
                f"report {self.name!r}: unknown frequency {self.frequency!r}; "
                f"expected 'monthly', 'yearly', or a list of "
                f"{{'year':, 'month':}} entries")


def build_report_config(name: str, spec: dict) -> ReportConfig:
    freq = spec.get("frequency", DEFAULT_FREQUENCY)
    if isinstance(freq, list):
        try:
            freq = [(int(item["year"]), int(item["month"])) for item in freq]
        except (KeyError, TypeError, ValueError) as e:
            raise ReportConfigError(
                f"report {name!r}: 'frequency' list entries need integer "
                f"'year' and 'month', got {freq!r}") from e
    return ReportConfig(
        name=name,
        mode=spec.get("mode", DEFAULT_MODE),
        show_start=bool(spec.get("show_start", False)),
        timing=spec.get("timing", DEFAULT_TIMING),
        frequency=freq,
        include=list(spec.get("include", [])),
        exclude=list(spec.get("exclude", [])),
        transactions=list(spec.get("transactions", [])),
    )


# --- snapshot filtering / formatting ----------------------------------

def _filtered_balances(engine: Engine, cfg: ReportConfig) -> dict:
    if cfg.include:
        merged = {}
        for prefix in cfg.include:
            merged.update(engine.balances(prefix))
    else:
        merged = engine.balances()
    if cfg.exclude:
        merged = {name: bal for name, bal in merged.items()
                 if not any(name.startswith(p) for p in cfg.exclude)}
    return merged


def _print_snapshot(label: str, engine: Engine, cfg: ReportConfig) -> None:
    balances = _filtered_balances(engine, cfg)
    print(label)
    width = max((len(a) for a in balances), default=0)
    for account in sorted(balances):
        print(f"  {account:<{width}}  {balances[account]:>14,.2f}")


def _print_transactions(label: str, engine: Engine, period,
                        cfg: ReportConfig) -> None:
    """Dump journal entries touching any declared prefix for this period.
    Independent of 'timing': the close-sweep only ever touches Income:/
    Expenses: accounts (see Simulation._close), so a declared prefix like
    'Assets:Checking' is unaffected by before/after-sweep positioning --
    there is exactly one meaningful point to print it, unlike balances.
    """
    seen = set()
    entries = []
    for prefix in cfg.transactions:
        for txn in engine.transactions_for(prefix, period.year, period.month):
            if id(txn) in seen:
                continue
            seen.add(id(txn))
            entries.append(txn)
    if not entries:
        return
    print(label)
    for txn in entries:
        print(f"  {txn}")


def _due(period, cfg: ReportConfig) -> bool:
    if isinstance(cfg.frequency, list):
        return (period.year, period.month) in cfg.frequency
    if cfg.frequency == "monthly":
        return True
    return period.is_year_end   # "yearly"


# --- the detail-mode loop -----------------------------------------------

def run_detail(engine: Engine, sim: Simulation, years: int,
               cfg: ReportConfig) -> None:
    """Drive the same phase sequence Simulation.run() uses, by hand, so a
    snapshot can be taken between _assess and _close on a scheduled
    year-end period (timing="before_sweep"/"both"), and so the terminal
    halt always gets one final snapshot regardless of 'frequency' -- past
    that period there is no later tick to catch it on.

    The sweep itself always still runs on every year-end period exactly as
    Simulation.run() would; 'cfg' only controls what gets PRINTED, never
    what the simulation DOES.
    """
    if cfg.show_start:
        _print_snapshot(f"[{cfg.name}] {sim.start} (start, year 0):",
                        engine, cfg)

    for period in _months(sim.start, years, sim.inflation):
        if sim._transition(period):
            _print_snapshot(f"[{cfg.name}] {period.date} (HALT):",
                            engine, cfg)
            break

        sim._accrue(period)
        sim._fund(period)
        sim._assess(period)

        due = _due(period, cfg)
        is_close = period.is_year_end

        if due and cfg.transactions:
            _print_transactions(f"[{cfg.name}] {period.date} (transactions):",
                                engine, period, cfg)

        if due and is_close and cfg.timing in ("before_sweep", "both"):
            _print_snapshot(f"[{cfg.name}] {period.date} (pre-close):",
                            engine, cfg)

        if is_close:
            sim._close(period)

        if due:
            if is_close:
                if cfg.timing in ("after_sweep", "both"):
                    _print_snapshot(
                        f"[{cfg.name}] {period.date} (post-close):",
                        engine, cfg)
            else:
                # Non-year-end months have exactly one meaningful snapshot
                # point (_close never runs there), so 'timing' has nothing
                # to distinguish outside a year-end period.
                _print_snapshot(f"[{cfg.name}] {period.date}:", engine, cfg)


# --- the pre-existing end-of-run summary, moved verbatim -----------------

def print_summary(scenario: str, engine: Engine, sim: Simulation,
                  years: int) -> None:
    """Exactly cli.py's original post-run block. 'detail' mode calls this
    too, after its periodic snapshots, per S16 design: detail runs still
    end with the full summary, they just show more along the way.
    """
    print(f"Scenario {scenario!r} — final ledger after {years} year(s):")
    width = max((len(a) for a in engine.accounts()), default=0)
    for account in engine.accounts():
        print(f"  {account:<{width}}  {engine.balance(account):>14,.2f}")
    print(f"  {'(whole-ledger sum)':<{width}}  "
          f"{sum(engine.balances().values()):>14,.2f}")

    if sim.cash_manager is not None:
        print()
        print(sim.cash_manager.report())
    if sim.tax_engine is not None:
        print()
        print(sim.tax_engine.report())

    if sim.estate is not None:
        print()
        print(sim.estate.report())

    schedules = [o for o in sim.objects if isinstance(o, Schedule)]
    if schedules:
        print()
        print(schedule_report(schedules))
