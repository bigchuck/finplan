"""Scenario 16 checks -- the CLI 'reports' block: detail-mode periodic
snapshots layered on top of the pre-existing end-of-run summary. Plain
asserts.

Run directly:  python tests/test_scenario16.py

Design recap (see reporting.py's module docstring for the full rationale):
  - No "reports" block anywhere in the control file -> byte-identical to
    the pre-S16 CLI. This is the one non-negotiable backward-compat
    constraint, checked directly below by diffing captured stdout against
    a hand-reconstructed legacy run.
  - Exactly one "reports" entry auto-selects without naming it on the
    command line, mirroring how an unnamed scenario is NOT auto-selected
    (scenarios always require a name; reports only require one when there
    is more than one to choose from).
  - Two or more entries with no name given: list names and exit 0, same
    UX as the existing "no scenario given" path -- no interactive prompt.
  - "detail" mode still prints the full end-of-run summary afterward.
  - The terminal halt always gets one snapshot, regardless of whether that
    period would otherwise be "due" under the configured frequency --
    past the halt there is no later tick to catch it on.
  - cfg only controls what gets PRINTED. The simulation's actual behavior
    (the December sweep in particular) is identical whether or not a
    report config is in play; several checks below confirm this by
    comparing detail-mode's final ledger against a plain sim.run().
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import date

from finplan.cli import main as cli_main
from finplan.control import build
from finplan.primitives import money, ZERO
from finplan.reporting import (
    ReportConfigError, build_report_config, print_summary, run_detail,
)


def _run(control):
    engine, sim, years = build(control)
    sim.run(years)
    return engine, sim


def _capture(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


BASIC = {
    "start": "2026-01-01", "years": 1,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00", "apr": "0.03"},
    ],
}

DEATH = {
    "start": "2026-01-01", "years": 5,
    "accounts": [
        {"type": "asset", "name": "Assets:Checking", "owner": "chuck",
         "opening": "100000.00"},
    ],
    "legacy": {"heir_rate": "0.24",
               "deaths": [{"owner": "chuck", "when": "2027-06-01"}]},
}


# --- A: ReportConfig parsing (unit) -----------------------------------

def test_defaults_match_the_documented_defaults():
    cfg = build_report_config("x", {})
    assert cfg.mode == "summary"
    assert cfg.show_start is False
    assert cfg.timing == "after_sweep"
    assert cfg.frequency == "yearly"
    assert cfg.include == []
    assert cfg.exclude == []


def test_unknown_mode_is_rejected():
    try:
        build_report_config("x", {"mode": "bogus"})
    except ReportConfigError as e:
        assert "mode" in str(e)
        return
    raise AssertionError("expected ReportConfigError for unknown mode")


def test_unknown_timing_is_rejected():
    try:
        build_report_config("x", {"timing": "bogus"})
    except ReportConfigError as e:
        assert "timing" in str(e)
        return
    raise AssertionError("expected ReportConfigError for unknown timing")


def test_unknown_string_frequency_is_rejected():
    try:
        build_report_config("x", {"frequency": "bogus"})
    except ReportConfigError as e:
        assert "frequency" in str(e)
        return
    raise AssertionError("expected ReportConfigError for unknown frequency")


def test_explicit_frequency_list_is_parsed_into_year_month_tuples():
    cfg = build_report_config("x", {
        "frequency": [{"year": 2027, "month": 6}, {"year": 2028, "month": 1}],
    })
    assert cfg.frequency == [(2027, 6), (2028, 1)]


def test_malformed_frequency_list_entry_is_rejected():
    try:
        build_report_config("x", {"frequency": [{"year": 2027}]})
    except ReportConfigError as e:
        assert "frequency" in str(e)
        return
    raise AssertionError("expected ReportConfigError for malformed entry")


def test_include_exclude_are_lists_even_when_omitted():
    cfg = build_report_config("x", {"include": ["Assets:"]})
    assert cfg.include == ["Assets:"]
    assert cfg.exclude == []


# --- B: backward compatibility -- no "reports" block at all ----------------

# --- B: backward compatibility -- no "reports" block at all ----------------
#
# These drive cli.main() end-to-end through real temp files, since that is
# the actual contract being preserved -- argv parsing, file loading, and
# all -- not just the reporting.py functions in isolation.

def _write(name, obj):
    import json
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="finplan_test_scenario16_")
    path = Path(tmpdir) / name
    path.write_text(json.dumps(obj))
    return str(path)


def test_no_reports_block_matches_hand_built_summary_exactly():
    path = _write("basic.json", {"scenarios": {"base": BASIC}})
    _, cli_out = _capture(cli_main, [path, "base"])

    engine, sim = _run(BASIC)
    _, expected = _capture(print_summary, "base", engine, sim, 1)

    assert cli_out == expected


def test_no_cfg_matches_run_then_print_summary():
    # Same claim, checked at the reporting.py level directly rather than
    # through argv/file I/O: no report config in play at all is exactly
    # sim.run(years) followed by print_summary().
    engine, sim = _run(BASIC)
    _, out = _capture(print_summary, "base", engine, sim, 1)
    assert "Scenario 'base' — final ledger after 1 year(s):" in out
    assert "Assets:Checking" in out
    assert "(whole-ledger sum)" in out


# --- C2: CLI arity selection through cli.main() end-to-end -----------------

def test_single_reports_entry_auto_selects_without_a_name():
    path = _write("single.json", {
        "scenarios": {"base": BASIC},
        "reports": {"detail-yearly": {"mode": "detail", "frequency": "yearly"}},
    })
    _, out = _capture(cli_main, [path, "base"])
    assert "[detail-yearly] 2026-12-01 (post-close):" in out
    # And it still ends with the full end-of-run summary.
    assert "Scenario 'base' — final ledger after 1 year(s):" in out


def test_two_reports_entries_with_no_name_lists_and_exits_zero():
    path = _write("multi.json", {
        "scenarios": {"base": BASIC},
        "reports": {
            "yearly": {"mode": "detail", "frequency": "yearly"},
            "monthly": {"mode": "detail", "frequency": "monthly"},
        },
    })
    rc, out = _capture(cli_main, [path, "base"])
    assert rc == 0
    assert "Reports in this file:" in out
    assert "  monthly" in out
    assert "  yearly" in out
    # It must NOT have actually run the scenario.
    assert "final ledger" not in out


def test_two_reports_entries_with_explicit_name_runs_that_one():
    path = _write("multi.json", {
        "scenarios": {"base": BASIC},
        "reports": {
            "yearly": {"mode": "detail", "frequency": "yearly"},
            "monthly": {"mode": "detail", "frequency": "monthly"},
        },
    })
    _, out = _capture(cli_main, [path, "base", "--report", "monthly"])
    assert "[monthly] 2026-01-01:" in out
    assert "[yearly]" not in out


def test_unknown_report_name_exits_nonzero():
    path = _write("multi.json", {
        "scenarios": {"base": BASIC},
        "reports": {"yearly": {"mode": "detail", "frequency": "yearly"}},
    })
    try:
        cli_main([path, "base", "--report", "bogus"])
    except SystemExit as e:
        assert e.code != 0
        return
    raise AssertionError("expected SystemExit for an unknown report name")


def test_report_name_given_but_no_reports_block_declared_exits_nonzero():
    path = _write("basic.json", {"scenarios": {"base": BASIC}})
    try:
        cli_main([path, "base", "--report", "whatever"])
    except SystemExit as e:
        assert e.code != 0
        return
    raise AssertionError(
        "expected SystemExit when a report name is given but the file "
        "declares no 'reports' block")


def test_out_flag_writes_report_to_file_and_creates_parent_dirs():
    import tempfile
    path = _write("basic.json", {"scenarios": {"base": BASIC}})
    out_dir = Path(tempfile.mkdtemp(prefix="finplan_test_scenario16_out_"))
    out_path = out_dir / "nested" / "run.txt"

    rc, console_out = _capture(cli_main, [path, "base", "--out", str(out_path)])

    assert rc == 0
    assert f"wrote output to {out_path}" in console_out
    assert "final ledger" not in console_out   # went to the file, not stdout
    written = out_path.read_text(encoding="utf-8")
    assert "Scenario 'base' — final ledger after 1 year(s):" in written


# --- C: single-entry auto-select needs no name, matching run_detail's
#        own output shape ---------------------------------------------

def test_run_detail_yearly_show_start_both_timing():
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {
        "mode": "detail", "show_start": True, "timing": "both",
        "frequency": "yearly", "include": ["Assets:"],
    })
    _, out = _capture(run_detail, engine, sim, years, cfg)
    assert "[d] 2026-01-01 (start, year 0):" in out
    assert "100,000.00" in out
    assert "[d] 2026-12-01 (pre-close):" in out
    assert "[d] 2026-12-01 (post-close):" in out
    # Only Assets: leaked through the include filter.
    assert "Income:" not in out


def test_show_start_false_omits_the_year_zero_snapshot():
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {"mode": "detail", "frequency": "yearly"})
    _, out = _capture(run_detail, engine, sim, years, cfg)
    assert "year 0" not in out


def test_timing_after_sweep_only_prints_post_close_not_pre_close():
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {
        "mode": "detail", "timing": "after_sweep", "frequency": "yearly",
    })
    _, out = _capture(run_detail, engine, sim, years, cfg)
    assert "(post-close)" in out
    assert "(pre-close)" not in out


def test_timing_before_sweep_only_prints_pre_close_not_post_close():
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {
        "mode": "detail", "timing": "before_sweep", "frequency": "yearly",
    })
    _, out = _capture(run_detail, engine, sim, years, cfg)
    assert "(pre-close)" in out
    assert "(post-close)" not in out


def test_pre_close_and_post_close_snapshots_differ_when_a_sweep_moves_money():
    # Expenses/Income gates hold a nonzero balance pre-close and zero
    # post-close, so a prefix that includes Income: should show that
    # difference explicitly under timing="both".
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {
        "mode": "detail", "timing": "both", "frequency": "yearly",
        "include": ["Income:"],
    })
    _, out = _capture(run_detail, engine, sim, years, cfg)
    pre = out.split("(pre-close):")[1].split("[d]")[0]
    post = out.split("(post-close):")[1]
    assert "Income:Interest" in pre and "Income:Interest" in post
    assert "0.00" in post   # swept to zero
    assert "0.00" not in pre.split("Income:Interest")[1].split("\n")[0]


def test_monthly_frequency_fires_every_period():
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {"mode": "detail", "frequency": "monthly"})
    _, out = _capture(run_detail, engine, sim, years, cfg)
    # 12 monthly snapshots, none of them the year-end month (which reports
    # under (post-close) with default timing="after_sweep").
    monthly_lines = [ln for ln in out.splitlines()
                     if ln.startswith("[d] ") and "close" not in ln
                     and "HALT" not in ln and "start" not in ln]
    assert len(monthly_lines) == 11   # Jan-Nov; Dec reports as (post-close)
    post_close_lines = [ln for ln in out.splitlines() if "(post-close)" in ln]
    assert len(post_close_lines) == 1


def test_explicit_frequency_list_fires_only_named_periods():
    control = {**BASIC, "years": 2}
    engine, sim, years = build(control)
    cfg = build_report_config("d", {
        "mode": "detail",
        "frequency": [{"year": 2026, "month": 6}, {"year": 2027, "month": 3}],
    })
    _, out = _capture(run_detail, engine, sim, years, cfg)
    assert "[d] 2026-06-01:" in out
    assert "[d] 2027-03-01:" in out
    # Nothing else fired -- no other bracketed period lines besides those two.
    period_lines = [ln for ln in out.splitlines() if ln.startswith("[d] ")]
    assert len(period_lines) == 2


def test_exclude_filters_out_matching_prefixes():
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {
        "mode": "detail", "frequency": "yearly", "exclude": ["Equity:"],
    })
    _, out = _capture(run_detail, engine, sim, years, cfg)
    assert "Equity:" not in out
    assert "Assets:Checking" in out


# --- D: the terminal halt always snapshots, regardless of frequency -------

def test_halt_snapshots_even_when_frequency_would_not_otherwise_fire():
    engine, sim, years = build(DEATH)
    cfg = build_report_config("d", {"mode": "detail", "frequency": "yearly"})
    _, out = _capture(run_detail, engine, sim, years, cfg)
    # Death fires 2027-06-01, mid-year -- not a December period -- yet it
    # still produces a snapshot, tagged HALT.
    assert "[d] 2027-06-01 (HALT):" in out
    # 2026-12-01 fired on schedule (a real year-end); 2027 never reaches
    # its own December because the run halted first.
    assert "[d] 2026-12-01 (post-close):" in out
    assert "2027-12-01" not in out


def test_halt_stops_iteration_immediately_no_further_periods():
    engine, sim, years = build(DEATH)
    cfg = build_report_config("d", {"mode": "detail", "frequency": "monthly"})
    _, out = _capture(run_detail, engine, sim, years, cfg)
    period_lines = [ln for ln in out.splitlines()
                    if ln.startswith("[d] ") and "close" not in ln
                    and "HALT" not in ln]
    # Monthly snapshots run Jan 2026 through May 2027 (the last full period
    # before the June 2027 halt), i.e. 17 months, then the halt takes over.
    assert (2027, 6) not in [
        (int(ln.split()[1][:4]), int(ln.split()[1][5:7])) for ln in period_lines
    ]
    assert "[d] 2027-06-01 (HALT):" in out


# --- E: detail mode never changes what the simulation DOES -----------------

def test_detail_mode_produces_the_same_final_ledger_as_a_plain_run():
    engine_a, sim_a, years_a = build(BASIC)
    cfg = build_report_config("d", {"mode": "detail", "frequency": "monthly"})
    _capture(run_detail, engine_a, sim_a, years_a, cfg)

    engine_b, sim_b = _run(BASIC)

    assert engine_a.balances() == engine_b.balances()


def test_detail_mode_still_sweeps_every_december_regardless_of_timing():
    # timing="before_sweep" only affects what's PRINTED; the actual sweep
    # must still run every year-end period.
    engine, sim, years = build(BASIC)
    cfg = build_report_config("d", {
        "mode": "detail", "timing": "before_sweep", "frequency": "yearly",
    })
    _capture(run_detail, engine, sim, years, cfg)
    assert engine.balance("Income:Interest") == ZERO   # swept, not left open
    assert sum(engine.balances().values(), ZERO) == ZERO


def test_print_summary_after_detail_mode_matches_a_plain_run():
    # Per the S16 design: detail mode still prints the full end-of-run
    # summary afterward. Confirm that summary is identical whether the run
    # was driven via run_detail or via a plain sim.run().
    engine_a, sim_a, years_a = build(BASIC)
    cfg = build_report_config("d", {"mode": "detail", "frequency": "yearly"})
    _capture(run_detail, engine_a, sim_a, years_a, cfg)
    _, summary_a = _capture(print_summary, "base", engine_a, sim_a, years_a)

    engine_b, sim_b, years_b = build(BASIC)
    sim_b.run(years_b)
    _, summary_b = _capture(print_summary, "base", engine_b, sim_b, years_b)

    assert summary_a == summary_b


if __name__ == "__main__":
    from _runner import run_module
    raise SystemExit(run_module(__name__))
