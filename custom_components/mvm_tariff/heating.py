"""Daily consumption vs. daily mean outdoor temperature.

Daily kWh always comes from the recorder's long-term statistics of the
consumption sensor (period "day", aligned to local midnight). The daily mean
temperature comes either from a local thermometer's recorder statistics or
from the Open-Meteo archive API for a location. Nothing is accumulated or
stored by the integration, so existing history is available immediately.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiohttp import ClientError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import OPEN_METEO_ARCHIVE_URL, OPEN_METEO_GEOCODING_URL, TIME_ZONE

_LOGGER = logging.getLogger(__name__)


async def _async_recorder_daily(
    hass: HomeAssistant, statistic_id: str, stat_type: str, days: int
) -> dict[str, float]:
    """{local ISO date: value} of one daily statistic type for complete days."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    end = dt_util.start_of_local_day()
    start = end - timedelta(days=days)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "day",
        None,
        {stat_type},
    )
    result: dict[str, float] = {}
    for row in stats.get(statistic_id, []):
        value = row.get(stat_type)
        if value is None:
            continue
        day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
        result[day.isoformat()] = value
    return result


async def async_sensor_daily_temps(
    hass: HomeAssistant, entity_id: str, days: int
) -> dict[str, float]:
    temps = await _async_recorder_daily(hass, entity_id, "mean", days)
    if not temps:
        _LOGGER.warning(
            "MVM Tarifa: nincs napi átlag statisztika a(z) %s hőmérő szenzorhoz "
            "(state_class: measurement szükséges)",
            entity_id,
        )
    return temps


async def async_open_meteo_daily_temps(
    hass: HomeAssistant, latitude: float, longitude: float, days: int
) -> dict[str, float]:
    """Daily mean 2 m temperature for complete local days, from Open-Meteo."""
    end = dt_util.start_of_local_day().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_mean",
        "timezone": TIME_ZONE,
    }
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            OPEN_METEO_ARCHIVE_URL, params=params, timeout=30
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        daily = payload["daily"]
        pairs = zip(daily["time"], daily["temperature_2m_mean"])
    except (ClientError, TimeoutError, ValueError, KeyError, TypeError) as err:
        _LOGGER.warning("MVM Tarifa: Open-Meteo hőmérséklet lekérés sikertelen: %s", err)
        return {}
    return {day: temp for day, temp in pairs if temp is not None}


async def async_geocode(
    hass: HomeAssistant, name: str
) -> tuple[str, float, float] | None:
    """(display name, latitude, longitude) of the best match, preferring Hungary.

    None when nothing matches; raises ClientError/TimeoutError on network failure.
    """
    session = async_get_clientsession(hass)
    for country in ("HU", None):
        params = {"name": name, "count": 1, "language": "hu"}
        if country:
            params["countryCode"] = country
        async with session.get(
            OPEN_METEO_GEOCODING_URL, params=params, timeout=15
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        results = payload.get("results") or []
        if results:
            hit = results[0]
            return hit["name"], float(hit["latitude"]), float(hit["longitude"])
    return None


async def async_daily_temp_vs_consumption(
    hass: HomeAssistant,
    consumption_entity_id: str,
    temps: dict[str, float],
    days: int,
) -> list[dict[str, object]]:
    """[{date, temp, kwh}] for each complete local day, oldest first.

    Days missing either value (sensor offline, no statistics yet) are skipped.
    """
    kwh_by_day = await _async_recorder_daily(
        hass, consumption_entity_id, "change", days
    )
    return [
        {"date": day, "temp": round(temps[day], 1), "kwh": round(kwh, 2)}
        for day, kwh in sorted(kwh_by_day.items())
        if day in temps
    ]
