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
    statistics_during_period,
)
from homeassistant.const import VOLUME, UnitOfVolume
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
# How far back a single poll is allowed to reach to recover a gap (fresh
# install, or the coordinator/HA having been down or unavailable for a
# while). Without this, a plain `end - LOOKBACK` window permanently loses
# any consumption older than LOOKBACK the moment it ages out of range, with
# no way to ever recover it — confirmed to have dropped several real days
# of usage from the Energy dashboard after this integration's own
# multi-day outage during initial setup.
INITIAL_BACKFILL_LOOKBACK = timedelta(days=35)
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

    async def async_resync_statistics(self) -> None:
        """One-time recovery for a gap that predates this coordinator's own
        fixes (e.g. data missed before the INITIAL_BACKFILL_LOOKBACK/
        late-bucket fixes existed, or lost during initial setup).

        Resetting ``_last_processed`` to None makes the next
        ``_async_update_data`` treat this like a fresh install and fetch
        INITIAL_BACKFILL_LOOKBACK days instead of just LOOKBACK — the same
        path a real first run takes, just triggered on demand. Total/Daily
        get recomputed from that wider fetch automatically; nothing here
        needs to guess or carry forward old (possibly wrong) values.
        """
        self._last_processed = None
        await self._async_save()
        await self.async_request_refresh()

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
        unit_class: str | None,
        per_reading_value,
        round_digits: int,
    ) -> float | None:
        """Import real hourly readings into a dedicated external statistic.

        Returns the final running sum (baseline-before-window plus this
        window's total) so callers can reuse it as an authoritative,
        late-bucket-aware total instead of maintaining their own separately
        — or None if there was nothing to import.

        Uses async_add_external_statistics under our own "mywateradvisor:..."
        statistic_id rather than the live sensor's entity-based one, so this
        can never collide with HA's native total_increasing compiler (that
        collision is what corrupted the Energy dashboard previously — see
        MyWaterAdvisorTotalSensor's docstring).

        Re-imports the entire lookback window every poll rather than only
        appending rows newer than the last stored one. async_add_external_statistics
        is an upsert keyed on (statistic_id, start), so re-sending a bucket
        whose consumption the portal revised after first publish (late mesh
        reads, spike corrections — confirmed in the upstream API docs) updates
        the existing row in place instead of dropping it. The previous
        ``ts > last_start`` filter silently discarded those revisions and left
        the Energy dashboard with wrong/stale totals.

        To keep this idempotent (re-importing overlapping rows must not
        double-count), the running sum is re-seeded each poll to the stored
        sum at the earliest bucket we're re-importing, minus that bucket's
        own old contribution — i.e. the cumulative sum just BEFORE the first
        bucket. Existing rows are read via statistics_during_period. This
        mirrors the reference implementation's baseline-subtraction approach.

        Wrapped in one broad try/except, like the optional-feature fetches
        below: this is a nice-to-have for the Energy dashboard, and a
        recorder-internals surprise here (e.g. get_last_statistics returning
        "start" as a raw Unix timestamp instead of a datetime, depending on
        HA version) must never take down the core water-total tracking.
        Failures are logged at WARNING with a traceback so a regression here
        is visible — the previous DEBUG log hid a missing get_instance import
        for an entire release.
        """
        if not parsed:
            return

        try:
            # Hour-aligned starts, as required by the recorder (minutes/seconds
            # must be zero) and as returned by statistics_during_period.
            buckets = [
                (ts.replace(minute=0, second=0, microsecond=0), cons) for ts, cons in parsed
            ]
            first_start = buckets[0][0]

            # Cumulative sum of the NEW bucket values up to and including each
            # start. Used to recover the pre-window baseline: for any existing
            # stored row whose start matches one of our buckets, the stored sum
            # already includes that bucket's (old) value, so
            #   baseline = stored_sum - bucket_total_at_that_start
            # is the cumulative sum just before that bucket. Re-adding every
            # bucket from there on then reproduces the correct running totals
            # without double-counting the re-imported window.
            bucket_totals: dict[datetime, float] = {}
            running_bucket_total = 0.0
            for start, cons in buckets:
                running_bucket_total = round(running_bucket_total + per_reading_value(cons), round_digits)
                bucket_totals[start] = running_bucket_total

            running_sum = await self._async_baseline_sum(statistic_id, first_start, bucket_totals)

            statistics: list[StatisticData] = []
            for ts, cons in buckets:
                running_sum = round(running_sum + per_reading_value(cons), round_digits)
                statistics.append(StatisticData(start=ts, sum=running_sum))

            if not statistics:
                return

            metadata = StatisticMetaData(
                has_mean=False,
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=name,
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=unit,
                # unit_class is required from HA 2026.11 (omitting it raises a
                # deprecation warning on current HA). "volume" for gallons
                # (VolumeConverter.UNIT_CLASS == const.VOLUME); None for the USD
                # cost statistic — there is no monetary unit converter, and None
                # is the value HA itself assigns for units outside
                # STATISTIC_UNIT_TO_UNIT_CONVERTER.
                unit_class=unit_class,
            )
            async_add_external_statistics(self.hass, metadata, statistics)
            return running_sum
        except Exception as err:  # noqa: BLE001 - recorder internals; never fail the poll over this
            _LOGGER.warning(
                "MyWaterAdvisor: statistics import for %s failed (non-fatal): %s",
                statistic_id,
                err,
                exc_info=True,
            )

    async def _async_baseline_sum(
        self, statistic_id: str, first_start: datetime, bucket_totals: dict[datetime, float]
    ) -> float:
        """Recover the cumulative sum stored just before the import window.

        See _async_import_external_statistic for the double-counting rationale.
        ``bucket_totals`` maps each bucket start to the cumulative sum of NEW
        bucket values up to and including that start.

        1. Read the existing statistic rows from ``first_start`` onward.
        2. For the first stored row whose start matches a bucket we're
           re-importing, the stored sum includes that bucket's old value, so
           ``stored_sum - bucket_total[start]`` is the baseline before the
           window — re-adding the buckets reproduces correct running totals.
        3. If no stored row falls inside the window (first import, or the
           window slid past all stored rows), fall back to the statistic's
           last stored sum — but only if it predates ``first_start`` (else
           re-adding the window would double-count it). Brand-new statistics
           start at 0.0.
        """
        period_stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            first_start,
            None,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        existing = period_stats.get(statistic_id, []) if period_stats else []
        for record in existing:
            raw_start = record.get("start")
            record_start = (
                dt_util.utc_from_timestamp(raw_start)
                if isinstance(raw_start, (int, float))
                else dt_util.as_utc(raw_start)
            )
            if record_start in bucket_totals and record.get("sum") is not None:
                return float(record["sum"]) - bucket_totals[record_start]

        # No stored row inside the window: carry the last stored sum forward
        # only if it predates first_start (its contribution is entirely below
        # the window, so re-adding the window doesn't re-count it).
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if last_stats.get(statistic_id):
            last_row = last_stats[statistic_id][0]
            last_sum = last_row.get("sum")
            raw_start = last_row.get("start")
            last_start = (
                dt_util.utc_from_timestamp(raw_start)
                if isinstance(raw_start, (int, float))
                else dt_util.as_utc(raw_start)
            )
            if last_sum is not None and last_start is not None and last_start < first_start:
                return float(last_sum)
        return 0.0

    async def _async_stat_sum_before(self, statistic_id: str, at: datetime) -> float:
        """Return the external statistic's cumulative sum as of just before
        ``at`` (0.0 if there's no stored row that far back).

        Reads persisted long-term stats directly, independent of the current
        poll's hourly lookback window — so, unlike ``parsed``, this reflects
        the full stored history even beyond LOOKBACK/INITIAL_BACKFILL_LOOKBACK.
        Used by the billing-cycle reconciliation check below.
        """
        period_stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, None, at, {statistic_id}, "hour", None, {"sum"},
        )
        rows = period_stats.get(statistic_id, []) if period_stats else []
        if not rows:
            return 0.0
        last_sum = rows[-1].get("sum")
        return float(last_sum) if last_sum is not None else 0.0

    async def _async_reconcile_billing_cycle(
        self, cycle_start_dt: datetime, end: datetime, billing_cycle_usage: float
    ) -> dict:
        """Compare the hourly-derived total for the current billing cycle
        against the portal's own daily-consumption total for the same
        window (``billing_cycle_usage`` — confirmed to match the provider's
        billing statements exactly, since it isn't bounded by any lookback
        window).

        This is a diagnostic check only — it never corrects
        ``self._total``/``self._daily_total`` itself, since the hourly path
        is kept for its finer granularity and the daily endpoint has its own
        publish lag. It exists to surface a regression like the LOOKBACK/
        watermark bugs fixed in 1.3.0 (a real, silent divergence between the
        two) instead of only discovering it by manually diffing against the
        provider's portal again.
        """
        try:
            hourly_cycle_usage = round(
                await self._async_stat_sum_before(self._external_statistic_id, end)
                - await self._async_stat_sum_before(self._external_statistic_id, cycle_start_dt),
                2,
            )
        except Exception as err:  # noqa: BLE001 - recorder internals; never fail the poll over this
            _LOGGER.debug("MyWaterAdvisor: billing cycle reconciliation failed (non-fatal): %s", err)
            return {}

        delta = round(billing_cycle_usage - hourly_cycle_usage, 2)
        # Ignore tiny/early-cycle noise: require a delta that's both a
        # meaningful fraction of the cycle and not just rounding dust.
        threshold = max(10.0, 0.02 * billing_cycle_usage)
        if abs(delta) > threshold:
            _LOGGER.warning(
                "MyWaterAdvisor: billing cycle reconciliation mismatch — "
                "hourly-derived total %.2f gal vs portal daily total %.2f gal "
                "(delta %.2f gal). The hourly/Energy-dashboard totals may be "
                "under- or over-reporting; the portal's own daily total is "
                "the reference.",
                hourly_cycle_usage,
                billing_cycle_usage,
                delta,
            )
        return {
            "hourly_derived_total": hourly_cycle_usage,
            "delta": delta,
        }

    async def _async_backfill_statistics(self, parsed: list[tuple[datetime, float]]) -> float | None:
        """Import both external statistics; returns the volume statistic's
        final running sum (see _async_import_external_statistic) for the
        caller to reuse as the live Total sensor's value."""
        if not parsed:
            return None
        volume_total = await self._async_import_external_statistic(
            parsed,
            statistic_id=self._external_statistic_id,
            name=EXTERNAL_STATISTIC_NAME,
            unit=UnitOfVolume.GALLONS,
            unit_class=VOLUME,
            per_reading_value=lambda cons: cons,
            round_digits=2,
        )
        await self._async_import_external_statistic(
            parsed,
            statistic_id=self._external_cost_statistic_id,
            name=EXTERNAL_COST_STATISTIC_NAME,
            unit="USD",
            unit_class=None,
            per_reading_value=lambda cons: cons * self._water_price_per_gallon,
            round_digits=4,
        )
        return volume_total

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
            # Normally just LOOKBACK days back. But widen back to
            # `_last_processed` (capped at INITIAL_BACKFILL_LOOKBACK so a very
            # long outage doesn't balloon the request) whenever the gap since
            # the last successful poll is bigger than LOOKBACK — see
            # INITIAL_BACKFILL_LOOKBACK's docstring for why.
            if self._last_processed is None:
                start = end - INITIAL_BACKFILL_LOOKBACK
            else:
                start = max(
                    min(end - LOOKBACK, self._last_processed),
                    end - INITIAL_BACKFILL_LOOKBACK,
                )
            rows = await self.client.async_get_hourly_consumption(start, end)
        except MyWaterAdvisorAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MyWaterAdvisorError as err:
            raise UpdateFailed(str(err)) from err

        readings = rows if isinstance(rows, list) else rows.get("data", [])
        clean = _clean_consumption_rows(readings)
        parsed = _parse_and_sort(clean)

        window_total = await self._async_backfill_statistics(parsed)

        # Roll the daily counter over at local midnight even when no new
        # reading has arrived yet — otherwise "Today's Water Usage" keeps
        # showing yesterday's total until a reading dated in the new day
        # finally comes in.
        current_local_day = dt_util.now().strftime("%Y-%m-%d")
        day_rolled_over = current_local_day != self._daily_date
        if day_rolled_over:
            self._daily_date = current_local_day
            self._daily_total = 0.0
            # Mark the rollover with the exact reset instant (local midnight)
            # so the daily sensor's TOTAL statistics treat it as a deliberate
            # reset rather than an anomalous drop.
            self._daily_reset_at = dt_util.start_of_local_day()

        changed = day_rolled_over

        if parsed:
            # Recomputed fresh from the whole lookback window every poll,
            # rather than incrementally accumulated past a "last processed"
            # watermark: the portal publishes hourly buckets late or revises
            # them after the fact (see _async_import_external_statistic), and
            # a watermark silently drops any such bucket whose timestamp
            # falls at or before one that already advanced it — this was
            # under-reporting real days by 30-90% versus the provider's own
            # portal totals.
            self._daily_total = round(
                sum(
                    cons
                    for ts, cons in parsed
                    if dt_util.as_local(ts).strftime("%Y-%m-%d") == self._daily_date
                ),
                2,
            )
            newest = parsed[-1][0]
            if self._last_processed is None or newest > self._last_processed:
                self._last_processed = newest
            changed = True

        if window_total is not None:
            self._total = round(window_total, 2)
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
        cycle_reconciliation: dict = {}

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
                cycle_reconciliation = await self._async_reconcile_billing_cycle(
                    cycle_start_dt, end, billing_cycle_usage
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
            "cycle_reconciliation_hourly_total": cycle_reconciliation.get("hourly_derived_total"),
            "cycle_reconciliation_delta": cycle_reconciliation.get("delta"),
            "monthly_limit": monthly_limit,
            "avg_households_raw": avg_households_raw,
        }
