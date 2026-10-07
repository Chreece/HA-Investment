"""Search completeness is visible without claiming investment optimality."""
from copy import deepcopy

import pytest

from test_purchase_plan_ui import render, result


def with_search(status="exhaustive", *, complete=True, fractional=0):
    data = result()
    data["allocation"]["purchase_search"] = {
        "contract": "bounded-whole-unit-reductions-v1",
        "status": status,
        "optimality_proven": complete,
        "fractional_candidates": fractional,
        "global_optimality_claimed": False,
    }
    return data


@pytest.mark.parametrize("status", ["exhaustive", "upper_bound_verified"])
def test_complete_search_explains_approved_scope_without_predictive_claim(status):
    data = with_search(status)
    before = deepcopy(data)
    page, = render({"result": data})
    assert 'class="indication-purchase-search"' in page["html"]
    assert 'data-search-status="' + status + '"' in page["html"]
    assert page["translations"]["wholePurchaseSearchComplete"] in page["html"]
    assert page["translations"]["wholePurchaseSearchLimited"] not in page["html"]
    assert page["translations"]["wholePurchaseSearchFractional"] not in page["html"]
    assert page["inputUnchanged"] and data == before


def test_bounded_zero_plan_cannot_look_like_proved_infeasibility():
    data = with_search("bounded", complete=False)
    data["allocation"].update(deployed=0, cash_reserve=100)
    data["results"][0].update(suggested_amount=0, suggested_units=0)
    page, = render({"result": data})
    assert page["translations"]["wholePurchaseSearchLimited"] in page["html"]
    assert page["translations"]["wholePurchaseSearchComplete"] not in page["html"]


def test_inconsistent_search_metadata_cannot_claim_completed_search():
    data = with_search("bounded", complete=True)
    page, = render({"result": data})
    assert page["translations"]["wholePurchaseSearchLimited"] in page["html"]
    assert page["translations"]["wholePurchaseSearchComplete"] not in page["html"]


@pytest.mark.parametrize("risk_status", ["unknown", "blocked", "analysis_only"])
def test_invalidated_result_hides_previous_search_completeness(risk_status):
    data = with_search()
    data["allocation"]["portfolio_risk"]["status"] = risk_status
    page, = render({"result": data})
    assert 'class="indication-purchase-search"' not in page["html"]


@pytest.mark.parametrize("search_status", ["not_applicable", "blocked", "unrecognized"])
def test_nonsearch_status_has_no_completion_message(search_status):
    page, = render({"result": with_search(search_status)})
    assert 'class="indication-purchase-search"' not in page["html"]


def test_full_ai_displays_its_own_search_scope_and_fixed_fractional_note():
    data = with_search()
    data["mode"] = "full_ai"
    data["ai_allocation"] = deepcopy(data["allocation"])
    data["ai_allocation"]["purchase_search"].update(
        status="bounded", optimality_proven=False, fractional_candidates=1,
    )
    page, = render({"result": data})
    assert 'data-search-status="bounded"' in page["html"]
    assert page["translations"]["wholePurchaseSearchLimited"] in page["html"]
    assert page["translations"]["wholePurchaseSearchComplete"] not in page["html"]
    assert page["translations"]["wholePurchaseSearchFractional"] in page["html"]


def test_all_supported_languages_have_search_explanation_in_rendered_result():
    data = with_search("bounded", complete=False, fractional=1)
    first, = render({"result": data})
    pages = render(*({"language": language, "result": data} for language in first["supportedLanguages"]))
    assert len(pages) == 28
    for page in pages:
        assert page["ownTranslations"]
        for key in ("wholePurchaseSearchTitle", "wholePurchaseSearchLimited", "wholePurchaseSearchFractional"):
            assert page["translations"][key] in page["html"]
        assert page["translations"]["wholePurchaseSearchComplete"] not in page["html"]
        assert page["inputUnchanged"]
