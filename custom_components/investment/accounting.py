"""Pure accounting helpers for HA Investment."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class AssetQuantityBreakdown:
    """Gross/net asset quantities for one purchase."""

    gross: float
    net: float
    fee: float
    fee_percent: float


@dataclass(frozen=True, slots=True)
class PurchaseSettlementBreakdown:
    """Reconcile quoted trade value, retained asset fee and actual cash paid."""

    gross_trade_value: float | None
    net_asset_value: float | None
    withheld_asset_value: float
    cash_principal: float | None
    settlement_deduction: float
    embedded_asset_fee_cost: float
    asset_principal: float | None


def derive_purchase_settlement(
    *,
    gross_quantity: float,
    net_quantity: float,
    average_buy_price: float | None,
    investment_total: float | None,
    gross_trade_total: float | None = None,
) -> PurchaseSettlementBreakdown:
    """Split a purchase into asset value and embedded fee without double counting.

    The quoted unit price is always applied to the *gross* matched quantity.
    ``investment_total`` is the cash actually sent for the asset leg.  When a
    marketplace withholds units but reduces the seller payment (Bitcoin.de is
    one example), the difference between the cash paid and the quoted value of
    the net units is the buyer's embedded transaction cost.

    The embedded cost is informational/accounting classification only: it is
    already inside ``investment_total`` and must never be added to cash outflow
    a second time.
    """

    gross = _number(gross_quantity)
    net = _number(net_quantity)
    unit = _number(average_buy_price)
    cash = _number(investment_total)
    receipt_gross = _number(gross_trade_total)
    if gross is None or gross <= 0:
        raise ValueError("Gross quantity must be greater than zero")
    if net is None or net <= 0 or net > gross + 1e-12:
        raise ValueError("Net quantity must be greater than zero and not exceed gross quantity")

    quoted_gross = receipt_gross
    if quoted_gross is None and unit is not None:
        quoted_gross = gross * unit

    net_asset_value = net * unit if unit is not None else None

    # If no explicit cash principal was supplied, the normal settlement is the
    # gross quoted trade value.  This preserves legacy purchases.
    if cash is None:
        cash = quoted_gross

    settlement_deduction = 0.0
    if quoted_gross is not None and cash is not None:
        settlement_deduction = max(0.0, quoted_gross - cash)

    withheld_value = 0.0
    if unit is not None and gross > net + 1e-12:
        withheld_value = max(0.0, (gross - net) * unit)

    embedded = 0.0
    if cash is not None and net_asset_value is not None and withheld_value > 0:
        # The buyer cannot economically bear more of the unit-withholding fee
        # than the withheld asset was worth at the quoted trade price.
        embedded = min(withheld_value, max(0.0, cash - net_asset_value))

    asset_principal = None if cash is None else max(0.0, cash - embedded)
    return PurchaseSettlementBreakdown(
        gross_trade_value=quoted_gross,
        net_asset_value=net_asset_value,
        withheld_asset_value=withheld_value,
        cash_principal=cash,
        settlement_deduction=settlement_deduction,
        embedded_asset_fee_cost=embedded,
        asset_principal=asset_principal,
    )


def _number(value: float | int | str | None) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Asset quantities must be finite")
    if number < 0:
        raise ValueError("Asset quantities cannot be negative")
    return number


def derive_asset_quantities(
    *,
    quantity: float | None = None,
    gross_quantity: float | None = None,
    net_quantity: float | None = None,
    asset_fee_quantity: float | None = None,
    asset_fee_percent: float | None = None,
) -> AssetQuantityBreakdown:
    """Normalize a purchase where a provider may withhold part of the asset.

    ``quantity`` is the legacy/net quantity field. New callers should send gross
    and net quantities explicitly. The asset fee is quantity-denominated and is
    *not* an extra cash cost; it is the difference between gross and net units.
    """

    legacy = _number(quantity)
    gross = _number(gross_quantity)
    net = _number(net_quantity)
    fee = _number(asset_fee_quantity)
    pct = _number(asset_fee_percent)

    if pct is not None and pct >= 100:
        raise ValueError("Asset fee percentage must be below 100%")

    # Backwards compatibility: existing callers only supplied quantity, meaning
    # the whole purchased quantity was received.
    if gross is None and net is None:
        net = legacy
        gross = legacy
    elif net is None and legacy is not None:
        net = legacy

    if gross is None:
        if net is None:
            raise ValueError("Quantity must be greater than zero")
        if fee is not None:
            gross = net + fee
        elif pct is not None:
            gross = net / (1.0 - pct / 100.0)
        else:
            gross = net

    if gross <= 0:
        raise ValueError("Gross quantity must be greater than zero")

    if net is None:
        if fee is not None:
            net = gross - fee
        elif pct is not None:
            net = gross * (1.0 - pct / 100.0)
        else:
            net = gross

    if net < 0 or net > gross + 1e-12:
        raise ValueError("Net quantity cannot exceed gross quantity")

    inferred_fee = max(0.0, gross - net)
    if fee is not None and not math.isclose(fee, inferred_fee, rel_tol=1e-8, abs_tol=1e-12):
        raise ValueError("Asset fee quantity does not match gross and net quantity")
    fee = inferred_fee

    inferred_pct = (fee / gross * 100.0) if gross else 0.0
    if pct is not None and not math.isclose(pct, inferred_pct, rel_tol=1e-6, abs_tol=5e-4):
        raise ValueError("Asset fee percentage does not match gross and net quantity")

    if net <= 0:
        raise ValueError("Net quantity received must be greater than zero")

    return AssetQuantityBreakdown(gross=gross, net=net, fee=fee, fee_percent=inferred_pct)


def derive_purchase_principal(
    *,
    gross_quantity: float,
    average_buy_price: float | None = None,
    investment_total: float | None = None,
) -> tuple[float | None, float | None]:
    """Return quoted unit price and actual cash principal for a purchase.

    If only one monetary value is supplied, derive the other from gross units.
    If both are supplied, preserve both. Some marketplaces (for example a
    split-fee settlement) can quote a trade price whose gross trade value is
    different from the cash amount the buyer actually sends. P/L must use the
    explicit cash principal while the receipt can still retain its quoted price.
    """

    gross = _number(gross_quantity)
    if gross is None or gross <= 0:
        raise ValueError("Gross quantity must be greater than zero")

    unit = _number(average_buy_price)
    principal = _number(investment_total)
    unit = None if unit is None else round(unit, 12)
    principal = None if principal is None else round(principal, 2)

    if unit is None and principal is not None:
        unit = round(principal / gross, 2)
    elif principal is None and unit is not None:
        principal = round(unit * gross, 2)

    return unit, principal
