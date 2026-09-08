"""Data update coordinator for MyWaterAdvisor."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from typing import Any
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MyWaterAdvisorAuthError, MyWaterAdvisorClient, MyWaterAdvisorError
from .const import ANOMALY_GALLONS_PER_HOUR, DEFAULT_WATER_PRICE_PER_GALLON, DOMAIN

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=1)
LOOKBACK = timedelta(days=3)
AVG_HOUSEHOLDS_MONTHS = timedelta(days=365)
STORAGE_VERSION = 1

# Names shown for the backfilled statistics in the Energy dashboard's source picker.
EXTERNAL_STATISTIC_NAME = "Water Meter Consumption"
EXTERNAL_COST_STATISTIC_NAME = "Water Meter Cost"

_MONTHLY_LIMIT_KEYS = ("limit", "monthlyLimit", "value", "consumptionLimit", "dailyLimit")


def _coerce_limit(value) -> float | None:
    """The portal's GET returns {"limit": ...} where the value may be a number
    or a numeric string, and the string "null" (or empty) means "not set" —
    per the JS: e.limit && "null" !== e.limit ? e.limit : 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() == "null":
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _clean_consumption_rows(readings: list) -> list[dict]:
    clean = []
    for row in readings:
        if not isinstance(row, dict):
            continue
        cons = row.get("cons")
        estimation_type = row.get("estimationType", 0)
        if cons is None or estimation_type not in (0, None):
            continue
        if not isinstance(cons, (int, float)):
            try:
                cons = float(cons)
            except (TypeError, ValueError):
                continue
        if cons < 0 or cons > ANOMALY_GALLONS_PER_HOUR:
            continue
        clean.append({**row, "cons": cons})
    return clean


def _parse_and_sort(clean: list[dict]) -> list[tuple[datetime, float]]:
    parsed = []
    for row in clean:
        raw_dt = row.get("dateTime")
        if not raw_dt:
            continue
        try:
            # The portal returns naive "YYYY-MM-DDTHH:MM:SS" strings with no
            # offset. Treat them as UTC (the reference project's own
            # documented assumption).
            ts = datetime.fromisoformat(raw_dt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        parsed.append((ts, row["cons"]))
    return sorted(parsed, key=lambda item: item[0])


def _parse_monthly_limit(raw) -> float | None:
    if isinstance(raw, (int, float, str)):
        return _coerce_limit(raw)
    if isinstance(raw, dict):
        for key in _MONTHLY_LIMIT_KEYS:
            if key in raw:
                return _coerce_limit(raw.get(key))
        return None
    if isinstance(raw, list) and raw:
        return _parse_monthly_limit(raw[0])
    return None


class MyWaterAdvisorCoordinator(DataUpdateCoordinator):
    """Fetches hourly consumption and maintains running totals."""

    def __init__(self, hass: HomeAssistant, entry_id: str, email: str, password: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = MyWaterAdvisorClient(async_get_clientsession(hass), email, password)
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}")
        self._total = 0.0
        self._daily_total = 0.0
        self._daily_date: str | None = None
        self._daily_reset_at: datetime | None = None
        self._last_processed: datetime | None = None
        self.meter_info: dict = {}
        # Dedicated external statistics — deliberately NOT the live sensor's
        # own statistic_id (see MyWaterAdvisorTotalSensor's docstring for why
        # sharing one corrupted the Energy dashboard). Fed directly from the
        # API's real hourly rows, so they carry full history and are
        # unaffected by the live entities going unavailable/reloading.
        self._external_statistic_id = f"{DOMAIN}:{entry_id}_water_consumption"
        self._external_cost_statistic_id = f"{DOMAIN}:{entry_id}_water_cost"
        self._water_price_per_gallon = DEFAULT_WATER_PRICE_PER_GALLON

    @property
    def water_price_per_gallon(self) -> float:
        return self._water_price_per_gallon

    async def async_set_water_price_per_gallon(self, value: float) -> None:
        """Update the rate used for the cost backfill statistic — set from the
        "Water Price Per Gallon" number entity, so the rate is editable in
        the UI instead of requiring a code change."""
        self._water_price_per_gallon = value
        await self._async_save()
        self.async_update_listeners()

    async def async_load(self) -> None:
        """Restore persisted totals before the first refresh."""
        stored = await self._store.async_load()
        if not stored:
            return
        self._total = stored.get("total", 0.0)
        self._daily_total = stored.get("daily_total", 0.0)
        self._daily_date = stored.get("daily_date")
        daily_reset_at = stored.get("daily_reset_at")
        if daily_reset_at:
            self._daily_reset_at = datetime.fromisoformat(daily_reset_at)
        last_processed = stored.get("last_processed")
        if last_processed:
            parsed_ts = datetime.fromisoformat(last_processed)
            if parsed_ts.tzinfo is None:
                # Migrate a baseline saved before dateTime values were
                # normalized to UTC.
                parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
            self._last_processed = parsed_ts
        self._water_price_per_gallon = stored.get("water_price_per_gallon", DEFAULT_WATER_PRICE_PER_GALLON)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "total": self._total,
                "daily_total": self._daily_total,
                "daily_date": self._daily_date,
                "daily_reset_at": self._daily_reset_at.isoformat() if self._daily_reset_at else None,
                "last_processed": self._last_processed.isoformat() if self._last_processed else None,
                "water_price_per_gallon": self._water_price_per_gallon,
            }
        )

    async def _async_import_external_statistic(
        self,
        parsed: list[tuple[datetime, float]],
        *,
        statistic_id: str,
        name: str,
        unit: str,
        per_reading_value,
        round_digits: int,
    ) -> None:
        """Import real hourly readings into a dedicated external statistic.

        Uses async_add_external_statistics under our own "mywateradvisor:..."
        statistic_id rather than the live sensor's entity-based one, so this
        can never collide with HA's native total_increasing compiler (that
        collision is what corrupted the Energy dashboard previously — see
        MyWaterAdvisorTotalSensor's docstring). Continuing from the last
        imported hour's cumulative sum, rather than re-importing everything
        every refresh, makes this idempotent and gives the Energy dashboard
        full history instead of just what the live entities happened to be
        recording.

        Wrapped in one broad try/except, like the optional-feature fetches
        below: this is a nice-to-have for the Energy dashboard, and a
        recorder-internals surprise here (e.g. get_last_statistics returning
        "start" as a raw Unix timestamp instead of a datetime, depending on
        HA version) must never take down the core water-total tracking.
        """
        try:
            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
            )

            running_sum = 0.0
            last_start: datetime | None = None
            if last_stats.get(statistic_id):
                last_row = last_stats[statistic_id][0]
                running_sum = last_row["sum"] or 0.0
                raw_start = last_row["start"]
                last_start = (
                    dt_util.utc_from_timestamp(raw_start)
                    if isinstance(raw_start, (int, float))
                    else dt_util.as_utc(raw_start)
                )

            new_rows = [(ts, cons) for ts, cons in parsed if last_start is None or ts > last_start]
            if not new_rows:
                return

            statistics: list[StatisticData] = []
            for ts, cons in new_rows:
                running_sum += per_reading_value(cons)
                statistics.append(
                    StatisticData(
                        start=ts.replace(minute=0, second=0, microsecond=0), sum=round(running_sum, round_digits)
                    )
                )

            metadata = StatisticMetaData(
                has_mean=False,
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=name,
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=unit,
            )
            async_add_external_statistics(self.hass, metadata, statistics)
        except Exception as err:  # noqa: BLE001 - recorder internals; never fail the poll over this
            _LOGGER.debug("MyWaterAdvisor: statistics import for %s failed (non-fatal): %s", statistic_id, err)

    async def _async_backfill_statistics(self, parsed: list[tuple[datetime, float]]) -> None:
        if not parsed:
            return
        await self._async_import_external_statistic(
            parsed,
            statistic_id=self._external_statistic_id,
            name=EXTERNAL_STATISTIC_NAME,
            unit=UnitOfVolume.GALLONS,
            per_reading_value=lambda cons: cons,
            round_digits=2,
        )
        await self._async_import_external_statistic(
            parsed,
            statistic_id=self._external_cost_statistic_id,
            name=EXTERNAL_COST_STATISTIC_NAME,
            unit="USD",
            per_reading_value=lambda cons: cons * self._water_price_per_gallon,
            round_digits=4,
        )

    async def _fetch_optional(self, coro, label: str):
        """Fetch a non-critical endpoint; log and return None instead of failing the update.

        An auth error is never "non-fatal" — re-raise it as ConfigEntryAuthFailed
        (rather than the bare MyWaterAdvisorAuthError) so it reaches
        DataUpdateCoordinator as a request to start the reauth flow no matter
        which optional call surfaced it, not just the core fetch above.
        """
        try:
            return await coro
        except MyWaterAdvisorAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MyWaterAdvisorError as err:
            _LOGGER.debug("MyWaterAdvisor: %s fetch failed (non-fatal): %s", label, err)
            return None

    async def _fetch_optional_parallel(self, *tasks: tuple[str, callable]) -> dict[str, Any]:
        """Run multiple optional fetches concurrently.

        Each task is a (label, callable) pair. Auth errors from any task
        propagate immediately; all other errors are logged and the result
        for that label is None. Returns a {label: result} dict.
        """
        async def _run_one(label: str, coro):
            try:
                return label, await coro
            except MyWaterAdvisorAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except MyWaterAdvisorError as err:
                _LOGGER.debug("MyWaterAdvisor: %s fetch failed (non-fatal): %s", label, err)
                return label, None

        results = dict(await asyncio.gather(*(_run_one(label, fn()) for label, fn in tasks)))
        return results

    async def _async_update_data(self) -> dict:
        try:
            meter_id = await self.client.async_get_meter_id()
            # _ensure_meter_info() (called by async_get_meter_id) already caches
            # the meter object — grab it instead of fetching again.
            self.meter_info = self.client._meter_info or {}
            end = datetime.now(timezone.utc)
            start = end - LOOKBACK
            rows = await self.client.async_get_hourly_consumption(start, end)
        except MyWaterAdvisorAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MyWaterAdvisorError as err:
            raise UpdateFailed(str(err)) from err

        readings = rows if isinstance(rows, list) else rows.get("data", [])
        clean = _clean_consumption_rows(readings)
        parsed = _parse_and_sort(clean)

        await self._async_backfill_statistics(parsed)

        # Roll the daily counter over at local midnight even when no new
        # reading has arrived yet — otherwise "Today's Water Usage" keeps
        # showing yesterday's total until a reading dated in the new day
        # finally comes in through the incremental loop below.
        current_local_day = dt_util.now().strftime("%Y-%m-%d")
        if current_local_day != self._daily_date:
            self._daily_date = current_local_day
            self._daily_total = 0.0
            # Mark the rollover with the exact reset instant (local midnight)
            # so the daily sensor's TOTAL statistics treat it as a deliberate
            # reset rather than an anomalous drop.
            self._daily_reset_at = dt_util.start_of_local_day()
            await self._async_save()

        if self._last_processed is None:
            if parsed:
                # First run ever: seed the live total to the visible lookback
                # sum instead of starting at 0, so the Total sensor reflects
                # real recent consumption immediately rather than counting the
                # whole backlog as usage the instant it first reports.
                self._last_processed = parsed[-1][0]
                self._total = round(sum(cons for _, cons in parsed), 2)
                # Seed today's portion too, rather than leaving it at 0 until
                # the next incremental reading — the lookback window may
                # already include hours from today.
                self._daily_total = round(
                    sum(
                        cons
                        for ts, cons in parsed
                        if dt_util.as_local(ts).strftime("%Y-%m-%d") == self._daily_date
                    ),
                    2,
                )
                await self._async_save()
        else:
            changed = False
            for ts, cons in parsed:
                if ts <= self._last_processed:
                    continue
                local_day = dt_util.as_local(ts).strftime("%Y-%m-%d")
                if local_day != self._daily_date:
                    self._daily_date = local_day
                    self._daily_total = 0.0
                    # Reset instant is this reading's local midnight (see the
                    # rollover block above for why last_reset must be marked).
                    self._daily_reset_at = dt_util.start_of_local_day(dt_util.as_local(ts))
                self._total += cons
                self._daily_total += cons
                self._last_processed = ts
                changed = True
            if changed:
                await self._async_save()

        # --- Optional features below: each is isolated so a bad guess on one
        # (mainly avg_households, whose response shape is unconfirmed) can't
        # take down the core water-total tracking above.
        # All fired concurrently — a single slow endpoint no longer blocks the
        # rest from completing. ---

        optional = await self._fetch_optional_parallel(
            ("alerts", lambda: self.client.async_get_alerts()),
            ("forecast", lambda: self.client.async_get_forecast()),
            ("vacations", lambda: self.client.async_get_vacations()),
            ("billing_cycles", lambda: self.client.async_get_billing_cycles()),
            ("monthly_limit", lambda: self.client.async_get_monthly_limit()),
            ("avg_households", lambda: self.client.async_get_avg_households(
                end - AVG_HOUSEHOLDS_MONTHS, end,
            )),
        )

        alerts = optional.get("alerts") or []
        if not isinstance(alerts, list):
            alerts = []

        forecast_raw = optional.get("forecast")
        forecast_gallons = (
            forecast_raw.get("estimatedConsumption")
            if isinstance(forecast_raw, dict)
            else None
        )

        vacations = optional.get("vacations") or []
        if not isinstance(vacations, list):
            vacations = []
        my_vacations = [
            v for v in vacations if not isinstance(v, dict) or v.get("meterCount") in (None, meter_id)
        ]

        billing_cycle_start = None
        billing_cycle_end = None
        billing_cycle_usage = None
        monthly_limit = None

        billing_cycles = optional.get("billing_cycles")
        if isinstance(billing_cycles, list):
            current_cycle = next(
                (
                    c
                    for c in billing_cycles
                    if isinstance(c, dict)
                    and c.get("type") == "current"
                    and c.get("meterCount") in (None, meter_id)
                ),
                None,
            )
            if current_cycle:
                billing_cycle_start = current_cycle.get("billingCycleStart")
                billing_cycle_end = current_cycle.get("billingCycleEnd")

        if billing_cycle_start:
            try:
                cycle_start_dt = datetime.fromisoformat(billing_cycle_start).replace(tzinfo=timezone.utc)
                daily_rows = await self.client.async_get_daily_consumption(cycle_start_dt, end)
                daily_readings = daily_rows if isinstance(daily_rows, list) else daily_rows.get("data", [])
                billing_cycle_usage = round(
                    sum(row["cons"] for row in _clean_consumption_rows(daily_readings)), 2
                )
            except MyWaterAdvisorAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (MyWaterAdvisorError, ValueError, KeyError) as err:
                _LOGGER.debug("MyWaterAdvisor: billing cycle usage fetch failed (non-fatal): %s", err)

        monthly_limit_raw = optional.get("monthly_limit")
        monthly_limit = _parse_monthly_limit(monthly_limit_raw)

        avg_households_raw = optional.get("avg_households")

        return {
            "total": round(self._total, 2),
            "daily_total": round(self._daily_total, 2),
            "daily_reset_at": self._daily_reset_at,
            "last_reading_time": self._last_processed,
            "meter_info": self.meter_info,
            "raw_rows": clean,
            "alerts": alerts,
            "forecast_gallons": forecast_gallons,
            "vacations": my_vacations,
            "billing_cycle_start": billing_cycle_start,
            "billing_cycle_end": billing_cycle_end,
            "billing_cycle_usage": billing_cycle_usage,
            "monthly_limit": monthly_limit,
            "avg_households_raw": avg_households_raw,
        }
