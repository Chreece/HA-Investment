"""Conservative, cash-funded purchase planning with explicit cost assumptions.

This module never recommends an instrument or increases an approved purchase.
It reduces a pre-approved set of rows until raw-price purchases, user-confirmed
cost estimates and conservative cent rounding fit the available cash. A missing
cost profile is unknown, including when a broker might in fact charge zero.

All four cost inputs apply to every purchase. Fixed fees use portfolio currency;
commission is a percentage of marked principal; spread and FX charges are basis
points of the same principal. Users must enter only costs not already included
in the supplied price. These estimates do not assert broker execution prices.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import math
from typing import Any


CENT = Decimal("0.01")
UNIT_STEP = Decimal("0.000000000001")
ZERO = Decimal(0)
ONE = Decimal(1)
_LIMITS = {
    "fixed_fee": Decimal("1000000000"),
    "commission_pct": Decimal(100),
    "spread_bps": Decimal(10000),
    "fx_bps": Decimal(10000),
}


def _number(value: Any) -> Decimal | None:
    """Accept actual finite numbers, never bools, coercible objects or strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return result if result.is_finite() else None


def validate_execution_costs(raw: Any) -> dict[str, Any]:
    """Validate an explicit per-purchase cost profile without defaulting to zero."""
    result: dict[str, Any] = {
        "status": "unknown", "confirmed": False,
        "fixed_fee": None, "commission_pct": None,
        "spread_bps": None, "fx_bps": None,
        "reasons": [],
    }
    if raw is None:
        result["reasons"] = ["execution_costs_unknown"]
        return result
    if not isinstance(raw, Mapping):
        result.update(status="invalid", reasons=["execution_costs_invalid"])
        return result
    if not isinstance(raw.get("confirmed"), bool):
        result["reasons"].append("execution_costs_confirmation_invalid")
    for key, maximum in _LIMITS.items():
        value = _number(raw.get(key))
        if value is None or value < ZERO or value > maximum:
            result["reasons"].append(f"execution_costs_{key}_invalid")
        else:
            result[key] = float(value)
    if result["reasons"]:
        # Unconfirmed, empty controls still represent unknown costs. A malformed
        # confirmed profile is always invalid and cannot be mistaken for zero.
        result["status"] = "invalid"
    elif raw["confirmed"] is not True:
        result["reasons"] = ["execution_costs_unknown"]
    else:
        result.update(status="confirmed", confirmed=True)
    return result


def _floor_cash(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def _ceil_cash(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_CEILING)


def _float_down(value: Decimal) -> float:
    """Serialize units without a float conversion exceeding the Decimal cap."""
    result = float(value)
    if Decimal(str(result)) > value:
        result = math.nextafter(result, 0.0)
    return result


def _float_up(value: Decimal) -> float:
    """Keep the exact marked principal inside its serialized numeric bound.

    A nearest float can round the principal below ``units * raw_price``.
    Reusing that output as the next purchase ceiling would then discard an
    otherwise unchanged whole unit. The least float whose decimal spelling
    covers the principal avoids that loss without changing cash accounting.
    The caller first bounds quantities by a representable incoming ceiling,
    so this serialization cannot increase the original purchase authority.
    """
    result = float(value)
    if Decimal(str(result)) < value:
        result = math.nextafter(result, math.inf)
    return result


def apply_execution_costs(
    rows: Iterable[dict[str, Any]],
    budget: float | int | Decimal | None,
    profile: Any,
    *,
    amount_key: str = "suggested_amount",
    units_key: str = "suggested_units",
    reserve_amount: float | int | Decimal = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return only downward purchase adjustments plus a cash-accounting summary.

    A common proportional scale preserves the allocator's relative intent; lot
    and cash rounding can leave additional cash. This is not an optimal trading
    strategy. Fixed fees are charged only when a purchase remains positive.

    ``reserve_amount`` is cash within ``budget`` which must remain unspent after
    costs. The caller must validate portfolio risk again after these independent
    row reductions: changing composition can remove a useful hedge.

    Reapplication is safe after any later risk/AI reductions. It recomputes costs
    from the final quantities and never restores quantities removed earlier.
    """
    with localcontext() as context:
        context.prec = 60
        return _apply_execution_costs(
            rows, budget, profile, amount_key=amount_key, units_key=units_key,
            reserve_amount=reserve_amount,
        )


def _apply_execution_costs(
    rows: Iterable[dict[str, Any]],
    budget: float | int | Decimal | None,
    profile: Any,
    *,
    amount_key: str,
    units_key: str,
    reserve_amount: float | int | Decimal,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output = [dict(row) for row in rows]
    checked = validate_execution_costs(profile)
    prefix = "ai_" if amount_key.startswith("ai_") else ""
    cost_key = f"{prefix}estimated_transaction_cost"
    debit_key = f"{prefix}estimated_cash_debit"
    reason_key = f"{prefix}execution_cost_blockers"
    budget_value = _number(budget)
    reserve_value = _number(reserve_amount)
    reasons = list(checked["reasons"])
    summary: dict[str, Any] = {
        "status": checked["status"], "confirmed": checked["confirmed"],
        "profile": checked, "budget": None,
        "principal": 0.0, "estimated_transaction_cost": 0.0,
        "estimated_cash_debit": 0.0, "rounding_allowance": 0.0,
        "cash_remaining": None, "reserve_amount": None,
        "purchase_count": 0, "reduced_count": 0, "scale": 0.0,
        "reasons": reasons,
        "contract": "confirmed-costs-downward-cash-debit-v1",
        "cost_basis": "portfolio_currency_marked_principal",
    }
    if budget is None:
        summary["status"] = "not_requested"
        for row in output:
            row[cost_key] = None
            row[debit_key] = None
        return output, summary
    if budget_value is None or budget_value < ZERO or budget_value > Decimal("1e15"):
        reasons.append("execution_budget_invalid")
    if reserve_value is None or reserve_value < ZERO:
        reasons.append("execution_reserve_invalid")
    invalid_budget = budget_value is None or budget_value < ZERO or budget_value > Decimal("1e15")
    if not invalid_budget:
        summary["budget"] = float(budget_value)
        summary["cash_remaining"] = float(budget_value)
    if reserve_value is not None and reserve_value >= ZERO and not invalid_budget:
        summary["reserve_amount"] = float(reserve_value)
        if reserve_value > budget_value:
            reasons.append("execution_reserve_exceeds_budget")
    if reasons:
        if any(reason.startswith(("execution_budget_", "execution_reserve_")) for reason in reasons):
            summary["status"] = "invalid"
        for row in output:
            row[amount_key] = 0.0
            row[units_key] = 0.0
            row[cost_key] = 0.0
            row[debit_key] = 0.0
            row[reason_key] = list(reasons)
            if not prefix:
                row["allocation_eligible"] = False
        return output, summary
    assert budget_value is not None and reserve_value is not None
    available = max(ZERO, _floor_cash(budget_value) - _ceil_cash(reserve_value))
    fixed = Decimal(str(checked["fixed_fee"]))
    rate = (
        Decimal(str(checked["commission_pct"])) / 100
        + Decimal(str(checked["spread_bps"])) / 10000
        + Decimal(str(checked["fx_bps"])) / 10000
    )
    prepared: list[tuple[int, Decimal, Decimal, bool]] = []
    for index, row in enumerate(output):
        row[cost_key] = 0.0
        row[debit_key] = 0.0
        row[reason_key] = []
        ceiling = _number(row.get(amount_key))
        old_units = _number(row.get(units_key))
        price = _number(row.get("portfolio_price") if "portfolio_price" in row else row.get("price"))
        whole = row.get("whole_units_only") is True
        if ceiling is not None and ceiling == ZERO:
            row[amount_key], row[units_key] = 0.0, 0.0
            continue
        blocked = row.get("allocation_eligible") is False or any(
            row.get(key) is False for key in ("data_quality_eligible", "instrument_identity_eligible")
        )
        if (
            ceiling is None or ceiling < ZERO or ceiling > Decimal("1e15")
            or old_units is None or old_units < ZERO or old_units > Decimal("1e24")
            or price is None or price <= ZERO or price > Decimal("1e15")
            or blocked
        ):
            row[amount_key], row[units_key] = 0.0, 0.0
            row[reason_key] = ["execution_purchase_unverified"]
            if not prefix:
                row["allocation_eligible"] = False
            continue
        # Public purchase amounts are JSON numbers. A float input already is
        # a representable bound; a higher-precision Decimal input may not be.
        # Narrow such a cap before deriving units, so the returned principal
        # can cover their exact marked value without exceeding the caller's
        # original cap. Never invent authority from prior output metadata.
        serialized_ceiling = Decimal(str(_float_down(ceiling)))
        units = min(old_units, serialized_ceiling / price)
        step = ONE if whole else UNIT_STEP
        units = units.quantize(step, rounding=ROUND_FLOOR)
        prepared.append((index, price, units, whole))

    def project(scale: Decimal) -> tuple[list[tuple[int, float, Decimal, Decimal, Decimal]], Decimal]:
        projected: list[tuple[int, float, Decimal, Decimal, Decimal]] = []
        total_debit = ZERO
        for index, price, cap_units, whole in prepared:
            step = ONE if whole else UNIT_STEP
            units = (cap_units * scale).quantize(step, rounding=ROUND_FLOOR)
            units_float = _float_down(units)
            units = Decimal(str(units_float))
            principal = units * price
            estimate = fixed + principal * rate if principal > ZERO else ZERO
            debit = _ceil_cash(principal + estimate) if principal > ZERO else ZERO
            projected.append((index, units_float, principal, debit, estimate))
            total_debit += debit
        return projected, total_debit

    full, full_debit = project(ONE)
    scale = ONE
    if full_debit > available:
        low, high = ZERO, ONE
        # The monotone debit includes fixed fees, whole lots and cash rounding.
        # Ninety-six iterations bound scale uncertainty far below our unit grid.
        for _ in range(96):
            mid = (low + high) / 2
            _, debit = project(mid)
            if debit <= available:
                low = mid
            else:
                high = mid
        scale = low
        full, full_debit = project(scale)
    principal_total, cost_total, rounding_total = ZERO, ZERO, ZERO
    for index, units, principal, debit, estimate in full:
        row = output[index]
        before = _number(row.get(amount_key)) or ZERO
        row[units_key] = units
        row[amount_key] = _float_up(principal)
        if Decimal(str(row[amount_key])) > before:
            raise AssertionError("Serialized purchase principal exceeded its ceiling")
        # All reserved cash beyond marked principal is reported as estimated cost,
        # with the cash-rounding component available separately for auditability.
        row[cost_key] = float(debit - principal)
        row[debit_key] = float(debit)
        row[f"{prefix}execution_rounding_allowance"] = float(debit - principal - estimate)
        if principal < before:
            summary["reduced_count"] += 1
        if principal > ZERO:
            summary["purchase_count"] += 1
        elif before > ZERO:
            row[reason_key] = ["execution_costs_no_affordable_purchase"]
        if not prefix:
            row["allocation_eligible"] = principal > ZERO
        principal_total += principal
        cost_total += debit - principal
        rounding_total += debit - principal - estimate
    if full_debit > available:
        raise AssertionError("Purchase plan exceeded cash after costs")
    summary.update(
        status="confirmed", principal=float(principal_total),
        estimated_transaction_cost=float(cost_total), estimated_cash_debit=float(full_debit),
        rounding_allowance=float(rounding_total), cash_remaining=float(budget_value - full_debit),
        scale=float(scale),
    )
    if prepared and summary["purchase_count"] == 0:
        summary["reasons"].append("execution_costs_no_affordable_purchase")
    return output, summary
