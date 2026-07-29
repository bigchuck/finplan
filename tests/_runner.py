"""Minimal test runner — no pytest. Finds and runs test_* functions.

Used both by each test file's __main__ (run one file) and by run_tests.py at
the repo root (run all files). A test "passes" if it returns without raising.
"""

import sys
import traceback


def run_functions(namespace: dict, label: str = "") -> tuple[int, int]:
    """Run every callable named test_* in ``namespace``. Returns (passed, failed)."""
    tests = sorted((n, f) for n, f in namespace.items()
                   if n.startswith("test_") and callable(f))
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"  FAIL  {label}{name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"  ok    {label}{name}")
    return passed, failed


def run_module(mod_name: str) -> int:
    """Run the tests in an already-imported module; return an exit code."""
    module = sys.modules[mod_name]
    passed, failed = run_functions(vars(module))
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0