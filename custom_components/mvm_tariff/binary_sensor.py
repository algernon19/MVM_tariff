"""Binary sensor platform for MVM Tariff Cost."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .coordinator import MvmTariffCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the MVM Tariff Cost binary sensors."""
    coordinator: MvmTariffCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MvmCheapPriceBinarySensor(entry, coordinator)])


class MvmCheapPriceBinarySensor(BinarySensorEntity):
    """On below the "turn on" price, off above the "turn off" price.

    Hysteresis band between the two thresholds holds the previous state, so a
    price hovering near one boundary doesn't chatter the sensor every 15 min.
    See MvmTariffCoordinator._update_cheap_state.
    """

    _attr_should_poll = False
    _attr_name = "MVM Tarifa Olcsó áram"
    _attr_icon = "mdi:cash-check"

    def __init__(self, entry: ConfigEntry, coordinator: MvmTariffCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{entry.entry_id}_cheap_price"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def _current_price(self) -> float | None:
        value = self._coordinator.current_d.get("gross_huf_kwh")
        return float(value) if value is not None else None

    @property
    def is_on(self) -> bool | None:
        return self._coordinator.cheap_on

    @property
    def available(self) -> bool:
        return self._coordinator.cheap_on is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "current_price_huf_kwh": self._current_price,
            "turn_on_below_huf_kwh": self._coordinator.cheap_price_on,
            "turn_off_above_huf_kwh": self._coordinator.cheap_price_off,
        }
