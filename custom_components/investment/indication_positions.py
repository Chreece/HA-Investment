"""Strict, network-free evidence for the portfolio owner's present units.

The mutable aggregate quantity and market-enriched display rows are not proof
of ownership. Storage migrates legitimate pre-ledger positions to BUY records;
this boundary therefore never substitutes a stored aggregate for a missing or
invalid ledger. It validates units, ownership and order without needing prices,
fees or FX, which cannot change how many units the owner holds.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
import hashlib
import json
import math
import re
from typing import Any

CONTRACT_VERSION = "transaction-owned-quantity-v1"
MAX_TRANSACTIONS = 20000
MAX_SHARED_ALLOCATIONS = 256
MAX_FINGERPRINT_HOLDINGS = 256
_FINGERPRINT_FIELDS = (
    "id", "provider", "provider_id", "symbol", "name", "category", "currency",
    "exchange", "isin", "share_class", "share_class_currency", "income_treatment",
    "distribution_policy", "leverage_factor", "leverage", "leveraged", "inverse",
    "high_yield", "replication", "currency_hedged", "transactions",
)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def portfolio_ledger_fingerprint(holdings: Any) -> str | None:
    """Hash ownership inputs so an in-flight edit cannot certify old holdings.

    Holding membership order is immaterial; record identities and complete
    stored transaction contents remain material. Enrichment fields such as
    price, status, aggregate quantity and display P/L are deliberately ignored.
    Unsupported/nonfinite JSON, duplicate stated holding IDs and excessive
    input sizes have no fingerprint and cannot be treated as an unchanged book.
    """
    if not isinstance(holdings, list) or len(holdings) > MAX_FINGERPRINT_HOLDINGS:
        return None
    canonical_rows: list[str] = []
    identities: set[str] = set()
    transaction_count = 0
    for holding in holdings:
        if not isinstance(holding, dict) or not isinstance(holding.get("transactions"), list):
            return None
        identifier = holding.get("id")
        if identifier is not None:
            if not isinstance(identifier, str) or not identifier.strip() or identifier in identities:
                return None
            identities.add(identifier)
        transaction_count += len(holding["transactions"])
        if transaction_count > MAX_TRANSACTIONS:
            return None
        selected = {key: holding[key] for key in _FINGERPRINT_FIELDS if key in holding}
        try:
            canonical = json.dumps(selected, sort_keys=True, allow_nan=False,
                                   ensure_ascii=True, separators=(",", ":"))
        except (ValueError, TypeError, OverflowError, RecursionError):
            return None
        canonical_rows.append(canonical)
    payload = "indication-owner-ledger-sha256-v1\n[" + ",".join(sorted(canonical_rows)) + "]"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quantity(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(float(value)):
            return None
    except (OverflowError, ValueError):
        return None
    result = Decimal(str(value))
    return result if result >= 0 else None


def _cutoff(as_of: Any) -> tuple[date | None, float | None, str | None]:
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            return None, None, "position_as_of_requires_timezone"
        # Transaction dates follow the user's HA calendar. Preserve the supplied
        # zone's date; created_at is independently checked as an absolute instant.
        return as_of.date(), as_of.timestamp(), None
    if isinstance(as_of, date):
        return as_of, None, None
    return None, None, "position_as_of_invalid"


def position_quantity_evidence(holding: Any, *, as_of: Any) -> dict[str, Any]:
    """Return verified net personal units or an explicit unknown result.

    Ordering uses transaction date, then complete recorded creation timestamp.
    IDs identify records but never invent temporal order. An ambiguous same-time
    BUY/SELL group is allowed only if opening personal units cover every SELL,
    so its quantity result is invariant to any within-group order. A later BUY
    cannot repair an earlier oversell. Missing creation timestamps are allowed
    for legacy dated records, with this same conservative ambiguity rule.
    """
    result: dict[str, Any] = {
        "contract": CONTRACT_VERSION, "source": "immutable_transaction_ledger",
        "status": "unknown", "quantity": None, "reasons": [],
        "transaction_count": 0, "stored_aggregate_used": False,
        "order_policy": "transaction_date_then_recorded_creation_time_no_id_tiebreak",
    }

    def reject(reason: str, tx_id: str | None = None) -> dict[str, Any]:
        result["reasons"] = [reason]
        if tx_id is not None:
            result["transaction_id"] = tx_id
        return result

    cutoff_day, cutoff_time, cutoff_error = _cutoff(as_of)
    if cutoff_error:
        return reject(cutoff_error)
    result["as_of_date"] = cutoff_day.isoformat()
    result["as_of_resolution"] = "instant_and_local_calendar" if cutoff_time is not None else "calendar_day"
    if not isinstance(holding, dict):
        return reject("position_ledger_holding_invalid")
    transactions = holding.get("transactions")
    if not isinstance(transactions, list) or not transactions:
        return reject("position_ledger_missing")
    if len(transactions) > MAX_TRANSACTIONS:
        return reject("position_ledger_too_large")
    result["transaction_count"] = len(transactions)
    identifiers: set[str] = set()
    # 720 decimal digits cover the full finite binary64 exponent range, its
    # smallest positive values, and the bounded number of additive records.
    with localcontext() as context:
        context.prec = 720
        groups: dict[tuple[date, int], list[tuple[str, Decimal, str]]] = defaultdict(list)
        total_shared = Decimal(0)
        total_personal_buys = Decimal(0)
        total_sells = Decimal(0)
        for transaction in transactions:
            if not isinstance(transaction, dict):
                return reject("position_ledger_transaction_invalid")
            tx_id = transaction.get("id")
            if not isinstance(tx_id, str) or not tx_id or tx_id != tx_id.strip() or len(tx_id) > 256:
                return reject("position_ledger_transaction_id_invalid")
            if tx_id in identifiers:
                return reject("position_ledger_duplicate_transaction", tx_id)
            identifiers.add(tx_id)
            tx_type = transaction.get("type")
            if tx_type not in ("buy", "sell"):
                return reject("position_ledger_transaction_type_invalid", tx_id)
            raw_day = transaction.get("date")
            if not isinstance(raw_day, str) or not _DAY.fullmatch(raw_day):
                return reject("position_ledger_date_invalid", tx_id)
            try:
                tx_day = date.fromisoformat(raw_day)
            except ValueError:
                return reject("position_ledger_date_invalid", tx_id)
            if tx_day > cutoff_day:
                return reject("position_ledger_future_date", tx_id)
            created = transaction.get("created_at")
            if created is not None:
                if isinstance(created, bool) or not isinstance(created, (int, float)):
                    return reject("position_ledger_creation_time_invalid", tx_id)
                try:
                    valid_created = math.isfinite(float(created)) and created > 0 and int(created) == created
                    created_day = datetime.fromtimestamp(created, UTC).date() if valid_created else None
                except (ValueError, OverflowError, OSError):
                    valid_created = False
                    created_day = None
                if not valid_created:
                    return reject("position_ledger_creation_time_invalid", tx_id)
                if (cutoff_time is not None and created > cutoff_time) or (cutoff_time is None and created_day > cutoff_day):
                    return reject("position_ledger_future_creation_time", tx_id)
            creation_order = int(created) if created is not None else 0
            if tx_type == "sell":
                quantity = _quantity(transaction.get("quantity"))
                if quantity is None or quantity <= 0:
                    return reject("position_ledger_sell_quantity_invalid", tx_id)
                if transaction.get("shared_allocations") not in (None, []):
                    return reject("position_ledger_sell_shared_ownership_invalid", tx_id)
                groups[(tx_day, creation_order)].append(("sell", quantity, tx_id))
                total_sells += quantity
                continue
            raw_net = transaction.get("net_quantity") if "net_quantity" in transaction else transaction.get("quantity")
            net = _quantity(raw_net)
            if net is None or net <= 0:
                return reject("position_ledger_buy_quantity_invalid", tx_id)
            if "quantity" in transaction:
                legacy_net = _quantity(transaction["quantity"])
                if legacy_net is None or legacy_net != net:
                    return reject("position_ledger_net_quantity_conflict", tx_id)
            if "gross_quantity" in transaction:
                gross = _quantity(transaction["gross_quantity"])
                if gross is None or gross < net:
                    return reject("position_ledger_gross_quantity_invalid", tx_id)
            shared_rows = transaction.get("shared_allocations", [])
            if shared_rows is None:
                shared_rows = []
            if not isinstance(shared_rows, list) or len(shared_rows) > MAX_SHARED_ALLOCATIONS:
                return reject("position_ledger_shared_ownership_invalid", tx_id)
            shared = Decimal(0)
            shared_ids: set[str] = set()
            for allocation in shared_rows:
                if not isinstance(allocation, dict):
                    return reject("position_ledger_shared_ownership_invalid", tx_id)
                name = allocation.get("participant") or allocation.get("name")
                if not isinstance(name, str) or not name.strip():
                    return reject("position_ledger_shared_participant_invalid", tx_id)
                shared_quantity = _quantity(allocation.get("quantity"))
                if shared_quantity is None or shared_quantity <= 0:
                    return reject("position_ledger_shared_quantity_invalid", tx_id)
                allocation_id = allocation.get("id")
                if allocation_id is not None:
                    if not isinstance(allocation_id, str) or not allocation_id.strip() or allocation_id != allocation_id.strip():
                        return reject("position_ledger_shared_id_invalid", tx_id)
                    if allocation_id in shared_ids:
                        return reject("position_ledger_duplicate_shared_allocation", tx_id)
                    shared_ids.add(allocation_id)
                shared += shared_quantity
            if shared > net:
                return reject("position_ledger_shared_units_exceed_net", tx_id)
            personal = net - shared
            total_shared += shared
            total_personal_buys += personal
            groups[(tx_day, creation_order)].append(("buy", personal, tx_id))

        # With even one undated creation time, that day's missing record could
        # fall anywhere among the timed records. Group the whole day instead of
        # silently treating an unknown BUY as an opening purchase.
        unknown_order_days = {day for day, created in groups if created == 0}
        ordered_groups: dict[tuple[date, int], list[tuple[str, Decimal, str]]] = defaultdict(list)
        for (day, created), records in groups.items():
            ordered_groups[(day, 0 if day in unknown_order_days else created)].extend(records)
        balance = Decimal(0)
        for _, records in sorted(ordered_groups.items()):
            bought = sum((quantity for kind, quantity, _ in records if kind == "buy"), Decimal(0))
            sold = sum((quantity for kind, quantity, _ in records if kind == "sell"), Decimal(0))
            if sold > balance:
                if bought > 0 and sold <= balance + bought:
                    return reject("position_ledger_transaction_order_ambiguous")
                return reject("position_ledger_oversell")
            balance += bought - sold
        try:
            quantity_float = float(balance)
            summaries = [float(total_personal_buys), float(total_shared), float(total_sells)]
        except (ValueError, OverflowError):
            return reject("position_ledger_quantity_not_representable")
        if not math.isfinite(quantity_float) or (balance > 0 and quantity_float <= 0) or any(not math.isfinite(number) for number in summaries):
            return reject("position_ledger_quantity_not_representable")
        result.update({
            "status": "verified", "quantity": quantity_float,
            "quantity_decimal": str(balance),
            "personal_buy_units": summaries[0], "shared_buy_units": summaries[1],
            "personal_sell_units": summaries[2],
        })
        return result
