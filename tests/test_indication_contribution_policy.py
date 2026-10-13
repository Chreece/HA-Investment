from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"


def load_policy():
    name = "investment_indication_policy_test"
    spec = spec_from_file_location(name, ROOT / "indication_policy.py")
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


policy_module = load_policy()
resolve = policy_module.resolve_indication_portfolio_policy


def test_visible_portfolio_toggle_is_exact_owned_filter_only():
    policy = resolve("use", "allow", "discover")
    assert policy.filter_owned is True
    assert policy.display_context == "use"
    assert policy.display_existing_policy == "exclude"
    assert policy.holdings_use == "exact_identity_filter_only"
    assert policy.risk_scope == "new_contribution_only"
    assert policy.complete_portfolio_enforcement is False


def test_include_owned_mode_does_not_enable_complete_portfolio_risk():
    policy = resolve("ignore", "allow", "search")
    assert policy.filter_owned is False
    assert policy.display_context == "ignore"
    assert policy.display_existing_policy == "allow"
    assert policy.core_portfolio_context == "ignore"
    assert policy.core_existing_policy == "allow"
    assert policy.core_overlap_policy == "allow"
    assert policy.core_existing_cash == 0.0


def test_legacy_new_only_preference_remains_an_alias():
    policy = resolve("ignore", "exclude", "search")
    assert policy.filter_owned is True
    assert policy.display_context == "use"
    assert policy.display_existing_policy == "exclude"


def test_current_portfolio_source_is_analyzed_without_filtering_it_away():
    policy = resolve("use", "exclude", "portfolio")
    assert policy.filter_owned is False
    assert policy.display_context == "use"
    assert policy.display_existing_policy == "allow"
    assert policy.core_portfolio_context == "ignore"


@pytest.mark.parametrize(
    "context,existing,scope",
    [
        ("bad", "allow", "discover"),
        ("use", "bad", "discover"),
        ("use", "allow", "bad"),
    ],
)
def test_invalid_policy_values_are_rejected(context, existing, scope):
    with pytest.raises(ValueError):
        resolve(context, existing, scope)


def test_runtime_never_forwards_existing_portfolio_to_purchase_gate():
    manager = (ROOT / "contribution_manager.py").read_text(encoding="utf-8")
    assert '"portfolio_context": policy.core_portfolio_context' in manager
    assert '"overlap_policy": policy.core_overlap_policy' in manager
    assert '"existing_cash": policy.core_existing_cash' in manager
    assert 'policy.risk_scope' in manager
    assert 'policy.complete_portfolio_enforcement' in manager
    assert 'portfolio_risk["scope"] = policy.risk_scope' in manager


def test_panel_exposes_one_owned_filter_and_hides_old_portfolio_constraints():
    panel = (ROOT / "www" / "investment-panel-contribution.js").read_text(encoding="utf-8")
    assert "Owned-investment filter" in panel
    assert "Φίλτρο επενδύσεων που ήδη κατέχω" in panel
    assert '"indication_new_only","indication_overlap","indication_overlap_threshold","indication_existing_cash"' in panel
    assert 'payload.overlap_policy="allow"' in panel
    assert 'payload.existing_cash=0' in panel
    assert 'portfolio_context:scope==="portfolio"?"use":(filter?"use":"ignore")' in panel


def test_contribution_panel_module_is_valid_javascript():
    node = shutil.which("node")
    assert node is not None
    source = (ROOT / "www" / "investment-panel-contribution.js").read_text(encoding="utf-8")
    result = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_integration_loads_contribution_manager_and_wrapper():
    init = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "from .contribution_manager import InvestmentManager" in init
    assert "investment-panel-contribution.js" in init


def test_defaults_use_owned_filter_but_not_overlap_or_existing_cash():
    const = (ROOT / "const.py").read_text(encoding="utf-8")
    assert '"portfolio_context": "use"' in const
    assert '"existing_instruments": "exclude"' in const
    assert '"overlap_policy": "allow"' in const
    assert '"overlap_threshold_pct": 0.0' in const
    assert '"existing_cash": 0.0' in const
