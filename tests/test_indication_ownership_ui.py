"""Ownership uncertainty remains an understandable exclusion in both UI layers."""
import pytest

from test_indication_evidence_ui import analysis, candidate, render


@pytest.mark.parametrize("language, expected", [
    ("en", "Ownership could not be verified. Check this instrument’s details and transactions."),
    ("de", "Der Bestand konnte nicht bestätigt werden. Prüfe die Angaben und Transaktionen dieser Anlage."),
    ("el", "Δεν επιβεβαιώθηκε αν κατέχεις την επένδυση. Έλεγξε τα στοιχεία και τις συναλλαγές της."),
])
def test_uncertain_ownership_exclusion_explains_the_required_record_check(language, expected):
    excluded = {
        "provider": "yahoo", "provider_id": "UNCERTAIN.DE", "symbol": "UNCERTAIN.DE",
        "reason": "ownership_unverified",
        "ownership_evidence": {"status": "unknown", "reasons": ["position_ledger_oversell"]},
    }
    page, = render({"language": language, "result": analysis([candidate()], excluded=[excluded])})
    assert page["visible"] == ["EUNL.DE"]
    assert page["excluded"][0]["reason"] == expected
    assert "ownership_unverified" not in page["html"]
    assert "position_ledger_oversell" not in page["html"]
    assert page["inputUnchanged"]


def test_ownership_exclusions_have_readable_copy_in_every_supported_language():
    initial, = render({"result": analysis([candidate()])})
    excluded = {"symbol": "UNKNOWN", "reason": "ownership_unverified"}
    pages = render(*[
        {"language": language, "result": analysis([candidate()], excluded=[excluded])}
        for language in initial["supportedLanguages"]
    ])
    assert len(pages) == len(initial["supportedLanguages"])
    for page in pages:
        reason = page["excluded"][0]["reason"]
        assert len(reason) > 12
        assert "ownership_unverified" not in reason
        assert "ownershipUnverified" not in reason
        assert page["inputUnchanged"]
