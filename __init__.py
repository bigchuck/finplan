"""finplan — a forward-looking retirement financial projection engine."""

from .primitives import Posting, Transaction, UnbalancedTransaction, money
from .engine import Engine, LedgerLeak
from .simobject import SimObject
from .accounts import (
    Account, AssetAccount, LiabilityAccount, IncomeAccount,
    BrokerageAccount, TraditionalIRAAccount, RothAccount, HELOCAccount,
    Withdrawal, rate, check_unrealized, unrealized_gap, UnrealizedInvariantError,
    TaxDeferredGrowth, UNREALIZED_DEFERRED,
)
from .generators import (
    Generator, Policy, InterestPolicy, DividendPolicy,
    HELOCInterestPolicy, HELOCPayment, Schedule,
    ScheduleEvent, schedule_report, Shock, Stream, RecurringExpense,
)
from .cashmanager import CashManager, Source, FundingEvent
from .legacy import Death, Estate, BequestReport
from .taxengine import (
    TaxEngine, TaxLawChange, TaxYearResult, SettlementEvent, EstimateEvent,
    ORDINARY, PREFERENTIAL, SS, EXEMPT,
    DEFAULT_SAFE_HARBOR, DEFAULT_SALT_CAP, DEDUCTIBLE_INTEREST,
    ESTIMATE_MONTHS, ESTIMATE_JAN_QUARTER,
    DEFAULT_GATE_CHARACTER, DEFAULT_GATE_PREFIXES,
)
from .simulation import Simulation, Period
from .scenarios import resolve, deep_merge, names, ScenarioError
from .control import build, build_scenario, load

__all__ = [
    "Posting", "Transaction", "UnbalancedTransaction", "money",
    "Engine", "LedgerLeak",
    "SimObject",
    "Account", "AssetAccount", "LiabilityAccount", "IncomeAccount",
    "BrokerageAccount", "TraditionalIRAAccount", "RothAccount",
    "HELOCAccount", "Withdrawal",
    "rate", "check_unrealized", "unrealized_gap", "UnrealizedInvariantError",
    "TaxDeferredGrowth", "UNREALIZED_DEFERRED",
    "Generator", "Policy", "InterestPolicy", "DividendPolicy",
    "HELOCInterestPolicy", "HELOCPayment", "Schedule",
    "ScheduleEvent", "schedule_report", "Shock", "Stream", "RecurringExpense",
    "CashManager", "Source", "FundingEvent",
    "Death", "Estate", "BequestReport",
    "TaxEngine", "TaxLawChange", "TaxYearResult", "SettlementEvent", "EstimateEvent",
    "ORDINARY", "PREFERENTIAL", "SS", "EXEMPT",
    "DEFAULT_SAFE_HARBOR", "DEFAULT_SALT_CAP", "DEDUCTIBLE_INTEREST",
    "ESTIMATE_MONTHS", "ESTIMATE_JAN_QUARTER",
    "DEFAULT_GATE_CHARACTER", "DEFAULT_GATE_PREFIXES",
    "Simulation", "Period",
    "resolve", "deep_merge", "names", "ScenarioError",
    "build", "build_scenario", "load",
]