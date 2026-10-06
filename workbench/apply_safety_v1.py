"""Reproduce the locally tested patch on an exact detached base checkout."""
from pathlib import Path
import hashlib
import sys

p = Path(sys.argv[1]) / 'custom_components/investment/validated_model.py'
c = p.read_text()
assert hashlib.sha256(c.encode()).hexdigest() == '9e639fc143bdd5db8623a33e8cfd2ed0165a0fc750aecd9242542b2748de72b5'


def replace(old, new):
    global c
    assert c.count(old) == 1, f'Unexpected source marker: {old[:100]}'
    c = c.replace(old, new)


replace('VALIDATED_SIGNAL_STRATEGY = "adaptive"', 'VALIDATED_SIGNAL_STRATEGY = "adaptive"\nSAFETY_CONTRACT_VERSION = "approved-candidates-aligned-history-v1"')
replace('''    for index in whole_indices:
        item = rows[index]
        price = _sf(item.get("portfolio_price") or item.get("price"))''', '''    for index in whole_indices:
        item = rows[index]
        # A zero target can be rounding, or a prior rejection. Only the original
        # positive approved set may receive redistributed whole-lot capacity.
        if weighted_by_key.get(_identity(item), 0.0) <= EPS:
            continue
        price = _sf(item.get("portfolio_price") or item.get("price"))''')
start = c.index('def portfolio_weekly_returns(')
end = c.index('\ndef expected_shortfall_loss(', start)
c = c[:start] + '''def _validated_risk_map(raw: Any) -> dict[str, float] | None:
    """Validate a complete return map; missing or corrupt data is not cash.

    Keys are observation identifiers (ISO weeks in the provider path). Numeric
    validation never silently drops an observation or substitutes zero. Calendar
    adjacency is enforced by ``weekly_return_map_from_points`` before this map
    reaches the model. Freshness/as-of validation is a separate provider concern.
    """
    if not isinstance(raw, dict) or len(raw) < MIN_RISK_HISTORY_WEEKS:
        return None
    clean: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip() or isinstance(value, bool):
            return None
        number = _sf(value, math.nan)
        if not math.isfinite(number) or number < -1.0:
            return None
        clean[key] = number
    return clean


def portfolio_weekly_returns(
    weighted: Iterable[tuple[dict[str, Any], float]],
) -> list[float]:
    """Use only aligned observations, with usable history for every position.

    Zero-weight instruments need no risk history. Every positive position does,
    including tiny positions: scaling exposure cannot repair missing evidence.
    No usable common window means unknown portfolio risk, not zero risk.
    """
    maps: list[tuple[float, dict[str, float]]] = []
    for item, weight in weighted:
        number = _sf(weight, math.nan)
        if isinstance(weight, bool) or not math.isfinite(number) or number < 0.0:
            return []
        if number == 0.0:
            continue
        clean = _validated_risk_map(item.get("risk_weekly_returns"))
        if clean is None:
            return []
        maps.append((number, clean))
    if not maps:
        return []
    weeks = sorted(set.intersection(*(set(values) for _, values in maps)))
    # risk_signature requires at least MIN_RISK_HISTORY_WEEKS common periods.
    return [sum(weight * values[week] for weight, values in maps) for week in weeks]

''' + c[end:]
replace('''    for index in range(1, len(ordered)):
        previous = ordered[index - 1][1][1]
        if previous <= 0:
            continue
        (year, week), (_, value) = ordered[index]
        out[f"{year:04d}-W{week:02d}"] = value / previous - 1.0''', '''    for index in range(1, len(ordered)):
        (previous_year, previous_week), (_, previous) = ordered[index - 1]
        (year, week), (_, value) = ordered[index]
        previous_monday = dt.date.fromisocalendar(previous_year, previous_week, 1)
        monday = dt.date.fromisocalendar(year, week, 1)
        if previous <= 0 or (monday - previous_monday).days != 7:
            # A multiweek price change is not a one-week risk observation.
            continue
        out[f"{year:04d}-W{week:02d}"] = value / previous - 1.0''')
replace('    risk_eligible = len(risk_map) >= MIN_RISK_HISTORY_WEEKS', '    risk_eligible = _validated_risk_map(risk_map) is not None')
replace('''        risk_map = item.get("risk_weekly_returns")
        if not isinstance(risk_map, dict) or len(risk_map) < MIN_RISK_HISTORY_WEEKS:
            item["activation"] = 0.0''', '''        risk_map = _validated_risk_map(item.get("risk_weekly_returns"))
        item["risk_history_eligible"] = risk_map is not None
        if risk_map is None:
            item["activation"] = 0.0''')
replace('''    if vol is None or es is None:
        return False
    return (
        _sf(vol) <= PORTFOLIO_VOL_TARGET[risk] * RISK_TOLERANCE
        and _sf(es) <= PORTFOLIO_WEEKLY_ES95_TARGET[risk] * RISK_TOLERANCE
    )''', '''    if isinstance(vol, bool) or isinstance(es, bool):
        return False
    vol = _sf(vol, math.nan)
    es = _sf(es, math.nan)
    observations = _sf(signature.get("weekly_observations_3y"), math.nan)
    if (
        not math.isfinite(vol)
        or not math.isfinite(es)
        or vol < 0.0
        or es < 0.0
        or not math.isfinite(observations)
        or observations < MIN_RISK_HISTORY_WEEKS
    ):
        return False
    return (
        vol <= PORTFOLIO_VOL_TARGET[risk] * RISK_TOLERANCE
        and es <= PORTFOLIO_WEEKLY_ES95_TARGET[risk] * RISK_TOLERANCE
    )''')
replace('''    pre = risk_signature(weighted)
    if within_risk_target(pre, risk):''', '''    pre = risk_signature(weighted)
    if pre["annualized_volatility_3y"] is None:
        # Do not search for an infinitesimal weight that makes missing history
        # disappear below EPS. Unknown risk cannot be cured by scaling.
        return [], {
            "risk_scale": 0.0,
            "pre": pre,
            "post": risk_signature([]),
            "blocked_reason": "insufficient_aligned_risk_history",
        }
    if within_risk_target(pre, risk):''')
replace('''    whole_categories = _whole_unit_category_set(whole_unit_categories)
    candidate_rows = [dict(item) for item in candidates]
    weighted_rows = list(weighted)''', '''    whole_categories = _whole_unit_category_set(whole_unit_categories)
    candidate_rows = [dict(item) for item in candidates]
    weighted_rows = list(weighted)
    approved_keys = {
        _identity(item) for item, weight in weighted_rows if _sf(weight) > EPS
    }''')
replace('''    for item in projected:
        item["allocation_weight_validated"] = (
            _sf(item.get("suggested_amount")) / budget if budget > 0 else 0.0
        )''', '''    for item in projected:
        if _sf(item.get("suggested_amount")) > 0.0 and _identity(item) not in approved_keys:
            raise AssertionError("Projection reintroduced a rejected candidate")
        item["allocation_weight_validated"] = (
            _sf(item.get("suggested_amount")) / budget if budget > 0 else 0.0
        )''')
replace('''        **projection,
        "post_discrete_risk": discrete_risk,''', '''        **projection,
        "safety_contract": SAFETY_CONTRACT_VERSION,
        "post_discrete_risk": discrete_risk,''')
replace('''            ceiling = max(0.0, _sf(item.get("suggested_amount"))) if has_budget else None
            if has_budget and ceiling is not None and ceiling <= EPS and action == "consider":''', '''            ceiling = max(0.0, _sf(item.get("suggested_amount"))) if has_budget else None
            if item.get("allocation_eligible") is False:
                ceiling = 0.0 if has_budget else None
                if action == "consider":
                    action = "watch"
            if has_budget and ceiling is not None and ceiling <= EPS and action == "consider":''')
replace('    except (TypeError, ValueError):\n        return default', '    except (TypeError, ValueError, OverflowError):\n        return default')
assert hashlib.sha256(c.encode()).hexdigest() == '6208be3c86b6a5abd06c78560bbbe93611c045977923ccc1b89ece52dbd59cea', 'Local and staged patch differ'
p.write_text(c)
print('SOURCE_PATCH_MATCHES_LOCAL_VALIDATION')
