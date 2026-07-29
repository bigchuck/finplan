"""Run all checks without pytest:  python run_tests.py

Discovers every tests/test_*.py, runs their test_* functions, and exits
non-zero if any fail.
"""

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent          # …/src/finplan
SRC = REPO_ROOT.parent                               # …/src  (finplan importable)
TESTS = REPO_ROOT / "tests"

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(TESTS))                       # for _runner and test modules

from _runner import run_functions  # noqa: E402


def main() -> int:
    total_pass = total_fail = 0
    for path in sorted(TESTS.glob("test_*.py")):
        mod = importlib.import_module(path.stem)
        print(f"{path.stem}:")
        p, f = run_functions(vars(mod), label="")
        total_pass += p
        total_fail += f
    print("-" * 32)
    print(f"TOTAL: {total_pass} passed, {total_fail} failed")
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())