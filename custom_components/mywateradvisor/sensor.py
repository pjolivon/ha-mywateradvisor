"""Sensor platform for MyWaterAdvisor."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PORTAL_ROOT_URL
from .util import stable_entry_id


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            MyWaterAdvisorTotalSensor(coordinator, entry),
            MyWaterAdvisorDailySensor(coordinator, entry),
            MyWaterAdvisorLastReadingSensor(coordinator, entry),
            MyWaterAdvisorMeterSerialSensor(coordinator, entry),
            MyWaterAdvisorServiceAddressSensor(coordinator, entry),
            MyWaterAdvisorDebugSensor(coordinator, entry),
            MyWaterAdvisorForecastSensor(coordinator, entry),
            MyWaterAdvisorForecastedCostSensor(coordinator, entry),
            MyWaterAdvisorAlertsSensor(coordinator, entry),
            MyWaterAdvisorBillingCycleUsageSensor(coordinator, entry),
            MyWaterAdvisorNeighborhoodAverageSensor(coordinator, entry),
        ]
    )


class _MyWaterAdvisorEntity(CoordinatorEntity):
    """Shared device grouping for all MyWaterAdvisor entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        # Name is deliberately static (not derived from meter_sn/coordinator
        # data): a name that can change across reloads causes Home Assistant
        # to attempt an entity_id migration for every entity on this device,
        # which collides with the previous entity_id and leaves duplicate,
        # orphaned registry entries — this is what broke the Energy
        # dashboard's water cost tracking (it kept losing continuity with
        # the renamed source entity and restarting from $0). The serial is
        # already exposed via the "Meter Serial Number" diagnostic sensor.
        return DeviceInfo(
            identifiers={(DOMAIN, stable_entry_id(self._entry))},
            name="Water Meter",
            manufacturer="Master Meter",
            model="Harmony Encore",
            configuration_url=PORTAL_ROOT_URL,
        )


class MyWaterAdvisorTotalSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Running lifetime total of real water consumption, in gallons.

    This entity's live state is a monotonic counter, so it stays
    ``total_increasing`` and lets HA's recorder compile its long-term
    statistics natively from the entity's recorded state history. The
    integration deliberately does NOT manually import into this same
    statistic_id: ``async_import_statistics`` writes straight into the
    long-term ``statistics`` table, but HA's own total_increasing compiler
    tracks continuity via ``statistics_short_term`` — with no bridging
    short-term rows it independently restarts the series at sum=0, decoupled
    from live state. That collision silently corrupted the Energy dashboard
    twice (the -1,129.8 gal dip on 2026-09-06), so the manual backfill was
    removed entirely.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water"
    _attr_name = "Total Usage"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_total_water"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data["total"]


class MyWaterAdvisorDailySensor(_MyWaterAdvisorEntity, SensorEntity):
    """Today's water consumption, in gallons. Resets at local midnight.

    Uses ``TOTAL`` with an explicit ``last_reset`` (the utility_meter helper's
    pattern) rather than ``total_increasing``: this counter drops to 0 by
    design every local midnight, and ``total_increasing`` would treat that
    expected daily rollover as an unexpected meter reset. ``last_reset`` marks
    the rollover as intentional so HA's statistics engine doesn't flag it as
    an anomaly.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water-outline"
    _attr_name = "Daily Usage"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_daily_water"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data["daily_total"]

    @property
    def last_reset(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("daily_reset_at")


class MyWaterAdvisorLastReadingSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Timestamp of the most recent real (non-estimated) meter reading."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-outline"
    _attr_name = "Last Reading Time"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_last_reading_time"

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data["last_reading_time"]


class MyWaterAdvisorMeterSerialSensor(_MyWaterAdvisorEntity, SensorEntity):
    """The physical meter's serial number."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:identifier"
    _attr_name = "Serial Number"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_meter_serial"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("meter_info", {}).get("meterSn")


class MyWaterAdvisorServiceAddressSensor(_MyWaterAdvisorEntity, SensorEntity):
    """The service address billed to this meter."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:map-marker"
    _attr_name = "Service Address"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_service_address"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("meter_info", {}).get("fullAddress")


class MyWaterAdvisorDebugSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Diagnostic sensor exposing the raw last poll, for troubleshooting."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bug-outline"
    _attr_name = "Debug"
    _attr_entity_registry_enabled_default = False
    # The raw payload can approach HA's per-attribute recorder size limit and
    # would otherwise be written to the database every poll for no benefit.
    _unrecorded_attributes = frozenset({"raw_rows"})

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_debug"

    @property
    def native_value(self) -> str:
        rows = self.coordinator.data.get("raw_rows", []) if self.coordinator.data else []
        return f"{len(rows)} rows"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "last_update_success": self.coordinator.last_update_success,
            "raw_rows": data.get("raw_rows", []),
        }


class MyWaterAdvisorForecastSensor(_MyWaterAdvisorEntity, SensorEntity):
    """The portal's own end-of-billing-cycle usage forecast, in gallons."""

    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_name = "Billing Cycle Forecast"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_forecast"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get("forecast_gallons")
        # The portal's own app rounds this to whole gallons (.toFixed(0)) —
        # match that rather than surfacing floating-point noise like
        # 10688.7999999999.
        return round(value) if isinstance(value, (int, float)) else value


class MyWaterAdvisorForecastedCostSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Estimated cost of the forecasted end-of-billing-cycle usage.

    Computed locally (forecast_gallons * water_price_per_gallon) since the
    portal never reports a dollar figure — same rate as the cost backfill
    statistic, kept in sync via the "Price Per Gallon" number entity.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash-multiple"
    _attr_name = "Forecasted Cost"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_forecasted_cost"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        forecast_gallons = self.coordinator.data.get("forecast_gallons")
        if not isinstance(forecast_gallons, (int, float)):
            return None
        return round(forecast_gallons * self.coordinator.water_price_per_gallon, 2)


class MyWaterAdvisorAlertsSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Count of active portal alerts (leak, budget limit, vacation usage, ...)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"
    _attr_name = "Active Alerts"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_alerts_count"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return len(self.coordinator.data.get("alerts") or [])

    @property
    def extra_state_attributes(self) -> dict:
        alerts = (self.coordinator.data or {}).get("alerts") or []
        return {
            "alerts": [
                {"type": a.get("alertTypeName"), "time": a.get("alertTime")}
                for a in alerts
                if isinstance(a, dict)
            ]
        }


class MyWaterAdvisorBillingCycleUsageSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Consumption so far in the current billing cycle, in gallons."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:calendar-range"
    _attr_name = "Billing Cycle Usage"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_billing_cycle_usage"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("billing_cycle_usage")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "billing_cycle_start": data.get("billing_cycle_start"),
            "billing_cycle_end": data.get("billing_cycle_end"),
        }


class MyWaterAdvisorNeighborhoodAverageSensor(_MyWaterAdvisorEntity, SensorEntity):
    """Neighborhood-comparison consumption figure from the portal, most recent
    completed month. Confirmed shape: [{"avgCons": <neighborhood gallons>,
    "ConsumerCons": <your gallons>, "Month": int, "Year": int}, ...].
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:home-group"
    _attr_name = "Neighborhood Average Usage"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_neighborhood_average"

    def _latest_entry(self) -> dict | None:
        raw = (self.coordinator.data or {}).get("avg_households_raw")
        items = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else None)
        if not items:
            return None
        last = items[-1]
        return last if isinstance(last, dict) else None

    @property
    def native_value(self) -> float | None:
        entry = self._latest_entry()
        if not entry:
            return None
        value = entry.get("avgCons")
        return round(value) if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict:
        entry = self._latest_entry() or {}
        return {
            "your_usage": entry.get("ConsumerCons"),
            "month": entry.get("Month"),
            "year": entry.get("Year"),
        }
