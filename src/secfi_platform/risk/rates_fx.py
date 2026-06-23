"""
Interest rate and FX risk engine for the financing book.

Securities finance books carry IR/FX risk primarily through:
  - cash collateral reinvestment vs. rebate rate mismatches (repo/GC book)
  - term mismatches between funding tenor and reinvestment tenor
  - non-USD-denominated positions / collateral / rebates creating FX exposure
  - cross-currency funding (e.g., USD securities borrowed against EUR cash collateral)

This module computes:
  - DV01 (dollar value of a 1bp rate move) by currency and tenor bucket
  - funding gap analysis (tenor mismatch between asset and funding legs)
  - FX net exposure by currency
  - cross-currency funding mismatch
  - hedge notional recommendations (IRS/FX forward) with carry trade-off

Assumption (stated explicitly): this module treats each Position's
maturity_date as the relevant rate-reset/funding tenor point, and treats
OPEN/evergreen positions as overnight (tenor bucket "O/N") for DV01
purposes, since open positions reprice at the desk's discretion daily.
A real deployment should connect actual behavioral tenor assumptions from
Treasury (e.g., evergreen books often carry an internally modeled
"sticky" effective tenor) — see docs/assumptions_and_limitations.md item R-7.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from secfi_platform.common.types import FXRate, Position

TENOR_BUCKETS = [
    ("O/N", 0, 1),
    ("1W", 2, 7),
    ("1M", 8, 30),
    ("3M", 31, 90),
    ("6M", 91, 180),
    ("1Y", 181, 365),
    ("1Y+", 366, 10_000),
]


def _bucket_for_days(days: int) -> str:
    for label, lo, hi in TENOR_BUCKETS:
        if lo <= days <= hi:
            return label
    return "1Y+"


def _tenor_bucket(pos: Position, as_of: date) -> str:
    if pos.maturity_date is None:
        return "O/N"
    days = (pos.maturity_date - as_of).days
    return _bucket_for_days(max(days, 0))


@dataclass
class DV01Bucket:
    currency: str
    tenor_bucket: str
    net_notional_usd: Decimal
    dv01_usd: Decimal           # P&L impact of a +1bp parallel move in that bucket's curve


@dataclass
class FXExposure:
    currency: str
    net_exposure_usd: Decimal          # converted to USD at current spot
    net_exposure_local: Decimal
    spot_rate_used: Optional[Decimal]
    funding_mismatch_usd: Decimal      # asset-leg vs funding-leg currency mismatch


@dataclass
class RatesAndFxRiskReport:
    as_of: str
    dv01_by_bucket: list                 # list[DV01Bucket]
    total_dv01_usd: Decimal
    fx_exposures: list                   # list[FXExposure]
    funding_gap_summary: dict            # tenor_bucket -> {assets, funding, gap}
    hedge_recommendations: list          # list[dict] explainable hedge suggestions


def _position_rate_sensitive_notional(pos: Position) -> Decimal:
    """
    Approximate rate-sensitive notional: for REPO/REVERSE_REPO this is the
    cash leg market value; for LEND/BORROW it is the cash-collateral
    portion of the position (rebate-rate-sensitive), which may be zero if
    collateral is entirely non-cash (then the position carries fee risk,
    not cash-reinvestment rate risk, and is excluded from DV01 here by
    design — fee risk is handled by pricing/pricing_intelligence.py).
    """
    from secfi_platform.common.enums import CollateralType, Direction

    if pos.direction in ("REPO", "REVERSE_REPO"):
        return pos.market_value
    cash_collateral = sum(
        (leg.market_value for leg in pos.collateral if leg.collateral_type == CollateralType.CASH),
        Decimal("0"),
    )
    return cash_collateral


def _rate_exposure_sign(pos: Position) -> Decimal:
    """
    Sign convention for rate sensitivity:
      +1 : desk is economically "long cash" (REVERSE_REPO, or LEND against
           cash collateral where the desk reinvests/earns the spread) —
           desk P&L rises when rates rise.
      -1 : desk is economically "short cash" (REPO, or BORROW against cash
           collateral posted out, owing a rebate) — desk P&L falls when
           rates rise.
    """
    direction_value = pos.direction.value if hasattr(pos.direction, "value") else str(pos.direction)
    if direction_value in ("REVERSE_REPO", "LEND"):
        return Decimal("1")
    return Decimal("-1")


def compute_dv01(positions: Iterable[Position], as_of: date) -> list[DV01Bucket]:
    agg: dict = defaultdict(lambda: Decimal("0"))
    for pos in positions:
        notional = _position_rate_sensitive_notional(pos)
        if notional == 0:
            continue
        bucket = _tenor_bucket(pos, as_of)
        sign = _rate_exposure_sign(pos)
        key = (pos.currency, bucket)
        agg[key] += sign * notional

    out = []
    for (ccy, bucket), net_notional in agg.items():
        dv01 = net_notional * Decimal("0.0001")  # 1bp
        out.append(DV01Bucket(currency=ccy, tenor_bucket=bucket, net_notional_usd=net_notional, dv01_usd=dv01))
    out.sort(key=lambda b: (b.currency, b.tenor_bucket))
    return out


def compute_fx_exposure(
    positions: Iterable[Position],
    fx_rates: dict,          # currency -> FXRate (quote vs USD)
    base_currency: str = "USD",
) -> list[FXExposure]:
    net_by_ccy: dict = defaultdict(lambda: Decimal("0"))
    for pos in positions:
        if pos.currency == base_currency:
            continue
        net_by_ccy[pos.currency] += pos.market_value

    results = []
    for ccy, local_net in net_by_ccy.items():
        rate_obj: Optional[FXRate] = fx_rates.get(ccy)
        spot = rate_obj.rate if rate_obj else None
        usd_equiv = local_net * spot if spot else Decimal("0")
        results.append(
            FXExposure(
                currency=ccy,
                net_exposure_usd=usd_equiv,
                net_exposure_local=local_net,
                spot_rate_used=spot,
                funding_mismatch_usd=usd_equiv,  # simplified: see module docstring assumption
            )
        )
    results.sort(key=lambda r: abs(r.net_exposure_usd), reverse=True)
    return results


def compute_funding_gap(positions: Iterable[Position], as_of: date) -> dict:
    """
    Buckets cash-leg 'assets' (desk owed cash back, e.g. reverse repo /
    lend-against-cash-collateral) against 'funding' (desk owes cash back,
    e.g. repo / borrow-against-cash-collateral) by tenor bucket, surfacing
    where the desk is funding long-tenor assets with short-tenor funding
    (roll risk) or vice versa.
    """
    gap: dict = defaultdict(lambda: {"assets_usd": Decimal("0"), "funding_usd": Decimal("0")})
    for pos in positions:
        notional = _position_rate_sensitive_notional(pos)
        if notional == 0:
            continue
        bucket = _tenor_bucket(pos, as_of)
        direction_value = pos.direction.value if hasattr(pos.direction, "value") else str(pos.direction)
        is_asset_side = direction_value in ("REVERSE_REPO", "LEND")
        if is_asset_side:
            gap[bucket]["assets_usd"] += notional
        else:
            gap[bucket]["funding_usd"] += notional

    summary = {}
    for bucket, vals in gap.items():
        net_gap = vals["assets_usd"] - vals["funding_usd"]
        summary[bucket] = {
            "assets_usd": vals["assets_usd"],
            "funding_usd": vals["funding_usd"],
            "net_gap_usd": net_gap,
        }
    return summary


def recommend_hedges(
    dv01_buckets: list,
    fx_exposures: list,
    dv01_materiality_usd: Decimal = Decimal("25000"),
    fx_materiality_usd: Decimal = Decimal("5_000_000"),
) -> list:
    recs = []
    for b in dv01_buckets:
        if abs(b.dv01_usd) >= dv01_materiality_usd:
            direction = "pay fixed / receive floating IRS" if b.dv01_usd > 0 else "receive fixed / pay floating IRS"
            recs.append(
                {
                    "type": "INTEREST_RATE_HEDGE",
                    "currency": b.currency,
                    "tenor_bucket": b.tenor_bucket,
                    "exposure_dv01_usd": str(b.dv01_usd),
                    "instrument": "Interest rate swap (OIS/SOFR) or Treasury futures, tenor-matched",
                    "suggested_direction": direction,
                    "rationale": (
                        f"Net DV01 of {b.dv01_usd:.0f} USD in {b.currency} {b.tenor_bucket} bucket exceeds "
                        f"materiality threshold of {dv01_materiality_usd}. Unhedged, a 100bp parallel move "
                        f"in this bucket moves book P&L by ~{(b.dv01_usd * 100):.0f} USD."
                    ),
                    "tradeoff": (
                        "Hedging removes directional rate risk but incurs swap bid/offer and ties up "
                        "ISDA/CSA-eligible balance sheet; carry cost depends on curve shape (positive "
                        "carry if curve inverted in the hedge's favor, negative if not)."
                    ),
                }
            )
    for fx in fx_exposures:
        if abs(fx.net_exposure_usd) >= fx_materiality_usd:
            recs.append(
                {
                    "type": "FX_HEDGE",
                    "currency": fx.currency,
                    "exposure_usd": str(fx.net_exposure_usd),
                    "instrument": "FX forward or FX swap, tenor-matched to weighted-average book maturity",
                    "suggested_direction": "Sell" if fx.net_exposure_usd > 0 else "Buy",
                    "rationale": (
                        f"Net {fx.currency} exposure of {fx.net_exposure_usd:.0f} USD-equivalent exceeds "
                        f"materiality threshold of {fx_materiality_usd}."
                    ),
                    "tradeoff": (
                        "FX forward removes translation risk but locks in forward points (positive or "
                        "negative carry depending on rate differential between USD and "
                        f"{fx.currency})."
                    ),
                }
            )
    return recs


def build_rates_fx_report(
    positions: Iterable[Position],
    fx_rates: dict,
    as_of: date,
) -> RatesAndFxRiskReport:
    positions = list(positions)
    dv01_buckets = compute_dv01(positions, as_of)
    fx_exposures = compute_fx_exposure(positions, fx_rates)
    funding_gap = compute_funding_gap(positions, as_of)
    hedges = recommend_hedges(dv01_buckets, fx_exposures)
    total_dv01 = sum((b.dv01_usd for b in dv01_buckets), Decimal("0"))

    return RatesAndFxRiskReport(
        as_of=as_of.isoformat(),
        dv01_by_bucket=dv01_buckets,
        total_dv01_usd=total_dv01,
        fx_exposures=fx_exposures,
        funding_gap_summary=funding_gap,
        hedge_recommendations=hedges,
    )
