"""MVM 'D' (dynamic, HUPX-based) tariff: market data fetch and pricing.

Opt-in what-if comparison. Historical **15-minute** day-ahead prices for the
Hungarian bidding zone come from api.energy-charts.info (free, no token). The
gross per-kWh price of a 15-minute slot is

    ( hupx_eur_mwh * eur_huf / 1000 + merchant + transmission + distribution )
    * (1 + vat / 100)

where eur_huf is either a fixed configured rate or, when configured as 0, the
MNB official daily Ft/EUR rate for that day (weekends filled forward). Fetched
prices and rates are cached on disk so a re-import does not re-download them.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from aiohttp import ClientError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import ENERGY_CHARTS_PRICE_URL, MNB_SOAP_URL

_LOGGER = logging.getLogger(__name__)

_CHUNK_DAYS = 45
_BIDDING_ZONE = "HU"
_BUDAPEST = timezone(timedelta(hours=1))  # replaced by the real zone below

try:  # local date of a slot needs the real zone
    from zoneinfo import ZoneInfo

    _BUDAPEST = ZoneInfo("Europe/Budapest")
except Exception:  # pragma: no cover
    pass

_MNB_DAY_RE = re.compile(
    r'<Day date="(\d{4}-\d{2}-\d{2})">.*?curr="EUR">([0-9]+[.,][0-9]+)</Rate>',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class DTariffConfig:
    """User-tunable parts of the dynamic tariff price formula."""

    merchant_fee: float
    transmission_fee: float
    distribution_fee: float
    vat_percent: float
    eur_huf: float  # > 0: fixed rate; 0: use MNB daily rate

    def gross_price(self, eur_mwh: float, eur_huf: float) -> float:
        net = (
            eur_mwh * eur_huf / 1000.0
            + self.merchant_fee
            + self.transmission_fee
            + self.distribution_fee
        )
        return net * (1.0 + self.vat_percent / 100.0)


class DMarketStore:
    """On-disk cache: 15-min day-ahead prices and MNB daily EUR/HUF rates."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._hass = hass
        self._store: Store = Store(hass, 1, key)
        self._prices: dict[str, float] = {}  # 15-min UTC ISO -> EUR/MWh
        self._rates: dict[str, float] = {}  # date ISO -> HUF/EUR
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        stored = await self._store.async_load() or {}
        self._prices = {str(k): float(v) for k, v in stored.get("prices", {}).items()}
        self._rates = {str(k): float(v) for k, v in stored.get("rates", {}).items()}
        self._loaded = True

    async def _async_save(self) -> None:
        await self._store.async_save({"prices": self._prices, "rates": self._rates})

    # -- day-ahead prices --------------------------------------------------
    async def async_prices_for(self, slots_utc: list[datetime]) -> dict[str, float]:
        """Prices for the given UTC slots, fetching whatever is missing.

        No cut-off at "now": HUPX day-ahead prices are published for the next
        day around 13:00 CET, so a slot up to ~36h in the future can already
        be known. If it genuinely isn't published yet, energy-charts simply
        won't have it and it's just missing from the result - the next call
        (e.g. the 15-minute refresh) tries again.
        """
        await self.async_load()
        wanted = {s.replace(second=0, microsecond=0) for s in slots_utc}
        missing = sorted(s for s in wanted if s.isoformat() not in self._prices)
        if missing:
            await self._async_fetch_prices(missing[0].date(), missing[-1].date())
            await self._async_save()
        return {
            s.isoformat(): self._prices[s.isoformat()]
            for s in wanted
            if s.isoformat() in self._prices
        }

    async def _async_fetch_prices(self, start: date, end: date) -> None:
        session = async_get_clientsession(self._hass)
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=_CHUNK_DAYS))
            params = {
                "bzn": _BIDDING_ZONE,
                "start": cursor.isoformat(),
                "end": (chunk_end + timedelta(days=1)).isoformat(),
            }
            try:
                async with session.get(
                    ENERGY_CHARTS_PRICE_URL, params=params, timeout=30
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json(content_type=None)
            except (ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning(
                    "MVM Next: D tarifa árlekérés sikertelen (%s..%s): %s",
                    cursor,
                    chunk_end,
                    err,
                )
                cursor = chunk_end + timedelta(days=1)
                continue

            seconds = payload.get("unix_seconds") or []
            prices = payload.get("price") or []
            # Before ~mid-2025 the HU day-ahead auction was hourly (3600 s
            # steps); each hourly price then genuinely applied to all four
            # quarter-hours, so expand it to the 15-minute grid.
            resolution = seconds[1] - seconds[0] if len(seconds) > 1 else 3600
            per_point = max(1, round(resolution / 900))
            added = 0
            for sec, price in zip(seconds, prices):
                if price is None:
                    continue
                base = datetime.fromtimestamp(sec, timezone.utc).replace(
                    second=0, microsecond=0
                )
                value = round(float(price), 4)
                for k in range(per_point):
                    slot = base + timedelta(minutes=15 * k)
                    self._prices[slot.isoformat()] = value
                    added += 1
            _LOGGER.info(
                "MVM Next: D tarifa – %d negyedórás ár letöltve (%s..%s, %ds felbontás)",
                added,
                cursor,
                chunk_end,
                resolution,
            )
            cursor = chunk_end + timedelta(days=1)
            if cursor <= end:
                await asyncio.sleep(1)  # be polite to the free API

    # -- MNB EUR/HUF -----------------------------------------------------------
    async def async_rates_for(self, days: list[date]) -> dict[str, float]:
        await self.async_load()
        today = datetime.now(_BUDAPEST).date()
        need = [d for d in {*days} if d <= today]
        if need and any(d.isoformat() not in self._rates for d in need):
            await self._async_fetch_rates(min(need), max(need))
            await self._async_save()
        # forward-fill: for a day without a published rate use the latest earlier
        ordered = sorted(self._rates)
        out: dict[str, float] = {}
        for d in {*days}:
            key = d.isoformat()
            if key in self._rates:
                out[key] = self._rates[key]
                continue
            earlier = [r for r in ordered if r <= key]
            if earlier:
                out[key] = self._rates[earlier[-1]]
        return out

    async def _async_fetch_rates(self, start: date, end: date) -> None:
        session = async_get_clientsession(self._hass)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body>'
            '<GetExchangeRates xmlns="http://www.mnb.hu/webservices/">'
            f"<startDate>{(start - timedelta(days=10)).isoformat()}</startDate>"
            f"<endDate>{end.isoformat()}</endDate>"
            "<currencyNames>EUR</currencyNames>"
            "</GetExchangeRates></soap:Body></soap:Envelope>"
        )
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '"http://www.mnb.hu/webservices/GetExchangeRates"',
        }
        try:
            async with session.post(
                MNB_SOAP_URL, data=body, headers=headers, timeout=30
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except (ClientError, TimeoutError) as err:
            _LOGGER.warning("MVM Next: MNB árfolyam lekérés sikertelen: %s", err)
            return
        found = 0
        for day_iso, value in _MNB_DAY_RE.findall(html.unescape(text)):
            self._rates[day_iso] = float(value.replace(",", "."))
            found += 1
        _LOGGER.info(
            "MVM Next: D tarifa – %d napi MNB EUR/HUF árfolyam letöltve", found
        )


async def async_current_d_price(
    hass: HomeAssistant, store_key: str, config: DTariffConfig
) -> dict[str, float] | None:
    """The dynamic tariff price for the current 15-minute slot, right now."""
    store = DMarketStore(hass, store_key)
    now = datetime.now(timezone.utc)
    slot = now.replace(
        minute=(now.minute // 15) * 15, second=0, microsecond=0
    )
    prices = await store.async_prices_for(
        [slot - timedelta(minutes=15), slot, slot + timedelta(minutes=15)]
    )
    eur_mwh = prices.get(slot.isoformat()) or prices.get(
        (slot - timedelta(minutes=15)).isoformat()
    )
    if eur_mwh is None:
        return None

    if config.eur_huf > 0:
        rate: float | None = config.eur_huf
    else:
        rates = await store.async_rates_for([slot.astimezone(_BUDAPEST).date()])
        rate = next(iter(rates.values()), None)
    if rate is None:
        return None

    return {
        "hupx_eur_mwh": round(eur_mwh, 2),
        "eur_huf": round(rate, 2),
        "raw_huf_kwh": round(eur_mwh * rate / 1000.0, 2),
        "gross_huf_kwh": round(config.gross_price(eur_mwh, rate), 2),
        "slot_start": slot.isoformat(),
    }


async def async_d_price_forecast(
    hass: HomeAssistant,
    store_key: str,
    config: DTariffConfig,
    hours_back: int = 2,
    hours_ahead: int = 24,
) -> list[dict[str, object]]:
    """Known HUPX/D-tariff prices from ``hours_back`` ago to ``hours_ahead``.

    HUPX publishes the next day's day-ahead prices around 13:00 CET, so by
    the afternoon roughly the next ~24-36h are already known - this is what
    lets an automation plan around tomorrow's cheap/expensive hours. Only
    slots that are actually published come back; call again later (e.g. on
    the next 15-minute refresh) to pick up newly published hours.
    """
    store = DMarketStore(hass, store_key)
    now = datetime.now(timezone.utc)
    start = now.replace(
        minute=(now.minute // 15) * 15, second=0, microsecond=0
    ) - timedelta(hours=hours_back)
    n_slots = int((hours_back + hours_ahead) * 4)
    slots = [start + timedelta(minutes=15 * i) for i in range(n_slots)]

    prices = await store.async_prices_for(slots)
    if not prices:
        return []

    if config.eur_huf > 0:
        rates: dict[str, float] = {}
    else:
        days = sorted({s.astimezone(_BUDAPEST).date() for s in slots})
        rates = await store.async_rates_for(days)

    forecast: list[dict[str, object]] = []
    for slot in slots:
        eur_mwh = prices.get(slot.isoformat())
        if eur_mwh is None:
            continue
        if config.eur_huf > 0:
            rate = config.eur_huf
        else:
            rate = rates.get(slot.astimezone(_BUDAPEST).date().isoformat())
        if rate is None:
            continue
        forecast.append(
            {
                "start": slot.isoformat(),
                "hupx_eur_mwh": round(eur_mwh, 2),
                "raw_huf_kwh": round(eur_mwh * rate / 1000.0, 2),
                "gross_huf_kwh": round(config.gross_price(eur_mwh, rate), 2),
            }
        )
    return forecast


async def async_d_gross_prices(
    hass: HomeAssistant,
    store_key: str,
    slot_isos: list[str],
    config: DTariffConfig,
) -> tuple[dict[str, float], dict[str, object]]:
    """Return ({consumption slot ISO: gross HUF/kWh}, meta).

    slot_isos are the 15-minute consumption timestamps (UTC ISO). Only the
    part above the yearly allowance is priced at this rate; the tiering is
    applied by the caller.
    """
    if not slot_isos:
        return {}, {"slots_priced": 0, "slots_total": 0}

    store = DMarketStore(hass, store_key)
    slots_utc = [
        datetime.fromisoformat(iso).astimezone(timezone.utc) for iso in slot_isos
    ]
    prices = await store.async_prices_for(slots_utc)

    if config.eur_huf > 0:
        rate_getter = lambda _day: config.eur_huf  # noqa: E731
        rate_source = f"fix {config.eur_huf:g}"
    else:
        days = [s.astimezone(_BUDAPEST).date() for s in slots_utc]
        rates = await store.async_rates_for(days)
        rate_getter = lambda day: rates.get(day.isoformat())  # noqa: E731
        rate_source = "MNB napi"

    gross: dict[str, float] = {}
    for iso in slot_isos:
        slot_utc = (
            datetime.fromisoformat(iso)
            .astimezone(timezone.utc)
            .replace(second=0, microsecond=0)
        )
        eur_mwh = prices.get(slot_utc.isoformat())
        if eur_mwh is None:
            continue
        rate = rate_getter(slot_utc.astimezone(_BUDAPEST).date())
        if rate is None:
            continue
        gross[iso] = round(config.gross_price(eur_mwh, rate), 4)

    meta = {
        "slots_priced": len(gross),
        "slots_total": len(slot_isos),
        "eur_huf": rate_source,
    }
    return gross, meta
