"""Sensor platform for MVM Tariff Cost."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .coordinator import MvmTariffCoordinator

_TIER_LABELS = {"kedvezmenyes": "kedvezményes", "piaci": "piaci"}
_CROSSOVER_LABELS = {"atlepve": "átlépve", "ismeretlen": "ismeretlen"}


@dataclass(frozen=True, kw_only=True)
class MvmSummarySensor:
    """Description of a sensor backed by coordinator.attributes / current_d."""

    key: str
    name: str
    icon: str
    unit: str | None = None
    currency_unit: bool = False
    source: str = "summary"  # "summary" -> attributes, "current" -> current_d


SUMMARY_SENSORS: tuple[MvmSummarySensor, ...] = (
    MvmSummarySensor(
        key="period_consumption",
        name="MVM Tarifa Időszaki fogyasztás",
        icon="mdi:counter",
        unit="kWh",
    ),
    MvmSummarySensor(
        key="period_allowance",
        name="MVM Tarifa Kedvezményes keret",
        icon="mdi:gauge-low",
        unit="kWh",
    ),
    MvmSummarySensor(
        key="allowance_remaining",
        name="MVM Tarifa Hátralévő kedvezményes keret",
        icon="mdi:gauge",
        unit="kWh",
    ),
    MvmSummarySensor(
        key="allowance_used_pct",
        name="MVM Tarifa Kedvezményes keret kihasználtság",
        icon="mdi:percent",
        unit="%",
    ),
    MvmSummarySensor(
        key="price_tier",
        name="MVM Tarifa Aktuális ársáv",
        icon="mdi:cash",
    ),
    MvmSummarySensor(
        key="tier_crossover_estimate",
        name="MVM Tarifa Becsült sávváltás",
        icon="mdi:calendar-alert",
    ),
    MvmSummarySensor(
        key="sum_a1",
        name="MVM Tarifa Összes költség (A1)",
        icon="mdi:cash-multiple",
        currency_unit=True,
    ),
    MvmSummarySensor(
        key="sum_d",
        name="MVM Tarifa Összes költség (D)",
        icon="mdi:cash-clock",
        currency_unit=True,
    ),
    MvmSummarySensor(
        key="d_price_avg",
        name="MVM Tarifa D tarifa havi átlagár",
        icon="mdi:cash-sync",
        unit="HUF/kWh",
    ),
    MvmSummarySensor(
        key="gross_huf_kwh",
        name="MVM Tarifa D tarifa aktuális ár",
        icon="mdi:cash-fast",
        unit="HUF/kWh",
        source="current",
    ),
    MvmSummarySensor(
        key="raw_huf_kwh",
        name="MVM Tarifa D tarifa HUPX nyers ár",
        icon="mdi:chart-line",
        unit="HUF/kWh",
        source="current",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the MVM Tariff Cost sensors."""
    coordinator: MvmTariffCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [MvmConsumptionSensor(entry, coordinator)]
    entities += [
        MvmSummarySensorEntity(entry, coordinator, desc) for desc in SUMMARY_SENSORS
    ]
    async_add_entities(entities)


class _MvmBaseSensor(SensorEntity):
    """Shared wiring: refresh on the coordinator's update signal."""

    _attr_should_poll = False

    def __init__(self, coordinator: MvmTariffCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class MvmConsumptionSensor(_MvmBaseSensor):
    """The live P1 consumption (low + high tariff registers), from MQTT."""

    _attr_name = "MVM Tarifa Fogyasztás"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, entry: ConfigEntry, coordinator: MvmTariffCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_consumption"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.total_kwh

    @property
    def available(self) -> bool:
        return self._coordinator.total_kwh is not None


class MvmSummarySensorEntity(_MvmBaseSensor):
    """One figure from the running allowance/cost summary or the current D price."""

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: MvmTariffCoordinator,
        description: MvmSummarySensor,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        unique_suffix = description.key
        if description.source == "current":
            unique_suffix = f"current_{description.key}"
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"

    @property
    def _data(self) -> dict:
        if self._description.source == "current":
            return self._coordinator.current_d
        return self._coordinator.attributes

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._description.currency_unit:
            return self.hass.config.currency
        return self._description.unit

    @property
    def native_value(self) -> object:
        value = self._data.get(self._description.key)
        if self._description.key == "price_tier":
            return _TIER_LABELS.get(value, value)
        if self._description.key == "tier_crossover_estimate":
            return _CROSSOVER_LABELS.get(value, value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        if self._description.source == "current":
            data = self._coordinator.current_d
            return {
                "slot_start": data.get("slot_start"),
                "hupx_eur_mwh": data.get("hupx_eur_mwh"),
                "eur_huf": data.get("eur_huf"),
                "forecast": data.get("forecast"),
            }
        attrs = self._coordinator.attributes
        return {"period": attrs.get("period")}
