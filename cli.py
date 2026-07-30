"""Minimal CLI: run one scenario from an all_scenarios.json file."""

from __future__ import annotations

import argparse
import json

from .control import build_scenario
from .scenarios import names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="finplan")
    parser.add_argument("control", help="path to all_scenarios.json")
    parser.add_argument("scenario", nargs="?",
                        help="scenario name to run (omit to list them)")
    args = parser.parse_args(argv)

    with open(args.control) as fh:
        root = json.load(fh)

    if not args.scenario:
        print("Scenarios in this file:")
        for n in names(root):
            parent = root["scenarios"][n].get("extends")
            print(f"  {n}" + (f"  (extends {parent})" if parent else ""))
        return 0

    engine, sim, years = build_scenario(root, args.scenario)
    sim.run(years)

    print(f"Scenario {args.scenario!r} — final ledger after {years} year(s):")
    width = max((len(a) for a in engine.accounts()), default=0)
    for account in engine.accounts():
        print(f"  {account:<{width}}  {engine.balance(account):>14,.2f}")
    print(f"  {'(whole-ledger sum)':<{width}}  "
          f"{sum(engine.balances().values()):>14,.2f}")

    if sim.cash_manager is not None:
        print()
        print(sim.cash_manager.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())