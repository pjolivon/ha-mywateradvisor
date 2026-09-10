"""Integration setup for MyWaterAdvisor."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import MyWaterAdvisorError
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from .coordinator import STORAGE_VERSION, MyWaterAdvisorCoordinator
from .util import parse_flexible_date, stable_entry_id

PLATFORMS = ["sensor", "binary_sensor", "number", "switch"]

SERVICE_SCHEDULE_VACATION = "schedule_vacation"
SERVICE_CANCEL_VACATION = "cancel_vacation"
SERVICE_CLEAR_BILLING_CYCLE_LIMIT = "clear_billing_cycle_limit"

# Optional on every service below: limit the action to one account's device
# instead of all configured MyWaterAdvisor accounts (the default).
_TARGET_DEVICE_FIELD = {vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string])}

SCHEDULE_VACATION_SCHEMA = vol.Schema(
    {
        **_TARGET_DEVICE_FIELD,
        vol.Required("start_date"): cv.date,
        vol.Required("end_date"): cv.date,
        vol.Optional("daily_limit"): vol.Coerce(float),
    }
)
CANCEL_VACATION_SCHEMA = vol.Schema(
    {**_TARGET_DEVICE_FIELD, vol.Optional("vacation_id"): vol.Any(str, int)}
)
CLEAR_BILLING_CYCLE_LIMIT_SCHEMA = vol.Schema(_TARGET_DEVICE_FIELD)


def _target_coordinators(hass: HomeAssistant, call: ServiceCall) -> list[MyWaterAdvisorCoordinator]:
    """Resolve the service call's device_id target to its coordinator(s).

    Falls back to every configured account when no device_id is given, so
    the previous (untargeted) behavior is unchanged for existing automations.
    """
    coordinators: dict[str, MyWaterAdvisorCoordinator] = hass.data.get(DOMAIN, {})
    device_ids = call.data.get("device_id")
    if not device_ids:
        return list(coordinators.values())

    device_registry = dr.async_get(hass)
    entry_ids: set[str] = set()
    for device_id in device_ids:
        device = device_registry.async_get(device_id)
        if device:
            entry_ids.update(device.config_entries)

    return [coordinators[entry_id] for entry_id in entry_ids if entry_id in coordinators]


def _find_current_vacation_id(coordinator: MyWaterAdvisorCoordinator):
    vacations = (coordinator.data or {}).get("vacations") or []
    today = dt_util.now().date()
    for vacation in vacations:
        if not isinstance(vacation, dict):
            continue
        start = parse_flexible_date(vacation.get("startDate"))
        end = parse_flexible_date(vacation.get("endDate"))
        if start and end and start <= today <= end:
            return vacation.get("vacationID")
    return None


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SCHEDULE_VACATION):
        return

    async def handle_schedule_vacation(call: ServiceCall) -> None:
        start_dt = dt_util.start_of_local_day(call.data["start_date"])
        end_dt = dt_util.start_of_local_day(call.data["end_date"])
        daily_limit = call.data.get("daily_limit")
        for coordinator in _target_coordinators(hass, call):
            try:
                await coordinator.client.async_create_vacation(start_dt, end_dt, daily_limit)
            except MyWaterAdvisorError as err:
                raise HomeAssistantError(f"Could not schedule vacation: {err}") from err
            await coordinator.async_request_refresh()

    async def handle_cancel_vacation(call: ServiceCall) -> None:
        vacation_id = call.data.get("vacation_id")
        for coordinator in _target_coordinators(hass, call):
            target_id = vacation_id if vacation_id is not None else _find_current_vacation_id(coordinator)
            if target_id is None:
                continue
            try:
                await coordinator.client.async_cancel_vacation(target_id)
            except MyWaterAdvisorError as err:
                raise HomeAssistantError(f"Could not cancel vacation: {err}") from err
            await coordinator.async_request_refresh()

    async def handle_clear_billing_cycle_limit(call: ServiceCall) -> None:
        for coordinator in _target_coordinators(hass, call):
            try:
                await coordinator.client.async_clear_monthly_limit()
            except MyWaterAdvisorError as err:
                raise HomeAssistantError(f"Could not clear billing cycle limit: {err}") from err
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_VACATION, handle_schedule_vacation, schema=SCHEDULE_VACATION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_VACATION, handle_cancel_vacation, schema=CANCEL_VACATION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_BILLING_CYCLE_LIMIT,
        handle_clear_billing_cycle_limit,
        schema=CLEAR_BILLING_CYCLE_LIMIT_SCHEMA,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    stable_id = stable_entry_id(entry)
    coordinator = MyWaterAdvisorCoordinator(hass, stable_id, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SCHEDULE_VACATION)
            hass.services.async_remove(DOMAIN, SERVICE_CANCEL_VACATION)
            hass.services.async_remove(DOMAIN, SERVICE_CLEAR_BILLING_CYCLE_LIMIT)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the coordinator's persisted store when the entry is fully removed.

    A remove/re-add (e.g. for reauth) will lose the running total and
    re-seed from INITIAL_BACKFILL_LOOKBACK, same as a first install — this
    only runs on an actual "Delete" from the UI, not a reload/disable.
    """
    stable_id = stable_entry_id(entry)
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}_{stable_id}").async_remove()
