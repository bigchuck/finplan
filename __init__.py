"""finplan — a forward-looking retirement financial projection engine.

Not an accounting system: it simulates a forward plan. Double-entry is used
as an invariant harness (every transaction sums to zero; the whole ledger
sums to zero), not as bookkeeping ceremony.
"""

from .primitives import Posting, Transaction, UnbalancedTransaction, money
from .engine import Engine, LedgerLeak
from .simobject import SimObject
from .accounts import Account, AssetAccount, LiabilityAccount, IncomeAccount
from .generators import Generator, InterestPolicy, Stream
from .simulation import Simulation, Period
from .scenarios import resolve, deep_merge, names, ScenarioError
from .control import build, build_scenario, load

__all__ = [
    "Posting", "Transaction", "UnbalancedTransaction", "money",
    "Engine", "LedgerLeak",
    "SimObject",
    "Account", "AssetAccount", "LiabilityAccount", "IncomeAccount",
    "Generator", "InterestPolicy", "Stream",
    "Simulation", "Period",
    "resolve", "deep_merge", "names", "ScenarioError",
    "build", "build_scenario", "load",
]