"""Minimal CLI: run one scenario from an all_scenarios.json file."""

from __future__ import annotations

import argparse
import contextlib
import json
import os

from .control import build_scenario
from .reporting import (
    ReportConfigError, build_report_config, print_summary, run_detail,
)
from .scenarios import mixin_names, names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finplan")
    parser.add_argument("control", help="path to all_scenarios.json")
    parser.add_argument("scenario", nargs="?",
                        help="scenario name to run (omit to list them)")
    parser.add_argument("--report", metavar="NAME",
                        help="report name to use, from this file's "
                             "top-level 'reports' block (omit if that "
                             "block is absent or has exactly one entry)")
    parser.add_argument("--mixin", action="append", default=[],
                        dest="mixins", metavar="NAME",
                        help="mixin fragment, from this file's top-level "
                             "'mixins' block, to layer onto the scenario; "
                             "repeatable, applied in the order given")
    parser.add_argument("--out", metavar="PATH",
                        help="write output to this file, relative to the "
                             "current directory (e.g. output/run.txt), "
                             "instead of stdout; parent directories are "
                             "created if needed")
    args = parser.parse_args(argv)

    with open(args.control) as fh:
        root = json.load(fh)

    def run() -> int:
        if not args.scenario:
            print("Scenarios in this file:")
            for n in names(root):
                parent = root["scenarios"][n].get("extends")
                print(f"  {n}" + (f"  (extends {parent})" if parent else ""))
            available_mixins = mixin_names(root)
            if available_mixins:
                print("Mixins in this file (layer onto a scenario with --mixin):")
                for n in available_mixins:
                    print(f"  {n}")
            report_map = root.get("reports", {})
            if report_map:
                print("Reports in this file (select one with --report):")
                for n in sorted(report_map):
                    print(f"  {n}")
            return 0

        # A file with no "reports" block behaves exactly as before: cfg
        # stays None and the run goes straight to sim.run() + print_summary(),
        # byte-identical to pre-S16 output. A "reports" block with exactly
        # one entry auto-selects it, same convenience as an unnamed single
        # scenario would get if scenarios worked that way. Two or more
        # entries with no name given lists them and exits, mirroring the
        # no-scenario-given path above rather than prompting interactively.
        report_map = root.get("reports", {})
        cfg = None
        if args.report and not report_map:
            parser.error(f"{args.control!r} declares no 'reports' block, so "
                         f"there is no report named {args.report!r} to use")
        if report_map:
            try:
                if args.report:
                    if args.report not in report_map:
                        parser.error(
                            f"unknown report {args.report!r}; available: "
                            f"{sorted(report_map)}")
                    cfg = build_report_config(args.report, report_map[args.report])
                elif len(report_map) == 1:
                    only_name = next(iter(report_map))
                    cfg = build_report_config(only_name, report_map[only_name])
                else:
                    print("Reports in this file:")
                    for n in sorted(report_map):
                        print(f"  {n}")
                    return 0
            except ReportConfigError as e:
                parser.error(str(e))

        engine, sim, years = build_scenario(root, args.scenario, tuple(args.mixins))

        if cfg is not None and cfg.mode == "detail":
            run_detail(engine, sim, years, cfg)
        else:
            sim.run(years)

        label = "+".join([args.scenario, *args.mixins])
        print_summary(label, engine, sim, years)
        return 0

    with contextlib.ExitStack() as stack:
        if args.out:
            out_dir = os.path.dirname(args.out)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            out_fh = stack.enter_context(open(args.out, "w", encoding="utf-8"))
            stack.enter_context(contextlib.redirect_stdout(out_fh))
        rc = run()

    if args.out:
        print(f"wrote output to {args.out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())