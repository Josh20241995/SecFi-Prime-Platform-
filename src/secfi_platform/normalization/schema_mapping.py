"""
Normalization layer.

Converts raw rows (dicts, as produced by ingestion/connectors.py CSV/API
readers) into canonical, validated domain objects from common/types.py.
This is the ONLY place vendor-specific field names and formats should
ever appear — every analytics engine downstream depends on the canonical
shape, not the source format, so adding a new vendor (or a new custodian)
means writing one new `parse_*` function here, nothing else.

Validation policy: a record that fails a REQUIRED field check raises
DataQualityError and is excluded from the batch (with the error logged
and an Alert raised by the orchestration layer — see
orchestration/scheduler.py `_ingest_and_normalize`). A record that fails
an OPTIONAL/soft check (e.g., a stale timestamp) is included but stamped
with the relevant DataQualityFlag so downstream confidence scoring
(explainability/explain.py) discounts it appropriately rather than
silently treating it as full-quality data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from secfi_platform.common.enums import (
    CollateralType,
    CorporateActionType,
    CounterpartyTier,
    CounterpartyType,
    DataQualityFlag,
    Direction,
    LegalEntity,
    ProductType,
    Region,
)
from secfi_platform.common.types import (
    CollateralLeg,
    Counterparty,
    CorporateActionEvent,
    FXRate,
    MarketRateQuote,
    Position,
    Security,
)
from secfi_platform.ingestion.base import DataQualityError

STALE_AFTER_HOURS = 24


def _req(row: dict, field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise DataQualityError([f"required field '{field}' missing or empty"])
    return str(value).strip()


def _opt(row: dict, field: str) -> Optional[str]:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _parse_decimal(value: Optional[str], field: str, required: bool = True) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        if required:
            raise DataQualityError([f"required numeric field '{field}' missing or empty"])
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DataQualityError([f"field '{field}' value '{value}' is not a valid decimal"]) from exc


def _parse_date(value: Optional[str], field: str, required: bool = True) -> Optional[date]:
    if value is None or str(value).strip() == "":
        if required:
            raise DataQualityError([f"required date field '{field}' missing or empty"])
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise DataQualityError([f"field '{field}' value '{value}' is not a valid YYYY-MM-DD date"]) from exc


def _parse_datetime(value: Optional[str], field: str, required: bool = True) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        if required:
            raise DataQualityError([f"required datetime field '{field}' missing or empty"])
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise DataQualityError([f"field '{field}' value '{value}' is not a recognized datetime format"])


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _staleness_flag(as_of: datetime, reference_now: Optional[datetime] = None) -> DataQualityFlag:
    reference_now = reference_now or datetime.now(timezone.utc)
    age_hours = (reference_now - as_of).total_seconds() / 3600.0
    return DataQualityFlag.STALE if age_hours > STALE_AFTER_HOURS else DataQualityFlag.OK


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_security(row: dict) -> Security:
    return Security(
        internal_id=_req(row, "internal_id"),
        cusip=_opt(row, "cusip"),
        isin=_opt(row, "isin"),
        sedol=_opt(row, "sedol"),
        ticker=_req(row, "ticker"),
        description=_opt(row, "description") or "",
        product_type=ProductType(_req(row, "product_type")),
        currency=_req(row, "currency"),
        country_of_risk=_req(row, "country_of_risk"),
        issuer_id=_req(row, "issuer_id"),
        gics_sector=_opt(row, "gics_sector"),
        is_adr=_parse_bool(row.get("is_adr")),
        adr_ratio=_parse_decimal(row.get("adr_ratio"), "adr_ratio", required=False),
        index_memberships=tuple(filter(None, (_opt(row, "index_memberships") or "").split("|"))),
    )


def parse_counterparty(row: dict) -> Counterparty:
    booking_entities = tuple(
        LegalEntity(v) for v in filter(None, _req(row, "booking_entities").split("|"))
    )
    pd_1y = row.get("pd_1y")
    lgd = row.get("lgd_assumption")
    return Counterparty(
        counterparty_id=_req(row, "counterparty_id"),
        legal_name=_req(row, "legal_name"),
        counterparty_type=CounterpartyType(_req(row, "counterparty_type")),
        tier=CounterpartyTier(_req(row, "tier")),
        region=Region(_req(row, "region")),
        booking_entities=booking_entities,
        lei=_opt(row, "lei"),
        parent_group_id=_opt(row, "parent_group_id"),
        internal_credit_rating=_opt(row, "internal_credit_rating"),
        pd_1y=float(pd_1y) if pd_1y not in (None, "") else None,
        lgd_assumption=float(lgd) if lgd not in (None, "") else None,
        is_netting_eligible=_parse_bool(row.get("is_netting_eligible")),
        onboarding_date=_parse_date(row.get("onboarding_date"), "onboarding_date", required=False),
        watch_list=_parse_bool(row.get("watch_list")),
    )


def parse_position(row: dict, security_lookup: dict) -> Position:
    security_id = _req(row, "security_internal_id")
    security = security_lookup.get(security_id)
    if security is None:
        raise DataQualityError([f"position references unknown security_internal_id '{security_id}'"])

    collateral_leg = None
    collateral_type_raw = _opt(row, "collateral_type")
    if collateral_type_raw:
        collateral_leg = CollateralLeg(
            collateral_type=CollateralType(collateral_type_raw),
            market_value=_parse_decimal(row.get("collateral_market_value"), "collateral_market_value"),
            currency=_req(row, "collateral_currency"),
            haircut_pct=_parse_decimal(row.get("collateral_haircut_pct"), "collateral_haircut_pct"),
        )

    return Position(
        position_id=_req(row, "position_id"),
        trade_date=_parse_date(row.get("trade_date"), "trade_date"),
        value_date=_parse_date(row.get("value_date"), "value_date"),
        maturity_date=_parse_date(row.get("maturity_date"), "maturity_date", required=False),
        direction=Direction(_req(row, "direction")),
        security=security,
        counterparty_id=_req(row, "counterparty_id"),
        booking_entity=LegalEntity(_req(row, "booking_entity")),
        quantity=_parse_decimal(row.get("quantity"), "quantity"),
        market_value=_parse_decimal(row.get("market_value"), "market_value"),
        currency=_req(row, "currency"),
        rate_bps=_parse_decimal(row.get("rate_bps"), "rate_bps"),
        rate_type_is_rebate=_parse_bool(row.get("rate_type_is_rebate")),
        collateral=(collateral_leg,) if collateral_leg else (),
        term_type=_req(row, "term_type"),
        desk_id=_req(row, "desk_id"),
        trader_id=_req(row, "trader_id"),
        is_rehypothecable=_parse_bool(row.get("is_rehypothecable"), default=True),
        last_recall_date=_parse_date(row.get("last_recall_date"), "last_recall_date", required=False),
        last_return_date=_parse_date(row.get("last_return_date"), "last_return_date", required=False),
        source_system=_opt(row, "source_system") or "INTERNAL_TRADE_CAPTURE",
        data_quality_flag=DataQualityFlag.OK,
    )


def parse_market_rate_quote(row: dict, reference_now: Optional[datetime] = None) -> MarketRateQuote:
    as_of = _parse_datetime(row.get("as_of"), "as_of")
    flag = _staleness_flag(as_of, reference_now)
    return MarketRateQuote(
        security_internal_id=_req(row, "security_internal_id"),
        as_of=as_of,
        source=_req(row, "source"),
        avg_fee_bps=_parse_decimal(row.get("avg_fee_bps"), "avg_fee_bps", required=False),
        weighted_avg_fee_bps=_parse_decimal(row.get("weighted_avg_fee_bps"), "weighted_avg_fee_bps", required=False),
        min_fee_bps=_parse_decimal(row.get("min_fee_bps"), "min_fee_bps", required=False),
        max_fee_bps=_parse_decimal(row.get("max_fee_bps"), "max_fee_bps", required=False),
        utilization_pct=_parse_decimal(row.get("utilization_pct"), "utilization_pct", required=False),
        total_lendable_value=_parse_decimal(row.get("total_lendable_value"), "total_lendable_value", required=False),
        total_on_loan_value=_parse_decimal(row.get("total_on_loan_value"), "total_on_loan_value", required=False),
        sample_count=int(row["sample_count"]) if row.get("sample_count") else None,
        data_quality_flag=flag,
    )


def parse_corporate_action_event(row: dict) -> CorporateActionEvent:
    return CorporateActionEvent(
        event_id=_req(row, "event_id"),
        security_internal_id=_req(row, "security_internal_id"),
        action_type=CorporateActionType(_req(row, "action_type")),
        announce_date=_parse_date(row.get("announce_date"), "announce_date"),
        record_date=_parse_date(row.get("record_date"), "record_date", required=False),
        ex_date=_parse_date(row.get("ex_date"), "ex_date", required=False),
        effective_date=_parse_date(row.get("effective_date"), "effective_date", required=False),
        payment_date=_parse_date(row.get("payment_date"), "payment_date", required=False),
        is_mandatory=_parse_bool(row.get("is_mandatory")),
        election_deadline=_parse_date(row.get("election_deadline"), "election_deadline", required=False),
        terms_summary=_opt(row, "terms_summary") or "",
        source=_opt(row, "source") or "CORP_ACTIONS_FEED",
        data_quality_flag=DataQualityFlag.OK,
    )


def parse_fx_rate(row: dict) -> FXRate:
    return FXRate(
        base_ccy=_req(row, "base_ccy"),
        quote_ccy=_req(row, "quote_ccy"),
        rate=_parse_decimal(row.get("rate"), "rate"),
        as_of=_parse_datetime(row.get("as_of"), "as_of"),
        source=_opt(row, "source") or "INTERNAL_MARKET_DATA",
    )


def parse_rows(rows: list, parser, **kwargs) -> tuple:
    """
    Batch-parse helper used by orchestration. Returns (parsed_objects, errors),
    never raises — a single bad row must not take down an entire batch run.
    """
    parsed = []
    errors = []
    for i, row in enumerate(rows):
        try:
            parsed.append(parser(row, **kwargs))
        except DataQualityError as exc:
            errors.append({"row_index": i, "errors": exc.field_errors, "raw_row": row})
    return parsed, errors
