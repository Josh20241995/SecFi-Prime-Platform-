"""
Canonical enumerations.

These are the controlled vocabularies for the platform. Any new value must
go through reference-data change control (see docs/governance.md) because
these enums are referenced by SQL check constraints, risk limit configs,
and the explainability layer's rationale templates.
"""

from enum import Enum


class Direction(str, Enum):
    """Sign convention for a securities finance position."""
    LEND = "LEND"          # desk is lending securities out (on-loan, long financing)
    BORROW = "BORROW"      # desk is borrowing securities in (on-borrow, short financing)
    REPO = "REPO"           # desk is repo-ing out securities (cash borrow, collateral out)
    REVERSE_REPO = "REVERSE_REPO"  # desk is reverse-repoing in securities (cash lend, collateral in)


class ProductType(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    ADR = "ADR"
    CORPORATE_BOND = "CORPORATE_BOND"
    GOVT_BOND = "GOVT_BOND"
    GC_REPO = "GC_REPO"
    SPECIALS_REPO = "SPECIALS_REPO"
    SBL = "SBL"                      # securities-borrowed/loaned, non-repo SFT form


class RateType(str, Enum):
    FEE = "FEE"                     # securities lending fee, bps on market value, for HTB/specials
    REBATE = "REBATE"               # cash-collateral rebate rate for GC names
    REPO_RATE = "REPO_RATE"         # repo interest rate


class SpecialnessTier(str, Enum):
    GC = "GC"                                  # general collateral, abundant supply
    WARM = "WARM"                              # tightening, fee creeping up
    SPECIALS_IN_WAITING = "SPECIALS_IN_WAITING"  # utilization/fee trend signals imminent specialness
    SPECIAL = "SPECIAL"                        # confirmed special, fee well above GC
    HTB = "HTB"                                # hard-to-borrow, persistent scarcity
    DEEP_SPECIAL = "DEEP_SPECIAL"              # extreme scarcity, fee > configurable threshold


class CollateralType(str, Enum):
    CASH = "CASH"
    GOVT_SECURITIES = "GOVT_SECURITIES"
    AGENCY_SECURITIES = "AGENCY_SECURITIES"
    EQUITIES = "EQUITIES"
    LETTER_OF_CREDIT = "LETTER_OF_CREDIT"
    NON_CASH_OTHER = "NON_CASH_OTHER"


class LegalEntity(str, Enum):
    """
    Booking entity. Real deployments map this to the firm's actual legal
    entity master (LEI-keyed). Kept abstract here; see configs/base.yaml
    for the assumed entity set used in this reference build.
    """
    US_BROKER_DEALER = "US_BROKER_DEALER"
    UK_BROKER_DEALER = "UK_BROKER_DEALER"
    EU_BANK_ENTITY = "EU_BANK_ENTITY"
    APAC_BROKER_DEALER = "APAC_BROKER_DEALER"


class Region(str, Enum):
    AMERICAS = "AMERICAS"
    EMEA = "EMEA"
    APAC = "APAC"


class CounterpartyType(str, Enum):
    HEDGE_FUND = "HEDGE_FUND"
    ASSET_MANAGER = "ASSET_MANAGER"
    BANK_DEALER = "BANK_DEALER"
    PENSION_INSURANCE = "PENSION_INSURANCE"
    CCP = "CCP"
    SOVEREIGN_SUPRANATIONAL = "SOVEREIGN_SUPRANATIONAL"
    OTHER = "OTHER"


class CounterpartyTier(str, Enum):
    """Internal credit tiering — drives default limit templates and RW assumptions fallback."""
    TIER_1_PRIME = "TIER_1_PRIME"
    TIER_2_STANDARD = "TIER_2_STANDARD"
    TIER_3_WATCH = "TIER_3_WATCH"
    TIER_4_RESTRICTED = "TIER_4_RESTRICTED"


class RecommendationAction(str, Enum):
    GROW = "GROW"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    REPRICE = "REPRICE"
    REROUTE = "REROUTE"
    HEDGE = "HEDGE"
    SUBSTITUTE = "SUBSTITUTE"
    RETURN = "RETURN"
    RECALL = "RECALL"
    ROLL = "ROLL"
    UNWIND = "UNWIND"
    PAIR_OFF = "PAIR_OFF"
    DO_NOTHING = "DO_NOTHING"


class BreakType(str, Enum):
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_RATE_MISMATCH = "PRICE_RATE_MISMATCH"
    MISSING_AT_CUSTODIAN = "MISSING_AT_CUSTODIAN"
    MISSING_ON_BOOK = "MISSING_ON_BOOK"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    STALE_POSITION = "STALE_POSITION"
    SETTLEMENT_TIMING = "SETTLEMENT_TIMING"
    COLLATERAL_MISMATCH = "COLLATERAL_MISMATCH"
    CORPORATE_ACTION_UNADJUSTED = "CORPORATE_ACTION_UNADJUSTED"


class BreakSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"   # plausible path to buy-in, recall failure, or capital misstatement


class CorporateActionType(str, Enum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    MERGER = "MERGER"
    TENDER_OFFER = "TENDER_OFFER"
    SPIN_OFF = "SPIN_OFF"
    REDEMPTION = "REDEMPTION"
    BOND_CALL = "BOND_CALL"
    COUPON_PAYMENT = "COUPON_PAYMENT"
    UST_AUCTION_SETTLEMENT = "UST_AUCTION_SETTLEMENT"
    ADR_RATIO_CHANGE = "ADR_RATIO_CHANGE"
    INDEX_REBALANCE = "INDEX_REBALANCE"
    VOLUNTARY_ELECTION = "VOLUNTARY_ELECTION"
    MANDATORY_OTHER = "MANDATORY_OTHER"


class ActionUrgency(str, Enum):
    INFORMATIONAL = "INFORMATIONAL"
    MONITOR = "MONITOR"
    ACT_THIS_WEEK = "ACT_THIS_WEEK"
    ACT_TODAY = "ACT_TODAY"
    IMMEDIATE = "IMMEDIATE"


class DataQualityFlag(str, Enum):
    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    SOURCE_DISAGREEMENT = "SOURCE_DISAGREEMENT"
    FALLBACK_APPLIED = "FALLBACK_APPLIED"


class ApprovalStatus(str, Enum):
    """All recommendations are signal/advisory only until a human approves. See docs/governance.md."""
    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
