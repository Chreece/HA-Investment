"""Config flow for HA Investment."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ALPHA_VANTAGE_ENTITLEMENTS,
    CONF_ALPHA_VANTAGE_API_KEY,
    CONF_ALPHA_VANTAGE_ENTITLEMENT,
    CONF_TWELVE_DATA_API_KEY,
    DEFAULT_ALPHA_VANTAGE_ENTITLEMENT,
    DOMAIN,
    NAME,
)


class InvestmentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return InvestmentOptionsFlow()


class InvestmentOptionsFlow(config_entries.OptionsFlowWithReload):
    """Optional commercial/API-key market-data settings."""

    async def async_step_init(self, user_input=None):
        current = dict(self.config_entry.options)
        if user_input is not None:
            options = {
                CONF_ALPHA_VANTAGE_ENTITLEMENT: str(
                    user_input.get(
                        CONF_ALPHA_VANTAGE_ENTITLEMENT,
                        DEFAULT_ALPHA_VANTAGE_ENTITLEMENT,
                    )
                )
            }
            for key in (CONF_TWELVE_DATA_API_KEY, CONF_ALPHA_VANTAGE_API_KEY):
                value = str(user_input.get(key) or "").strip()
                if value:
                    options[key] = value
            return self.async_create_entry(title="", data=options)

        password_selector = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_TWELVE_DATA_API_KEY): password_selector,
                vol.Optional(CONF_ALPHA_VANTAGE_API_KEY): password_selector,
                vol.Optional(
                    CONF_ALPHA_VANTAGE_ENTITLEMENT,
                    default=DEFAULT_ALPHA_VANTAGE_ENTITLEMENT,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(ALPHA_VANTAGE_ENTITLEMENTS),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="alpha_vantage_entitlement",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
        )
