"""Core coordinator: MQTT-fed P1 consumption -> tiered A1/D tariff cost.

Design, deliberately kept simple:

* The two P1 registers (low/high tariff, Wh) arrive over MQTT and their sum
  is the live total consumption (kWh) - exposed directly as a normal energy
  sensor, so Home Assistant's own recorder handles its long-term statistics
  and it can be used on the Energy dashboard like any other meter.
* Once an hour (at the wall-clock hour boundary), the coordinator looks at
  how much that total grew since the previous boundary, prices that one
  hour's kWh with the tiered A1 / dynamic-D formula, and appends exactly one
  new row to each cost statistic - never re-reads or rewrites history, so the
  cost stays cheap to compute no matter how long the integration has been
  running.
* A separate 15-minute timer only refreshes the "current D price" sensors
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
    CONF_MQTT_ROOT_TOPIC,
    CONF_PRICE_HIGH,
    CONF_PRICE_LOW,
    COST_D_STATISTIC_ID,
    COST_D_STATISTIC_NAME,
    COST_STATISTIC_ID,
    COST_STATISTIC_NAME,
    D_PRICE_STORAGE_KEY,
    DEFAULT_ALLOWANCE_PERIOD,
    DEFAULT_ANNUAL_THRESHOLD,
    DEFAULT_D_DISTRIBUTION_FEE,
    DEFAULT_D_ENABLED,
    DEFAULT_D_EUR_HUF,
    DEFAULT_D_MERCHANT_FEE,
    DEFAULT_D_TRANSMISSION_FEE,
    DEFAULT_D_VAT_PERCENT,
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
        """Live cumulative consumption: low + high tariff registers, in kWh."""
        if self._latest_low is None or self._latest_high is None:
            return None
        return round((self._latest_low + self._latest_high) / 1000.0, 3)

    # -- persistence -----------------------------------------------------
    async def async_load(self) -> None:
        self.state = await self._store.async_load() or {}

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

        d_cost: float | None = None
        sum_d = float(self.state.get("sum_d", 0.0))
        if self.d_enabled:
            d_cost, sum_d = await self._async_price_d(
                hour_start_utc, low_part, high_part, sum_d, currency
            )

        bucket_used[bucket_key] = used_so_far + kwh
        bucket_hours[bucket_key] = bucket_hours.get(bucket_key, 0) + 1

        self.state.update(
            {
                "sum_a1": sum_a1,
                "sum_d": sum_d,
                "bucket_used": bucket_used,
                "bucket_hours": bucket_hours,
                "last_hour_kwh": kwh,
                "last_hour_a1_cost": a1_cost,
                "last_hour_d_cost": d_cost,
            }
        )
        self._save_baseline(current_kwh, hour_start_utc)
        await self._async_save()

        self._update_summary(bucket_key, allowance, bucket_used[bucket_key], bucket_hours[bucket_key])
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    def _save_baseline(self, kwh: float, hour_start_utc: datetime) -> None:
        self.state["baseline_kwh"] = kwh
        self.state["baseline_hour"] = hour_start_utc.isoformat()

    async def _async_price_d(
        self,
        hour_start_utc: datetime,
        low_part: float,
        high_part: float,
        sum_d: float,
        currency: str,
    ) -> tuple[float | None, float]:
        """Price the overage at the average of the hour's four 15-minute HUPX prices."""
        store_key = f"{D_PRICE_STORAGE_KEY}_{self.entry.entry_id}"
        slot_isos = [
            (hour_start_utc + timedelta(minutes=15 * k)).isoformat() for k in range(4)
        ]
        try:
            prices, _meta = await async_d_gross_prices(
                self.hass, store_key, slot_isos, self.d_config
            )
        except Exception:  # noqa: BLE001 - keep the A1 accounting intact
            _LOGGER.exception("MVM Tarifa: D tarifa óránkénti árlekérés hiba")
            return None, sum_d
        if not prices:
            _LOGGER.debug(
                "MVM Tarifa: nincs D tarifa ár ehhez az órához (%s), kimarad",
                hour_start_utc,
            )
            return None, sum_d

        avg_price = sum(prices.values()) / len(prices)
        d_cost = round(low_part * self.price_low + high_part * avg_price, 4)
        sum_d = round(sum_d + d_cost, 4)
        _push_cost_row(
            self.hass,
            COST_D_STATISTIC_ID,
            COST_D_STATISTIC_NAME,
            currency,
            hour_start_utc,
            d_cost,
            sum_d,
        )
        return d_cost, sum_d

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
            "last_hour_d_cost": self.state.get("last_hour_d_cost"),
        }
