"""Pinned TY2024 federal tables for tests whose hand calculations use them.

The TaxEngine defaults track the current IRS year and will keep moving. The
hand-checked figures in scenarios 9, 9.5, 10, 11 and 12 were derived from the
TY2024 tables (MFJ std 29,200.00, 10% band to 23,200.00, SALT cap
10,000.00), so those tests declare these tables explicitly rather than lean
on whatever the defaults happen to be.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from finplan.taxengine import TaxEngine


TY2024_MFJ = {
    "brackets": [
        ["23200.00", "0.10"], ["94300.00", "0.12"], ["201050.00", "0.22"],
        ["383900.00", "0.24"], ["487450.00", "0.32"], ["731200.00", "0.35"],
        ["1000000000000.00", "0.37"],
    ],
    "ltcg_brackets": [
        ["94050.00", "0.00"], ["583750.00", "0.15"],
        ["1000000000000.00", "0.20"],
    ],
    "std_deduction": "29200.00",
}

# Goes under legacy.single, so a survivor's Single year is TY2024 too.
TY2024_SINGLE = {
    "brackets": [
        ["11600.00", "0.10"], ["47150.00", "0.12"], ["100525.00", "0.22"],
        ["191950.00", "0.24"], ["243725.00", "0.32"], ["609350.00", "0.35"],
        ["1000000000000.00", "0.37"],
    ],
    "ltcg_brackets": [
        ["47025.00", "0.00"], ["518900.00", "0.15"],
        ["1000000000000.00", "0.20"],
    ],
    "std_deduction": "14600.00",
}

TY2024_SALT_CAP = "10000.00"


def ty2024_tax(**fields) -> dict:
    """A control-file "tax" block on TY2024 tables, plus ``fields``.

    ``deductions`` is merged rather than replaced, so a caller supplying
    other_itemized still gets the TY2024 SALT cap unless it restates one.
    """
    deductions = {"salt_cap": TY2024_SALT_CAP}
    deductions.update(fields.pop("deductions", None) or {})
    return {**TY2024_MFJ, "cash_account": "Assets:Checking",
            "deductions": deductions, **fields}


def ty2024_engine(**kw) -> TaxEngine:
    """A TaxEngine on TY2024 tables; ``deductions`` merges as above."""
    deductions = {"salt_cap": TY2024_SALT_CAP}
    deductions.update(kw.pop("deductions", None) or {})
    return TaxEngine(brackets=TY2024_MFJ["brackets"],
                     ltcg_brackets=TY2024_MFJ["ltcg_brackets"],
                     std_deduction=TY2024_MFJ["std_deduction"],
                     deductions=deductions, **kw)
