"""Core coordinator: MQTT-fed P1 consumption -> tiered A1/D tariff cost.

Design, deliberately kept simple:

* The two P1 registers (low/high tariff, Wh) arrive over MQTT and their sum
  is the live total consumption (kWh) - exposed directly as a normal energy
  sensor, so Home Assistant's own recorder handles its long-term statistics
  and it can be used on the Energy dashboard like any other meter.
* Once an hour (at the wall-clock hour boundary), the coordinator looks at
  how much that total grew since the previous boundary and prices that one
  hour's kWh with the tiered A1 formula (chronological allowance fill, exactly
  how an A1 invoice tiers), appending one new row to the A1 cost statistic.
* The "D" (dynamic) tariff is billed differently by MVM: every 15 minutes the
  consumption delta is priced at that quarter-hour's HUPX-based gross price,
  and two month-to-date totals are kept - Sum(kWh) and Sum(kWh * price). The
  D energy cost is then

      min(month_kWh, allowance) * A1_kedvezmenyes_ar
      + max(0, month_kWh - allowance) * (Sum(kWh*price) / month_kWh)

  i.e. the part above the allowance is charged at the *consumption-weighted
  average* of the whole month's quarter-hourly prices, matching MVM's
  "ugyfelenkent egyedi egysegar" method. The D cost statistic still gets one
  hourly row; its cumulative sum self-corrects as the running average drifts,
  and each month's final figure is frozen at the month boundary.
* A separate 15-minute timer also refreshes the "current D price" sensors
  and their forecast; it does not touch the recorder.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

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
    CONF_EXPORT_ENERGY_REFERENCE,
    CONF_EXPORT_POWER_ENTITY,
    CONF_IMPORT_ENERGY_REFERENCE,
    CONF_IMPORT_POWER_ENTITY,
    CONF_MQTT_ROOT_TOPIC,
    CONF_PRICE_HIGH,
    CONF_PRICE_LOW,
    COST_D_STATISTIC_ID,
    COST_D_STATISTIC_NAME,
    COST_STATISTIC_ID,
    COST_STATISTIC_NAME,
    D_PRICE_STORAGE_KEY,
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
    SIGNAL_UPDATE,
    STORAGE_KEY,
    TIME_ZONE,
    TOPIC_HIGH,
    TOPIC_LOW,
)
from .dynamic import (
    DTariffConfig,
    async_current_d_price,
    async_d_gross_prices,
    async_d_price_forecast,
)

_LOGGER = logging.getLogger(__name__)
BUDAPEST_TZ = ZoneInfo(TIME_ZONE)

# A household import/export reading past this is almost certainly a
# misconfigured sensor (e.g. a cumulative kWh total picked instead of an
# instantaneous kW reading) rather than a real power spike.
MAX_PLAUSIBLE_POWER_KW = 500.0

try:  # Home Assistant >= 2025.x exposes StatisticMeanType, replacing has_mean.
    from homeassistant.components.recorder.models import StatisticMeanType

    _HAS_MEAN_TYPE = True
except ImportError:  # pragma: no cover - older HA core
    _HAS_MEAN_TYPE = False


def _period_bucket(
    local: datetime, base_threshold: float, period: str
) -> tuple[str, float]:
    """(accounting-bucket key, allowance for that bucket) for a timestamp.

    "monthly": the yearly allowance split by day count into each month (what
    a monthly invoice shows); "yearly": one bucket per calendar year.
    """
    if period == "monthly":
        year, month = local.year, local.month
        days_in_year = 366 if calendar.isleap(year) else 365
        days_in_month = calendar.monthrange(year, month)[1]
        return (
            f"{year}-{month:02d}",
            round(base_threshold * days_in_month / days_in_year, 1),
        )
    return str(local.year), base_threshold


def _push_cost_row(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    currency: str,
    start: datetime,
    state: float,
    running_sum: float,
) -> None:
    """Append one new hourly row to a cost statistic (upsert, no history rewrite)."""
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    metadata = {
        "statistic_id": statistic_id,
        "source": statistic_id.split(":", 1)[0],
        "name": name,
        "unit_of_measurement": currency,
        "has_sum": True,
        "unit_class": None,
    }
    if _HAS_MEAN_TYPE:
        metadata["mean_type"] = StatisticMeanType.NONE
    else:  # pragma: no cover - older HA core
        metadata["has_mean"] = False

    async_add_external_statistics(
        hass, metadata, [{"start": start, "state": state, "sum": running_sum}]
    )


class MvmTariffCoordinator:
    """Owns the MQTT subscription, the on-disk running state and the pricing."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, 1, f"{STORAGE_KEY}_{entry.entry_id}")
        self.state: dict[str, object] = {}
        self.attributes: dict[str, object] = {}
        self.current_d: dict[str, object] = {}
        self._unsub_mqtt: list = []
        self._latest_low: float | None = None
        self._latest_high: float | None = None

        # -- power-sensor data source (trapezoidal kW -> kWh integration) --
        self._unsub_power: list = []
        self._power_last_value: dict[str, float] = {}
        self._power_last_time: dict[str, datetime] = {}
        self._power_import_kwh: float = 0.0
        self._power_export_kwh: float = 0.0
        self._power_warned: set[str] = set()

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="MVM Tarifa",
            manufacturer="MVM",
            model="P1 (MQTT) + A1/D tarifa kalkulátor",
        )

    # -- settings ------------------------------------------------------
    def _opt(self, key: str, default):
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def data_source(self) -> str:
        return str(self._opt(CONF_DATA_SOURCE, DEFAULT_DATA_SOURCE))

    @property
    def import_power_entity(self) -> str | None:
        return self._opt(CONF_IMPORT_POWER_ENTITY, None)

    @property
    def export_power_entity(self) -> str | None:
        return self._opt(CONF_EXPORT_POWER_ENTITY, None)

    @property
    def import_energy_reference_entity(self) -> str | None:
        return self._opt(CONF_IMPORT_ENERGY_REFERENCE, None)

    @property
    def export_energy_reference_entity(self) -> str | None:
        return self._opt(CONF_EXPORT_ENERGY_REFERENCE, None)

    @property
    def mqtt_root_topic(self) -> str:
        return str(self._opt(CONF_MQTT_ROOT_TOPIC, DEFAULT_MQTT_ROOT_TOPIC)).rstrip("/")

    @property
    def price_low(self) -> float:
        return float(self._opt(CONF_PRICE_LOW, DEFAULT_PRICE_LOW))

    @property
    def price_high(self) -> float:
        return float(self._opt(CONF_PRICE_HIGH, DEFAULT_PRICE_HIGH))

    @property
    def annual_threshold(self) -> float:
        return float(self._opt(CONF_ANNUAL_THRESHOLD, DEFAULT_ANNUAL_THRESHOLD))

    @property
    def allowance_period(self) -> str:
        value = str(self._opt(CONF_ALLOWANCE_PERIOD, DEFAULT_ALLOWANCE_PERIOD))
        return value if value in ("monthly", "yearly") else DEFAULT_ALLOWANCE_PERIOD

    @property
    def d_enabled(self) -> bool:
        return bool(self._opt(CONF_D_ENABLED, DEFAULT_D_ENABLED))

    @property
    def d_config(self) -> DTariffConfig:
        return DTariffConfig(
            merchant_fee=float(self._opt(CONF_D_MERCHANT_FEE, DEFAULT_D_MERCHANT_FEE)),
            transmission_fee=float(
                self._opt(CONF_D_TRANSMISSION_FEE, DEFAULT_D_TRANSMISSION_FEE)
            ),
            distribution_fee=float(
                self._opt(CONF_D_DISTRIBUTION_FEE, DEFAULT_D_DISTRIBUTION_FEE)
            ),
            vat_percent=float(self._opt(CONF_D_VAT_PERCENT, DEFAULT_D_VAT_PERCENT)),
            eur_huf=float(self._opt(CONF_D_EUR_HUF, DEFAULT_D_EUR_HUF)),
        )

    @property
    def total_kwh(self) -> float | None:
        """Live cumulative *net* consumption, in kWh.

        MQTT mode: sum of the low + high tariff registers. Power-sensor mode:
        the running import-minus-export integral (see `_integrate_power`).
        """
        if self.data_source == DATA_SOURCE_POWER_SENSORS:
            if not self.import_power_entity and not self.export_power_entity:
                return None
            return round(self._power_import_kwh - self._power_export_kwh, 3)
        if self._latest_low is None or self._latest_high is None:
            return None
        return round((self._latest_low + self._latest_high) / 1000.0, 3)

    # -- persistence -----------------------------------------------------
    async def async_load(self) -> None:
        self.state = await self._store.async_load() or {}
        self._power_import_kwh = float(self.state.get("power_import_kwh", 0.0))
        self._power_export_kwh = float(self.state.get("power_export_kwh", 0.0))

        # Restore the last computed sensor values so a restart shows them
        # immediately instead of "Ismeretlen" until the next hourly recompute.
        stored_attrs = self.state.get("attributes")
        if isinstance(stored_attrs, dict):
            self.attributes = dict(stored_attrs)
        stored_current_d = self.state.get("current_d")
        if isinstance(stored_current_d, dict):
            self.current_d = dict(stored_current_d)

        # Migration: the pre-weighted-average D accounting kept a running
        # "sum_d" from a different (hourly simple-average) formula. Drop it so
        # the new 15-minute weighted method starts its cumulative from zero;
        # its cost statistic should be cleared once in Developer Tools too.
        if "d" not in self.state and "sum_d" in self.state:
            _LOGGER.info(
                "MVM Tarifa: régi D tarifa elszámolás törölve, az új súlyozott "
                "átlag módszer nulláról indul (a D költség statisztikát is "
                "érdemes egyszer törölni)"
            )
            self.state.pop("sum_d", None)
            self.state.pop("last_hour_d_cost", None)

    async def _async_save(self) -> None:
        await self._store.async_save(self.state)

    # -- MQTT --------------------------------------------------------------
    async def async_setup_mqtt(self) -> None:
        root = self.mqtt_root_topic

        @callback
        def _on_low(msg) -> None:
            self._on_register("low", msg.payload)

        @callback
        def _on_high(msg) -> None:
            self._on_register("high", msg.payload)

        self._unsub_mqtt.append(
            await mqtt.async_subscribe(self.hass, f"{root}/{TOPIC_LOW}", _on_low)
        )
        self._unsub_mqtt.append(
            await mqtt.async_subscribe(self.hass, f"{root}/{TOPIC_HIGH}", _on_high)
        )
        _LOGGER.info(
            "MVM Tarifa: feliratkozva: %s/%s és %s/%s", root, TOPIC_LOW, root, TOPIC_HIGH
        )

    @callback
    def _on_register(self, field: str, payload: str) -> None:
        try:
            value = float(payload)
        except (TypeError, ValueError):
            _LOGGER.debug("MVM Tarifa: érvénytelen MQTT payload (%s): %r", field, payload)
            return
        if field == "low":
            self._latest_low = value
        else:
            self._latest_high = value
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def async_unsub_mqtt(self) -> None:
        for unsub in self._unsub_mqtt:
            unsub()
        self._unsub_mqtt = []

    # -- power-sensor data source --------------------------------------------
    @callback
    def async_setup_power_sensors(self) -> None:
        """Track the configured import/export power sensors and integrate them.

        Each sensor reports instantaneous kW; on every state change the
        elapsed time since the previous reading is multiplied by the average
        of the two kW values (trapezoidal rule) and added to that sensor's
        running kWh total. `total_kwh` above is import minus export.
        """
        from homeassistant.helpers.event import async_track_state_change_event

        entities = [
            e for e in (self.import_power_entity, self.export_power_entity) if e
        ]
        if not entities:
            _LOGGER.warning(
                "MVM Tarifa: nincs import/export teljesítmény-szenzor beállítva"
            )
            return

        self._seed_absolute_reference()

        @callback
        def _on_state(event) -> None:
            entity_id = event.data["entity_id"]
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            try:
                power_kw = float(new_state.state)
            except (TypeError, ValueError):
                return

            unit = new_state.attributes.get("unit_of_measurement")
            device_class = new_state.attributes.get("device_class")
            if unit == "W":
                power_kw /= 1000.0
            elif unit not in (None, "kW"):
                self._warn_bad_power_sensor(
                    entity_id,
                    "a mértékegysége (%s) nem teljesítmény (kW/W)" % unit,
                )
                return
            if device_class not in (None, "power"):
                self._warn_bad_power_sensor(
                    entity_id,
                    "az eszközosztálya (%s) nem 'power'" % device_class,
                )
                return
            if abs(power_kw) > MAX_PLAUSIBLE_POWER_KW:
                self._warn_bad_power_sensor(
                    entity_id,
                    "irreálisan nagy értéket adott (%.1f) - valószínűleg egy "
                    "összesített kWh-mérőállás lett tévedésből pillanatnyi "
                    "teljesítménynek megadva" % power_kw,
                )
                return

            self._integrate_power(entity_id, power_kw, new_state.last_updated)

        self._unsub_power.append(
            async_track_state_change_event(self.hass, entities, _on_state)
        )

        # Seed the last-known reading so the very first state change already
        # has a starting point to integrate from, instead of being dropped.
        now = datetime.now(timezone.utc)
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            try:
                self._power_last_value[entity_id] = float(state.state)
            except (TypeError, ValueError):
                continue
            self._power_last_time[entity_id] = now

        _LOGGER.info(
            "MVM Tarifa: teljesítmény-szenzorok figyelése: import=%s export=%s",
            self.import_power_entity,
            self.export_power_entity,
        )

    def _seed_absolute_reference(self) -> None:
        """Anchor the running total to the meter's own absolute reading.

        Runs once (tracked via "power_baseline_seeded" in storage): if an
        import/export *energy* reference entity is configured (the meter's
        real cumulative kWh total, e.g. "P1 Active Energy Import Total"),
        read its current value and start the running integral from there -
        so `total_kwh` reflects the actual meter reading instead of zero.
        Without a reference entity, the sensor simply starts counting from
        the moment the integration was (re)configured, as before.
        """
        if self.state.get("power_baseline_seeded"):
            return

        seeded = False
        if self.import_energy_reference_entity:
            state = self.hass.states.get(self.import_energy_reference_entity)
            if state is not None:
                try:
                    self._power_import_kwh = float(state.state)
                    seeded = True
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "MVM Tarifa: az import kezdő mérőállás (%s) nem "
                        "numerikus, kihagyva",
                        self.import_energy_reference_entity,
                    )
        if self.export_energy_reference_entity:
            state = self.hass.states.get(self.export_energy_reference_entity)
            if state is not None:
                try:
                    self._power_export_kwh = float(state.state)
                    seeded = True
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "MVM Tarifa: az export kezdő mérőállás (%s) nem "
                        "numerikus, kihagyva",
                        self.export_energy_reference_entity,
                    )

        if not seeded:
            return
        self.state["power_baseline_seeded"] = True
        self.state["power_import_kwh"] = round(self._power_import_kwh, 4)
        self.state["power_export_kwh"] = round(self._power_export_kwh, 4)
        self.hass.async_create_task(self._async_save())
        _LOGGER.info(
            "MVM Tarifa: kezdő mérőállás rögzítve a valós mérőről "
            "(import=%.3f kWh, export=%.3f kWh)",
            self._power_import_kwh,
            self._power_export_kwh,
        )

    def _warn_bad_power_sensor(self, entity_id: str, reason: str) -> None:
        if entity_id in self._power_warned:
            return
        self._power_warned.add(entity_id)
        _LOGGER.warning(
            "MVM Tarifa: a(z) %s szenzor %s - kihagyva, ellenőrizd az "
            "import/export teljesítmény-szenzor beállítást",
            entity_id,
            reason,
        )

    @callback
    def _integrate_power(
        self, entity_id: str, power_kw: float, timestamp: datetime
    ) -> None:
        last_value = self._power_last_value.get(entity_id)
        last_time = self._power_last_time.get(entity_id)
        self._power_last_value[entity_id] = power_kw
        self._power_last_time[entity_id] = timestamp

        if last_value is None or last_time is None:
            return
        elapsed_hours = (timestamp - last_time).total_seconds() / 3600.0
        if elapsed_hours <= 0:
            return

        energy_kwh = (last_value + power_kw) / 2.0 * elapsed_hours
        if entity_id == self.import_power_entity:
            self._power_import_kwh += energy_kwh
        elif entity_id == self.export_power_entity:
            self._power_export_kwh += energy_kwh
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def async_persist_power_accumulators(self, _now=None) -> None:
        """Periodic safety-net save, so a restart loses at most a few minutes.

        Unlike the MQTT registers (which report the meter's own absolute
        totals, so an unclean restart just resumes from the retained value),
        the power-sensor integral only exists in memory - it must be saved
        periodically to survive a restart.
        """
        if self.data_source != DATA_SOURCE_POWER_SENSORS:
            return
        self.state["power_import_kwh"] = round(self._power_import_kwh, 4)
        self.state["power_export_kwh"] = round(self._power_export_kwh, 4)
        await self._async_save()

    def async_unsub_power_sensors(self) -> None:
        for unsub in self._unsub_power:
            unsub()
        self._unsub_power = []

    # -- current D price + forecast (every 15 minutes) --------------------
    async def async_refresh_current_d(self) -> None:
        if not self.d_enabled:
            if self.current_d:
                self.current_d = {}
                async_dispatcher_send(self.hass, SIGNAL_UPDATE)
            return

        store_key = f"{D_PRICE_STORAGE_KEY}_{self.entry.entry_id}"
        try:
            data = await async_current_d_price(self.hass, store_key, self.d_config)
        except Exception:  # noqa: BLE001 - a timer callback must not raise
            _LOGGER.debug("MVM Tarifa: aktuális D ár lekérés hiba", exc_info=True)
            return
        if not data:
            return
        try:
            data["forecast"] = await async_d_price_forecast(
                self.hass, store_key, self.d_config
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("MVM Tarifa: D ár előrejelzés hiba", exc_info=True)
            data["forecast"] = self.current_d.get("forecast", [])
        self.current_d = data
        self.state["current_d"] = data
        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # -- hourly cost accounting --------------------------------------------
    async def async_process_hour(self, _now=None) -> None:
        """Cost the hour that just ended, from the P1 register delta."""
        current_kwh = self.total_kwh
        if current_kwh is None:
            _LOGGER.debug("MVM Tarifa: még nincs MQTT adat, óra kihagyva")
            return

        now_local = datetime.now(BUDAPEST_TZ)
        hour_start_local = now_local.replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=1)
        hour_start_utc = hour_start_local.astimezone(timezone.utc)

        baseline = self.state.get("baseline_kwh")
        if baseline is None:
            # First run: just anchor here - the first billable hour is the next one.
            self._save_baseline(current_kwh, hour_start_utc)
            await self._async_save()
            _LOGGER.info(
                "MVM Tarifa: kiinduló fogyasztás rögzítve (%.3f kWh), a következő "
                "órától kezd el számolni",
                current_kwh,
            )
            return

        kwh = round(current_kwh - baseline, 4)
        if kwh < 0:
            _LOGGER.warning(
                "MVM Tarifa: a fogyasztás csökkent (%.3f -> %.3f) - a mérő "
                "valószínűleg újraindult, az óra kimarad, új alapvonal rögzítve",
                baseline,
                current_kwh,
            )
            self._save_baseline(current_kwh, hour_start_utc)
            await self._async_save()
            return

        bucket_used: dict[str, float] = dict(self.state.get("bucket_used", {}))
        bucket_hours: dict[str, int] = dict(self.state.get("bucket_hours", {}))
        bucket_key, allowance = _period_bucket(
            hour_start_local, self.annual_threshold, self.allowance_period
        )
        used_so_far = bucket_used.get(bucket_key, 0.0)
        low_part = max(0.0, min(kwh, allowance - used_so_far))
        high_part = kwh - low_part

        a1_cost = round(low_part * self.price_low + high_part * self.price_high, 4)
        currency = self.hass.config.currency or "HUF"
        sum_a1 = round(float(self.state.get("sum_a1", 0.0)) + a1_cost, 4)
        _push_cost_row(
            self.hass,
            COST_STATISTIC_ID,
            COST_STATISTIC_NAME,
            currency,
            hour_start_utc,
            a1_cost,
            sum_a1,
        )

        bucket_used[bucket_key] = used_so_far + kwh
        bucket_hours[bucket_key] = bucket_hours.get(bucket_key, 0) + 1

        self.state.update(
            {
                "sum_a1": sum_a1,
                "bucket_used": bucket_used,
                "bucket_hours": bucket_hours,
                "last_hour_kwh": kwh,
                "last_hour_a1_cost": a1_cost,
            }
        )
        self._save_baseline(current_kwh, hour_start_utc)

        self._update_summary(bucket_key, allowance, bucket_used[bucket_key], bucket_hours[bucket_key])
        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _save_baseline(self, kwh: float, hour_start_utc: datetime) -> None:
        self.state["baseline_kwh"] = kwh
        self.state["baseline_hour"] = hour_start_utc.isoformat()

    # -- D (dynamic) tariff: 15-minute, consumption-weighted monthly average --
    def _d_month_allowance(self, local: datetime) -> float:
        """The kedvezményes sávhatár for the calendar month `local` falls in.

        MVM prorates the yearly allowance (2523 kWh lakossági) by day count:
        6,91 kWh/day, so a full month is 6,91 × days-in-month. "yearly" mode
        keeps a single annual bucket instead.
        """
        if self.allowance_period == "yearly":
            return self.annual_threshold
        days_in_year = 366 if calendar.isleap(local.year) else 365
        days_in_month = calendar.monthrange(local.year, local.month)[1]
        return self.annual_threshold * days_in_month / days_in_year

    def _recompute_d_month_cost(self, d: dict, local: datetime) -> None:
        """Month-to-date D energy cost from the running weighted average."""
        month_kwh = float(d.get("month_kwh", 0.0))
        priced_kwh = float(d.get("month_priced_kwh", 0.0))
        allowance = self._d_month_allowance(local)
        if priced_kwh > 0:
            p_avg = float(d["month_weighted"]) / priced_kwh
        else:
            p_avg = self.price_high  # fallback until the first slot is priced
        below = min(month_kwh, allowance)
        above = max(0.0, month_kwh - allowance)
        d["allowance"] = round(allowance, 1)
        d["p_avg"] = round(p_avg, 4)
        d["month_cost"] = round(below * self.price_low + above * p_avg, 4)

    def _freeze_d_month(self, d: dict) -> None:
        """Roll the finished month's cost into the permanent cumulative total."""
        d["prior_sum"] = round(
            float(d.get("prior_sum", 0.0)) + float(d.get("month_cost", 0.0)), 4
        )
        _LOGGER.info(
            "MVM Tarifa: D tarifa – %s lezárva (%.0f Ft, havi átlagár %.2f Ft/kWh)",
            d.get("month_key"),
            float(d.get("month_cost", 0.0)),
            float(d.get("p_avg", 0.0)),
        )

    async def async_process_d_quarter(self, _now=None) -> None:
        """Every 15 minutes: price the just-finished quarter-hour and accrue it.

        Timer fires a minute past each boundary (:01/:16/:31/:46), so the
        HUPX price for the slot that just ended is already published, and the
        hourly A1 accounting (:00:10) has run first.
        """
        if not self.d_enabled:
            return
        current = self.total_kwh
        if current is None:
            return

        now_local = datetime.now(BUDAPEST_TZ)
        slot_end_local = now_local.replace(
            minute=(now_local.minute // 15) * 15, second=0, microsecond=0
        )
        slot_start_local = slot_end_local - timedelta(minutes=15)
        slot_start_utc = slot_start_local.astimezone(timezone.utc)

        d: dict = dict(self.state.get("d", {}))
        base = d.get("quarter_baseline_kwh")
        d["quarter_baseline_kwh"] = current
        if base is None:
            self.state["d"] = d
            await self._async_save()
            _LOGGER.info(
                "MVM Tarifa: D tarifa – negyedórás alapvonal rögzítve (%.3f kWh)",
                current,
            )
            return

        delta = round(current - float(base), 4)
        if delta < 0:
            _LOGGER.warning(
                "MVM Tarifa: D tarifa – a fogyasztás csökkent (%.3f -> %.3f), "
                "negyedóra kihagyva, új alapvonal",
                float(base),
                current,
            )
            self.state["d"] = d
            await self._async_save()
            return

        month_key = slot_start_local.strftime("%Y-%m")
        if d.get("month_key") != month_key:
            if d.get("month_key"):
                self._freeze_d_month(d)
            d["month_key"] = month_key
            d["month_kwh"] = 0.0
            d["month_weighted"] = 0.0
            d["month_priced_kwh"] = 0.0

        price = None
        try:
            prices, _meta = await async_d_gross_prices(
                self.hass,
                f"{D_PRICE_STORAGE_KEY}_{self.entry.entry_id}",
                [slot_start_utc.isoformat()],
                self.d_config,
            )
            price = prices.get(slot_start_utc.isoformat())
        except Exception:  # noqa: BLE001 - a timer callback must not raise
            _LOGGER.exception("MVM Tarifa: D tarifa negyedórás árlekérés hiba")

        d["month_kwh"] = round(float(d["month_kwh"]) + delta, 4)
        if price is not None:
            d["month_weighted"] = round(
                float(d["month_weighted"]) + delta * price, 4
            )
            d["month_priced_kwh"] = round(float(d["month_priced_kwh"]) + delta, 4)
        else:
            _LOGGER.debug(
                "MVM Tarifa: D tarifa – nincs ár ehhez a negyedórához (%s), "
                "a súlyozott átlagból kimarad",
                slot_start_utc,
            )

        self._recompute_d_month_cost(d, slot_start_local)

        if slot_end_local.minute == 0:
            cumulative = round(
                float(d.get("prior_sum", 0.0)) + float(d["month_cost"]), 4
            )
            hour_start_utc = (slot_end_local - timedelta(hours=1)).astimezone(
                timezone.utc
            )
            currency = self.hass.config.currency or "HUF"
            increment = round(cumulative - float(self.state.get("sum_d", 0.0)), 4)
            _push_cost_row(
                self.hass,
                COST_D_STATISTIC_ID,
                COST_D_STATISTIC_NAME,
                currency,
                hour_start_utc,
                increment,
                cumulative,
            )
            self.state["sum_d"] = cumulative

        self.state["d"] = d

        # Keep the D figures on the summary sensors fresh between hourly runs.
        if self.attributes:
            self.attributes["sum_d"] = self.state.get("sum_d")
            self.attributes["d_price_avg"] = d.get("p_avg")
            self.attributes["d_period_allowance"] = d.get("allowance")
            self.attributes["d_period_consumption"] = round(
                float(d.get("month_kwh", 0.0)), 2
            )
            self.state["attributes"] = dict(self.attributes)

        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _update_summary(
        self, bucket_key: str, allowance: float, used_total: float, hours: int
    ) -> None:
        remaining = round(allowance - used_total, 1)
        used_pct = round(used_total / allowance * 100, 1) if allowance else 0.0
        tier = "piaci" if remaining <= 0 else "kedvezmenyes"

        crossover = "atlepve" if remaining <= 0 else "ismeretlen"
        if remaining > 0 and hours > 0:
            avg_hourly = used_total / hours
            if avg_hourly > 0:
                hit = datetime.now(BUDAPEST_TZ) + timedelta(
                    hours=remaining / avg_hourly
                )
                crossover = hit.strftime("%Y-%m-%d %H:%M")

        d: dict = self.state.get("d", {})
        self.attributes = {
            "period": bucket_key,
            "period_allowance": round(allowance, 1),
            "period_consumption": round(used_total, 2),
            "allowance_remaining": remaining,
            "allowance_used_pct": used_pct,
            "price_tier": tier,
            "tier_crossover_estimate": crossover,
            "sum_a1": self.state.get("sum_a1"),
            "sum_d": self.state.get("sum_d"),
            "last_hour_kwh": self.state.get("last_hour_kwh"),
            "last_hour_a1_cost": self.state.get("last_hour_a1_cost"),
            "d_price_avg": d.get("p_avg"),
            "d_period_allowance": d.get("allowance"),
            "d_period_consumption": round(float(d.get("month_kwh", 0.0)), 2),
        }
        # Persisted so the sensors survive a restart with their last values
        # (caller saves state right after).
        self.state["attributes"] = dict(self.attributes)
