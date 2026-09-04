"""Transaction-ledger accounting helpers for HA Investment."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
import math
from typing import Any


@dataclass(slots=True)
class LedgerSummary:
    """FIFO result for one asset in one normalized currency."""

    quantity: float
    remaining_cost_basis: float | None
    cost_basis_complete: bool
    realized_pnl: float | None
    realized_pnl_complete: bool
    rows: list[dict[str, Any]]
    total_buy_cash: float
    total_sell_proceeds: float
    total_explicit_costs: float


def validate_transaction_date(raw: str | None, *, today: date | None = None) -> str | None:
    """Validate an optional YYYY-MM-DD transaction date.

    ``today`` is injectable so the pure ledger contract can be tested without
    Home Assistant. The manager passes Home Assistant's local calendar date.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) != 10:
        raise ValueError("Transaction date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as err:
        raise ValueError("Transaction date must be YYYY-MM-DD") from err
    if parsed > (today or datetime.now(UTC).date()):
        raise ValueError("Transaction date cannot be in the future")
    return parsed.isoformat()


def transaction_date(transaction: dict[str, Any]) -> str:
    """Return YYYY-MM-DD for new or legacy transactions."""
    raw = str(transaction.get("date") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    created = int(transaction.get("created_at") or 0)
    if created > 0:
        return datetime.fromtimestamp(created, UTC).date().isoformat()
    return datetime.now(UTC).date().isoformat()


def transaction_timestamp(transaction: dict[str, Any]) -> int:
    """Timestamp used for chronological FIFO and historical quantity curves."""
    raw = transaction_date(transaction)
    date_ts = int(datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    created = int(transaction.get("created_at") or 0)
    # A created_at inside the same calendar day preserves entry order. Backdated
    # records use the requested historical date and their created_at only as a tie-breaker.
    return date_ts + (created % 86400 if created else 0)


def shared_quantity(transaction: dict[str, Any]) -> float:
    """Return units in a BUY that belong to other people.

    Shared allocations are transaction-local: the same instrument may be bought
    repeatedly with different friends/partners without merging those ownership
    records. Legacy transactions simply have no allocations and remain 100%
    personal.
    """
    if str(transaction.get("type") or "buy") == "sell":
        return 0.0
    total = 0.0
    for allocation in transaction.get("shared_allocations") or []:
        try:
            value = float(allocation.get("quantity") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(value) and value > 0:
            total += value
    return total


def personal_quantity(transaction: dict[str, Any]) -> float:
    """Return the units of a transaction that belong to this portfolio owner."""
    if str(transaction.get("type") or "buy") == "sell":
        return max(0.0, float(transaction.get("quantity") or 0))
    net = max(0.0, float(transaction.get("net_quantity", transaction.get("quantity", 0)) or 0))
    return max(0.0, net - shared_quantity(transaction))


def personal_ratio(transaction: dict[str, Any]) -> float:
    """Return the owner's economic share of a BUY, clamped to [0, 1]."""
    if str(transaction.get("type") or "buy") == "sell":
        return 1.0
    net = max(0.0, float(transaction.get("net_quantity", transaction.get("quantity", 0)) or 0))
    if net <= 0:
        return 1.0
    return min(1.0, max(0.0, personal_quantity(transaction) / net))




def shared_participant_summary(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate transaction-local shared units by participant label.

    This is display metadata only; FIFO remains based on each individual BUY.
    Keeping the accounting transaction-local means two purchases of the same
    instrument can have entirely different co-owners without merging lots.
    """
    totals: dict[str, float] = {}
    display_names: dict[str, str] = {}
    for tx in transactions or []:
        if str(tx.get("type") or "buy") == "sell":
            continue
        for allocation in tx.get("shared_allocations") or []:
            try:
                name = str(allocation.get("participant") or "").strip()
                quantity = float(allocation.get("quantity") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if not name or not math.isfinite(quantity) or quantity <= 0:
                continue
            key = name.casefold()
            display_names.setdefault(key, name)
            totals[key] = totals.get(key, 0.0) + quantity
    return [
        {"participant": display_names[key], "quantity": round(totals[key], 12)}
        for key in sorted(totals, key=lambda item: display_names[item].casefold())
    ]

def normalize_shared_allocations(
    allocations: list[dict[str, Any]] | None, net_quantity: float
) -> list[dict[str, Any]]:
    """Validate and normalize transaction-local shared ownership rows.

    Each row needs a human-readable participant label and a positive unit
    quantity. The sum may equal the full net quantity (the portfolio owner then
    economically owns zero of this BUY) but can never exceed it.
    """
    clean: list[dict[str, Any]] = []
    total = 0.0
    for raw in allocations or []:
        if not isinstance(raw, dict):
            raise ValueError("Shared ownership rows must be objects")
        name = str(raw.get("participant") or raw.get("name") or "").strip()[:120]
        if not name:
            raise ValueError("Every shared ownership row needs a participant name")
        try:
            quantity = float(raw.get("quantity"))
        except (TypeError, ValueError) as err:
            raise ValueError("Shared quantity must be a number") from err
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Shared quantity must be greater than zero")
        quantity = round(quantity, 12)
        total += quantity
        item = {
            "participant": name,
            "quantity": quantity,
        }
        allocation_id = str(raw.get("id") or "").strip()
        if allocation_id:
            item["id"] = allocation_id[:64]
        input_mode = str(raw.get("input_mode") or "").strip()
        if input_mode in {"units", "percent", "investment", "pool_percent"}:
            item["input_mode"] = input_mode
        for key in ("input_value", "pool_percent"):
            if raw.get(key) in (None, ""):
                continue
            try:
                value = float(raw.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                item[key] = round(value, 12)
        clean.append(item)
    net = max(0.0, float(net_quantity or 0))
    if total > net + 1e-10:
        raise ValueError("Shared units cannot exceed the net units received")
    return clean



def normalize_shared_ownership(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize presentation metadata for a transaction's ownership editor.

    Accounting never trusts this metadata: normalized shared allocations remain the
    source of truth for units excluded from the current user's portfolio.  The
    metadata only lets the UI restore whether the user entered a common pool or
    per-person amounts and which unit/percent/money basis they used.
    """
    if not raw or not isinstance(raw, dict):
        return None
    style = str(raw.get("style") or "direct").strip().lower()
    if style not in {"direct", "common"}:
        style = "direct"
    clean: dict[str, Any] = {"style": style}
    for key in ("common_mode", "user_input_mode"):
        mode = str(raw.get(key) or "").strip().lower()
        if mode in {"units", "percent", "investment"}:
            clean[key] = mode
    for key in ("common_value", "user_common_percent", "user_input_value"):
        if raw.get(key) in (None, ""):
            continue
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            clean[key] = round(value, 12)
    return clean

def normalize_legacy_transaction(transaction: dict[str, Any]) -> bool:
    """Populate ledger metadata on an old transaction in-place.

    Returns True when the record was changed.
    """
    changed = False
    if transaction.get("type") not in {"buy", "sell"}:
        transaction["type"] = "buy"
        changed = True
    if not transaction.get("date"):
        transaction["date"] = transaction_date(transaction)
        changed = True
    if transaction["type"] == "buy":
        if "shared_allocations" not in transaction:
            transaction["shared_allocations"] = []
            changed = True
        if "net_quantity" not in transaction:
            transaction["net_quantity"] = float(transaction.get("quantity") or 0)
            changed = True
        if "gross_quantity" not in transaction:
            transaction["gross_quantity"] = float(transaction.get("net_quantity") or transaction.get("quantity") or 0)
            changed = True
    return changed


def quantity_at(transactions: list[dict[str, Any]], timestamp: int) -> float:
    """Return units owned at a historical timestamp from immutable ledger rows."""
    quantity = 0.0
    for tx in sorted(transactions, key=lambda item: (transaction_timestamp(item), str(item.get("id") or ""))):
        if transaction_timestamp(tx) > timestamp:
            break
        if tx.get("type", "buy") == "sell":
            quantity -= float(tx.get("quantity") or 0)
        else:
            quantity += personal_quantity(tx)
    return max(0.0, quantity)


def fifo_summary(records: list[dict[str, Any]], current_price: float | None = None) -> LedgerSummary:
    """Apply immutable BUY/SELL records to FIFO lots.

    Input monetary values must already be converted to one common currency.
    BUY records accept ``cash_principal`` and ``explicit_costs``. SELL records
    accept ``net_proceeds`` and ``explicit_costs``. The returned rows preserve
    every original record and only add derived FIFO/status fields.
    """
    ordered = sorted(
        (deepcopy(record) for record in records),
        key=lambda item: (int(item.get("sort_ts") or 0), str(item.get("id") or "")),
    )
    lots: list[dict[str, Any]] = []
    rows_by_id: dict[str, dict[str, Any]] = {}
    realized_total = 0.0
    realized_complete = True
    total_buy_cash = 0.0
    total_sell_proceeds = 0.0
    total_explicit_costs = 0.0

    for record in ordered:
        tx_type = str(record.get("type") or "buy")
        tx_id = str(record.get("id") or "")
        qty = max(0.0, float(record.get("quantity") or 0))
        costs = max(0.0, float(record.get("explicit_costs") or 0))
        total_explicit_costs += costs
        record["fifo_allocations"] = []

        if tx_type == "buy":
            cash = record.get("cash_principal")
            all_in = None if cash is None else max(0.0, float(cash)) + costs
            if cash is not None:
                total_buy_cash += max(0.0, float(cash)) + costs
            unit_cost = all_in / qty if all_in is not None and qty > 0 else None
            lot = {
                "id": tx_id,
                "original_quantity": qty,
                "remaining_quantity": qty,
                "unit_cost": unit_cost,
                "realized_pnl": 0.0,
                "realized_complete": True,
            }
            lots.append(lot)
            record.update(
                {
                    "original_quantity": qty,
                    "remaining_quantity": qty,
                    "status": "open",
                    "all_in_cost": all_in,
                    "realized_pnl_allocated": 0.0,
                }
            )
            rows_by_id[tx_id] = record
            continue

        # SELL
        remaining_to_sell = qty
        allocated_cost = 0.0
        allocation_complete = True
        allocations: list[dict[str, Any]] = []
        for lot in lots:
            available = float(lot["remaining_quantity"])
            if available <= 1e-12 or remaining_to_sell <= 1e-12:
                continue
            consumed = min(available, remaining_to_sell)
            lot["remaining_quantity"] = max(0.0, available - consumed)
            remaining_to_sell -= consumed
            if lot["unit_cost"] is None:
                allocation_complete = False
                cost = None
            else:
                cost = consumed * float(lot["unit_cost"])
                allocated_cost += cost
            allocations.append({"buy_id": lot["id"], "quantity": consumed, "cost_basis": cost})

        if remaining_to_sell > 1e-9:
            raise ValueError("Sell quantity exceeds units owned on the transaction date")

        proceeds = record.get("net_proceeds")
        if proceeds is not None:
            proceeds = max(0.0, float(proceeds))
            total_sell_proceeds += proceeds
        realized = (
            proceeds - allocated_cost
            if proceeds is not None and allocation_complete
            else None
        )
        if realized is None:
            realized_complete = False
        else:
            realized_total += realized

        record.update(
            {
                "status": "sold",
                "allocated_cost_basis": allocated_cost if allocation_complete else None,
                "realized_pnl": realized,
                "fifo_allocations": allocations,
            }
        )
        rows_by_id[tx_id] = record

        # Allocate realized result back to the consumed BUY rows for historical
        # lot status. This never mutates the stored transaction itself.
        if proceeds is not None and qty > 0:
            for allocation in allocations:
                lot_row = rows_by_id.get(str(allocation["buy_id"]))
                if not lot_row:
                    continue
                share = float(allocation["quantity"]) / qty
                alloc_proceeds = proceeds * share
                alloc_cost = allocation.get("cost_basis")
                if alloc_cost is None:
                    lot_row["realized_pnl_allocated"] = None
                elif lot_row.get("realized_pnl_allocated") is not None:
                    lot_row["realized_pnl_allocated"] += alloc_proceeds - float(alloc_cost)

    remaining_cost = 0.0
    cost_complete = True
    quantity = 0.0
    for lot in lots:
        remaining = max(0.0, float(lot["remaining_quantity"]))
        quantity += remaining
        row = rows_by_id.get(str(lot["id"]))
        if row is not None:
            row["remaining_quantity"] = remaining
            row["status"] = "closed" if remaining <= 1e-12 else ("partial" if remaining + 1e-12 < float(lot["original_quantity"]) else "open")
            if current_price is not None:
                row["today_value"] = remaining * current_price
                if lot["unit_cost"] is not None:
                    row["unrealized_pnl"] = remaining * (current_price - float(lot["unit_cost"]))
                else:
                    row["unrealized_pnl"] = None
            else:
                row["today_value"] = None
                row["unrealized_pnl"] = None
            realized_part = row.get("realized_pnl_allocated")
            unrealized_part = row.get("unrealized_pnl")
            if realized_part is None or unrealized_part is None:
                # A fully-open lot has no realized component; a fully-closed lot
                # has no unrealized component. Treat those structural zeros
                # explicitly while preserving unknown cost/proceeds as unknown.
                if row["status"] == "open" and unrealized_part is not None:
                    row["total_pnl"] = unrealized_part
                elif row["status"] == "closed" and realized_part is not None:
                    row["total_pnl"] = realized_part
                else:
                    row["total_pnl"] = None
            else:
                row["total_pnl"] = float(realized_part) + float(unrealized_part)
        if remaining <= 1e-12:
            continue
        if lot["unit_cost"] is None:
            cost_complete = False
        else:
            remaining_cost += remaining * float(lot["unit_cost"])

    if current_price is not None:
        for record in ordered:
            if record.get("type") == "sell":
                record["today_value"] = float(record.get("quantity") or 0) * current_price

    # rows_by_id contains the same row objects present in ordered.
    return LedgerSummary(
        quantity=quantity,
        remaining_cost_basis=remaining_cost if cost_complete else None,
        cost_basis_complete=cost_complete,
        realized_pnl=realized_total if realized_complete else None,
        realized_pnl_complete=realized_complete,
        rows=ordered,
        total_buy_cash=total_buy_cash,
        total_sell_proceeds=total_sell_proceeds,
        total_explicit_costs=total_explicit_costs,
    )
