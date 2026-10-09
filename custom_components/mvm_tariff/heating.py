"""Daily consumption vs. daily mean outdoor temperature, from recorder statistics.

Nothing is accumulated or stored by the integration here: both series are read
back from Home Assistant's own long-term statistics (period "day", aligned to
local midnight), so existing history is available immediately after setup.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


async def async_daily_temp_vs_consumption(
    hass: HomeAssistant,
    consumption_entity_id: str,
    temp_entity_id: str,
    days: int,
) -> list[dict[str, object]]:
    """[{date, temp, kwh}] for each complete local day, oldest first.

    Days missing either value (sensor offline, no statistics yet) are skipped.
    """
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
        {consumption_entity_id, temp_entity_id},
        "day",
        None,
        {"change", "mean"},
    )

    temps = {
        row["start"]: row["mean"]
        for row in stats.get(temp_entity_id, [])
        if row.get("mean") is not None
    }
    if not temps:
        _LOGGER.warning(
            "MVM Tarifa: nincs napi átlag statisztika a(z) %s hőmérő szenzorhoz "
            "(state_class: measurement szükséges)",
            temp_entity_id,
        )

    history: list[dict[str, object]] = []
    for row in stats.get(consumption_entity_id, []):
        temp = temps.get(row["start"])
        kwh = row.get("change")
        if temp is None or kwh is None:
            continue
        day = dt_util.as_local(dt_util.utc_from_timestamp(row["start"])).date()
        history.append(
            {"date": day.isoformat(), "temp": round(temp, 1), "kwh": round(kwh, 2)}
        )
    return history
