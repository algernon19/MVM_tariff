"""Config flow for the MVM Tariff Cost integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_ALLOWANCE_PERIOD,
    CONF_ANNUAL_THRESHOLD,
    CONF_D_DISTRIBUTION_FEE,
    CONF_D_ENABLED,
    CONF_D_EUR_HUF,
    CONF_D_MERCHANT_FEE,
    CONF_D_TRANSMISSION_FEE,
    CONF_D_VAT_PERCENT,
    CONF_DATA_SOURCE,
    CONF_EXPORT_POWER_ENTITY,
    CONF_IMPORT_POWER_ENTITY,
    CONF_MQTT_ROOT_TOPIC,
    CONF_PRICE_HIGH,
    CONF_PRICE_LOW,
    DATA_SOURCE_MQTT,
    DATA_SOURCE_POWER_SENSORS,
    DEFAULT_ALLOWANCE_PERIOD,
    DEFAULT_ANNUAL_THRESHOLD,
    DEFAULT_D_DISTRIBUTION_FEE,
    DEFAULT_D_ENABLED,
    DEFAULT_D_EUR_HUF,
    DEFAULT_D_MERCHANT_FEE,
    DEFAULT_D_TRANSMISSION_FEE,
    DEFAULT_D_VAT_PERCENT,
    DEFAULT_DATA_SOURCE,
    DEFAULT_MQTT_ROOT_TOPIC,
    DEFAULT_PRICE_HIGH,
    DEFAULT_PRICE_LOW,
    DOMAIN,
)

try:
    from homeassistant.helpers.selector import (
        EntitySelector,
        EntitySelectorConfig,
        SelectSelector,
        SelectSelectorConfig,
    )
except ImportError:  # pragma: no cover - very old HA core
    SelectSelector = None  # type: ignore[assignment,misc]
    EntitySelector = None  # type: ignore[assignment,misc]
    EntitySelectorConfig = None  # type: ignore[assignment,misc]


def _data_source_selector():
    return (
        SelectSelector(
            SelectSelectorConfig(
                options=[DATA_SOURCE_MQTT, DATA_SOURCE_POWER_SENSORS],
                translation_key="data_source",
            )
        )
        if SelectSelector is not None
        else vol.In([DATA_SOURCE_MQTT, DATA_SOURCE_POWER_SENSORS])
    )


def _power_entity_selector():
    return (
        EntitySelector(EntitySelectorConfig(domain="sensor"))
        if EntitySelector is not None
        else str
    )


class MvmTariffConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MVM Tariff Cost."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if user_input[CONF_DATA_SOURCE] == DATA_SOURCE_POWER_SENSORS:
                return await self.async_step_power_sensors()
            return await self.async_step_mqtt()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DATA_SOURCE, default=DEFAULT_DATA_SOURCE
                ): _data_source_selector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="MVM Tarifa",
                data={
                    CONF_DATA_SOURCE: DATA_SOURCE_MQTT,
                    CONF_MQTT_ROOT_TOPIC: user_input[CONF_MQTT_ROOT_TOPIC].strip()
                    or DEFAULT_MQTT_ROOT_TOPIC,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MQTT_ROOT_TOPIC, default=DEFAULT_MQTT_ROOT_TOPIC
                ): str,
            }
        )
        return self.async_show_form(step_id="mqtt", data_schema=schema)

    async def async_step_power_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="MVM Tarifa",
                data={
                    CONF_DATA_SOURCE: DATA_SOURCE_POWER_SENSORS,
                    CONF_IMPORT_POWER_ENTITY: user_input[CONF_IMPORT_POWER_ENTITY],
                    CONF_EXPORT_POWER_ENTITY: user_input.get(CONF_EXPORT_POWER_ENTITY),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_IMPORT_POWER_ENTITY): _power_entity_selector(),
                vol.Optional(CONF_EXPORT_POWER_ENTITY): _power_entity_selector(),
            }
        )
        return self.async_show_form(step_id="power_sensors", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "MvmTariffOptionsFlow":
        return MvmTariffOptionsFlow()


class MvmTariffOptionsFlow(config_entries.OptionsFlow):
    """Pricing and D-tariff settings, reachable via the integration's Configure button."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["data_source", "pricing", "d_tariff"]
        )

    async def async_step_data_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            data = {
                **self.config_entry.options,
                CONF_DATA_SOURCE: user_input[CONF_DATA_SOURCE],
            }
            if user_input[CONF_DATA_SOURCE] == DATA_SOURCE_MQTT:
                data[CONF_MQTT_ROOT_TOPIC] = (
                    user_input.get(CONF_MQTT_ROOT_TOPIC, "").strip()
                    or DEFAULT_MQTT_ROOT_TOPIC
                )
            else:
                data[CONF_IMPORT_POWER_ENTITY] = user_input.get(
                    CONF_IMPORT_POWER_ENTITY
                )
                data[CONF_EXPORT_POWER_ENTITY] = user_input.get(
                    CONF_EXPORT_POWER_ENTITY
                )
            return self.async_create_entry(title="", data=data)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DATA_SOURCE,
                    default=current.get(CONF_DATA_SOURCE, DEFAULT_DATA_SOURCE),
                ): _data_source_selector(),
                vol.Optional(
                    CONF_MQTT_ROOT_TOPIC,
                    default=current.get(
                        CONF_MQTT_ROOT_TOPIC, DEFAULT_MQTT_ROOT_TOPIC
                    ),
                ): str,
                vol.Optional(
                    CONF_IMPORT_POWER_ENTITY,
                    description={
                        "suggested_value": current.get(CONF_IMPORT_POWER_ENTITY)
                    },
                ): _power_entity_selector(),
                vol.Optional(
                    CONF_EXPORT_POWER_ENTITY,
                    description={
                        "suggested_value": current.get(CONF_EXPORT_POWER_ENTITY)
                    },
                ): _power_entity_selector(),
            }
        )
        return self.async_show_form(step_id="data_source", data_schema=schema)

    async def async_step_pricing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_PRICE_LOW: user_input[CONF_PRICE_LOW],
                    CONF_PRICE_HIGH: user_input[CONF_PRICE_HIGH],
                    CONF_ANNUAL_THRESHOLD: user_input[CONF_ANNUAL_THRESHOLD],
                    CONF_ALLOWANCE_PERIOD: user_input[CONF_ALLOWANCE_PERIOD],
                },
            )

        period_selector = (
            SelectSelector(
                SelectSelectorConfig(
                    options=["monthly", "yearly"],
                    translation_key="allowance_period",
                )
            )
            if SelectSelector is not None
            else vol.In(["monthly", "yearly"])
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PRICE_LOW,
                    default=current.get(CONF_PRICE_LOW, DEFAULT_PRICE_LOW),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_PRICE_HIGH,
                    default=current.get(CONF_PRICE_HIGH, DEFAULT_PRICE_HIGH),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_ANNUAL_THRESHOLD,
                    default=current.get(
                        CONF_ANNUAL_THRESHOLD, DEFAULT_ANNUAL_THRESHOLD
                    ),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_ALLOWANCE_PERIOD,
                    default=current.get(
                        CONF_ALLOWANCE_PERIOD, DEFAULT_ALLOWANCE_PERIOD
                    ),
                ): period_selector,
            }
        )
        return self.async_show_form(step_id="pricing", data_schema=schema)

    async def async_step_d_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_D_ENABLED: user_input[CONF_D_ENABLED],
                    CONF_D_MERCHANT_FEE: user_input[CONF_D_MERCHANT_FEE],
                    CONF_D_TRANSMISSION_FEE: user_input[CONF_D_TRANSMISSION_FEE],
                    CONF_D_DISTRIBUTION_FEE: user_input[CONF_D_DISTRIBUTION_FEE],
                    CONF_D_VAT_PERCENT: user_input[CONF_D_VAT_PERCENT],
                    CONF_D_EUR_HUF: user_input[CONF_D_EUR_HUF],
                },
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_D_ENABLED,
                    default=current.get(CONF_D_ENABLED, DEFAULT_D_ENABLED),
                ): bool,
                vol.Required(
                    CONF_D_MERCHANT_FEE,
                    default=current.get(CONF_D_MERCHANT_FEE, DEFAULT_D_MERCHANT_FEE),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_D_TRANSMISSION_FEE,
                    default=current.get(
                        CONF_D_TRANSMISSION_FEE, DEFAULT_D_TRANSMISSION_FEE
                    ),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_D_DISTRIBUTION_FEE,
                    default=current.get(
                        CONF_D_DISTRIBUTION_FEE, DEFAULT_D_DISTRIBUTION_FEE
                    ),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_D_VAT_PERCENT,
                    default=current.get(CONF_D_VAT_PERCENT, DEFAULT_D_VAT_PERCENT),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_D_EUR_HUF,
                    default=current.get(CONF_D_EUR_HUF, DEFAULT_D_EUR_HUF),
                ): vol.Coerce(float),
            }
        )
        return self.async_show_form(step_id="d_tariff", data_schema=schema)
