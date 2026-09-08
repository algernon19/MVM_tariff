"""The MVM Tariff Cost integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)

from .const import DATA_SOURCE_POWER_SENSORS, DOMAIN
from .coordinator import MvmTariffCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MVM Tariff Cost from a config entry."""
    coordinator = MvmTariffCoordinator(hass, entry)
    await coordinator.async_load()

    if coordinator.data_source == DATA_SOURCE_POWER_SENSORS:
        coordinator.async_setup_power_sensors()
        entry.async_on_unload(
            async_track_time_interval(
                hass, coordinator.async_persist_power_accumulators, timedelta(minutes=5)
            )
        )
    else:
        await coordinator.async_setup_mqtt()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Cost the previous hour once per hour, on the wall-clock boundary.
    entry.async_on_unload(
        async_track_time_change(
            hass, coordinator.async_process_hour, minute=0, second=10
        )
    )

    # Refresh the "current D price" + forecast sensors every 15 minutes.
    async def _refresh_current(_now=None) -> None:
        await coordinator.async_refresh_current_d()

    hass.async_create_task(_refresh_current())
    entry.async_on_unload(
        async_track_time_interval(hass, _refresh_current, timedelta(minutes=15))
    )

    # Reload on options change (e.g. switching data source) so the new
    # MQTT/power-sensor subscriptions actually take effect.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: MvmTariffCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_unsub_mqtt()
        coordinator.async_unsub_power_sensors()
    return unload_ok
