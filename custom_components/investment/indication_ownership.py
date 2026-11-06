"""Exact-owned filtering from verified local units, without display fallbacks.

Quantity uncertainty belongs to the instrument whose ledger is uncertain. It
must not turn into a verified zero, but it must not bring unrelated holdings
back into the new contribution's risk calculation either. An unidentifiable
record is different: its uncertainty cannot be scoped to a particular asset.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any

from .indication_positions import (
    MAX_FINGERPRINT_HOLDINGS,
    MAX_TRANSACTIONS,
    position_quantity_evidence,
)
from .portfolio_context import (
    find_ambiguous_identity_match,
    find_owned_match,
    instrument_identity_tokens,
)

CONTRACT_VERSION = "transaction-owned-filter-v1"
_IDENTITY_FIELDS = (
    "provider", "provider_id", "symbol", "category", "currency",
    "quote_currency", "exchange", "isin", "figi", "share_class_figi",
)


def _identity_tokens(holding: dict[str, Any]) -> set[str]:
    """Do not manufacture identity aliases by stringifying corrupt metadata."""
    for key in _IDENTITY_FIELDS:
        value = holding.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            return set()
    return instrument_identity_tokens(holding)


def _index_record(index: dict[str, list[dict[str, Any]]], row: dict[str, Any], tokens: set[str]) -> None:
    for token in tokens:
        index.setdefault(token, []).append(row)


def build_ownership_snapshot(holdings: Any, *, as_of: Any) -> dict[str, Any]:
    """Return candidate-scoped ownership evidence from a bounded raw ledger.

    ``holdings`` contains only identified, verified, positive personal positions.
    ``unknown_index`` holds identified positions whose quantities cannot be
    verified. ``unidentified_unknown`` means a no-match cannot prove nonownership.
    ``reasons`` describes only that global uncertainty; ``issues`` includes all
    uncertain records. Consumers must use candidate_ownership_evidence rather
    than treating any unrelated issue as a global portfolio-risk veto.
    """
    snapshot: dict[str, Any] = {
        "contract": CONTRACT_VERSION,
        "holdings": [],
        "owned_index": {},
        "unknown_index": {},
        "unidentified_unknown": False,
        "reasons": [],
        "issues": [],
        "stored_aggregate_used": False,
    }

    def global_issue(reason: str) -> None:
        snapshot["unidentified_unknown"] = True
        if reason not in snapshot["reasons"]:
            snapshot["reasons"].append(reason)

    if not isinstance(as_of, date):
        global_issue("position_as_of_invalid")
        return snapshot
    if isinstance(as_of, datetime) and (as_of.tzinfo is None or as_of.utcoffset() is None):
        global_issue("position_as_of_requires_timezone")
        return snapshot
    if not isinstance(holdings, list):
        global_issue("ownership_holdings_unavailable")
        return snapshot
    if len(holdings) > MAX_FINGERPRINT_HOLDINGS:
        global_issue("ownership_holdings_too_large")
        return snapshot

    total_transactions = sum(
        len(holding["transactions"])
        for holding in holdings
        if isinstance(holding, dict) and isinstance(holding.get("transactions"), list)
    )
    if total_transactions > MAX_TRANSACTIONS:
        global_issue("ownership_transactions_too_large")
        return snapshot

    identifiers = Counter(
        holding["id"] for holding in holdings
        if isinstance(holding, dict) and isinstance(holding.get("id"), str)
    )
    for holding in holdings:
        if not isinstance(holding, dict):
            global_issue("ownership_holding_invalid")
            snapshot["issues"].append({"holding_id": None, "reasons": ["ownership_holding_invalid"]})
            continue

        quantity_evidence = position_quantity_evidence(holding, as_of=as_of)
        identifier = holding.get("id")
        identity_issue = None
        if identifier is not None and (
            not isinstance(identifier, str) or not identifier.strip()
            or identifier != identifier.strip() or len(identifier) > 256
        ):
            identity_issue = "ownership_holding_id_invalid"
        elif isinstance(identifier, str) and identifiers[identifier] > 1:
            identity_issue = "ownership_holding_id_duplicate"
        if identity_issue:
            quantity_evidence = {
                **quantity_evidence, "status": "unknown", "quantity": None,
                "reasons": [identity_issue],
            }

        verified = quantity_evidence.get("status") == "verified"
        quantity = quantity_evidence.get("quantity") if verified else None
        if verified and quantity == 0:
            # A complete sale or fully shared custody proves no personal units,
            # independently of the display aggregate or now-stale listing data.
            continue

        tokens = _identity_tokens(holding)
        if not tokens:
            global_issue("ownership_identity_unavailable")
            snapshot["issues"].append({
                "holding_id": identifier if isinstance(identifier, str) else None,
                "reasons": ["ownership_identity_unavailable", *(quantity_evidence.get("reasons") or [])],
            })
            continue

        # Identity scalars and verified quantity are captured now. A concurrent
        # in-place edit of the source row must not alter this snapshot's match.
        row = {
            **{key: holding[key] for key in _IDENTITY_FIELDS if key in holding},
            "id": identifier if isinstance(identifier, str) else None,
            "name": holding.get("name") if isinstance(holding.get("name"), str) else None,
            "quantity": quantity, "net_quantity": quantity,
            "personal_quantity": quantity,
            "position_quantity_evidence": quantity_evidence,
        }
        if verified:
            snapshot["holdings"].append(row)
            # Exact ownership includes arbitrarily small positive personal units;
            # the display-oriented index's epsilon is inappropriate here.
            _index_record(snapshot["owned_index"], row, tokens)
        else:
            _index_record(snapshot["unknown_index"], row, tokens)
            snapshot["issues"].append({
                "holding_id": identifier if isinstance(identifier, str) else None,
                "symbol": holding.get("symbol"),
                "reasons": list(quantity_evidence.get("reasons") or ["ownership_quantity_unverified"]),
            })

    return snapshot


def candidate_ownership_evidence(candidate: Any, snapshot: Any) -> dict[str, Any]:
    """Return owned, not_owned or unknown without confusing absence and error."""
    result: dict[str, Any] = {"status": "unknown", "reasons": [], "holding_id": None}
    if (
        not isinstance(snapshot, dict) or snapshot.get("contract") != CONTRACT_VERSION
        or not isinstance(snapshot.get("owned_index"), dict)
        or not isinstance(snapshot.get("unknown_index"), dict)
        or not isinstance(snapshot.get("unidentified_unknown"), bool)
        or not isinstance(snapshot.get("reasons"), list)
    ):
        result["reasons"] = ["ownership_snapshot_unavailable"]
        return result
    if not isinstance(candidate, dict) or not _identity_tokens(candidate):
        result["reasons"] = ["ownership_candidate_identity_unavailable"]
        return result

    owned = find_owned_match(candidate, snapshot["owned_index"])
    if owned is not None:
        result.update({
            "status": "owned", "holding_id": owned.get("id"),
            "quantity": owned["quantity"], "holding": owned,
        })
        return result

    unknown = find_owned_match(candidate, snapshot["unknown_index"])
    if unknown is not None:
        result.update({
            "holding_id": unknown.get("id"), "holding": unknown,
            "reasons": list(unknown["position_quantity_evidence"].get("reasons")
                            or ["ownership_quantity_unverified"]),
        })
        return result

    for index in (snapshot["owned_index"], snapshot["unknown_index"]):
        ambiguous = find_ambiguous_identity_match(candidate, index)
        if ambiguous is not None:
            result.update({
                "holding_id": ambiguous.get("id"), "holding": ambiguous,
                "reasons": [
                    "ownership_identity_ambiguous",
                    *(ambiguous["position_quantity_evidence"].get("reasons") or []),
                ],
            })
            return result

    if snapshot["unidentified_unknown"]:
        result["reasons"] = list(snapshot["reasons"] or ["ownership_snapshot_unavailable"])
        return result

    result["status"] = "not_owned"
    return result
