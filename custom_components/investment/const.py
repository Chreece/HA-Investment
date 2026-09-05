"""Constants for HA Investment."""
from __future__ import annotations

DOMAIN = "investment"
NAME = "HA Investment"
VERSION = "0.4.0"
PANEL_ASSET_REVISION = "0.4.0-r23"
PANEL_URL = "investment"
PANEL_NAME = "investment-panel"
PANEL_TITLE = "Investments"
PANEL_ICON = "mdi:chart-donut"
STATIC_URL = "/api/investment/static"
STORE_KEY = "investment.portfolios"
STORE_VERSION = 1
DEFAULT_BASE_CURRENCY = "EUR"
DEFAULT_UI_LANGUAGE = "auto"
SUPPORTED_UI_LANGUAGES = (
    "en", "de", "el", "fr", "es", "it", "pt", "nl", "pl", "tr", "ru", "uk", "cs",
    "hu", "ro", "sv", "da", "fi", "no", "ja", "ko", "zh", "ar", "bg", "sk", "he", "hi", "id",
)
DEFAULT_QUOTE_CACHE_SECONDS = 60
DEFAULT_SEARCH_CACHE_SECONDS = 300
DEFAULT_HISTORY_CACHE_SECONDS = 900

CONF_TWELVE_DATA_API_KEY = "twelve_data_api_key"
CONF_ALPHA_VANTAGE_API_KEY = "alpha_vantage_api_key"
CONF_ALPHA_VANTAGE_ENTITLEMENT = "alpha_vantage_entitlement"
DEFAULT_ALPHA_VANTAGE_ENTITLEMENT = "default"
ALPHA_VANTAGE_ENTITLEMENTS = ("default", "realtime", "delayed")
DEFAULT_INCOGNITO_REVEAL_SECONDS = 5
MAX_INCOGNITO_REVEAL_SECONDS = 300
INDICATION_DISCLAIMER_VERSION = 2
INDICATION_LEGAL_REGIONS = ("germany", "eu_eea", "uk", "switzerland", "us", "canada", "australia_nz", "other")
DEFAULT_INDICATION_LEGAL_REGION = "other"
SUPPORTED_PERIODS = ("1d", "7d", "1m", "3m", "1y", "5y")
CATEGORIES = ("crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other")
TRANSACTION_COST_TYPES = ("platform", "bank", "exchange", "tax", "other")

EXPOSABLE_ENTITY_METRICS = (
    "portfolio_value",
    "today_change",
    "today_change_percent",
    "invested_principal",
    "current_cost_basis",
    "other_costs",
    "asset_fees",
    "total_pnl",
    "total_pnl_percent",
    "holding_count",
)

DEFAULT_INDICATION_PREFERENCES = {
    "scope": "discover",
    "mode": "deterministic",
    "amount": None,
    "category": None,
    "ai_task_entity_id": None,
    "risk_tolerance": "medium",
    "horizon": "medium",
    "strategy": "adaptive",
    "overlap_policy": "penalize",
    "overlap_threshold_pct": 20.0,
    "diversification": "medium",
    "max_candidate_pct": None,
    "min_confidence_pct": 45.0,
    "min_cash_reserve_pct": 0.0,
    "whole_units_only": False,
}

SIGNAL_ENTITY_EXPOSURE_CHANGED = f"{DOMAIN}_entity_exposure_changed"

SIDEBAR_TITLES = {
    "en": "Investments", "de": "Investments", "el": "Επενδύσεις", "fr": "Investissements",
    "es": "Inversiones", "it": "Investimenti", "pt": "Investimentos", "nl": "Beleggingen",
    "pl": "Inwestycje", "tr": "Yatırımlar", "ru": "Инвестиции", "uk": "Інвестиції",
    "cs": "Investice", "sk": "Investície", "hu": "Befektetések", "ro": "Investiții",
    "bg": "Инвестиции", "sv": "Investeringar", "da": "Investeringer", "fi": "Sijoitukset",
    "no": "Investeringer", "ja": "投資", "ko": "투자", "zh": "投资", "zh-Hans": "投资",
    "ar": "الاستثمارات", "he": "השקעות", "hi": "निवेश", "id": "Investasi",
}

def sidebar_title(language: str | None) -> str:
    """Return a localized global sidebar title for the HA system language."""
    language = language or "en"
    if language in SIDEBAR_TITLES:
        return SIDEBAR_TITLES[language]
    base = language.split("-")[0]
    return SIDEBAR_TITLES.get(base, PANEL_TITLE)
