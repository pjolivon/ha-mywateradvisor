"""Switch platform for MyWaterAdvisor."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import MyWaterAdvisorError
from .const import DOMAIN, VACATION_DEFAULT_DAYS
from .sensor import _MyWaterAdvisorEntity
from .util import parse_flexible_date, stable_entry_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MyWaterAdvisorVacationModeSwitch(coordinator, entry)])


class MyWaterAdvisorVacationModeSwitch(_MyWaterAdvisorEntity, SwitchEntity):
    """Toggle vacation mode: on schedules a vacation starting today, off cancels it.

    For a specific date range instead of the default length, call the
    mywateradvisor.schedule_vacation service directly.
    """

    _attr_icon = "mdi:bag-suitcase-outline"
    _attr_name = "Vacation Mode"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_vacation_mode"

    def _current_vacation(self) -> dict | None:
        vacations = (self.coordinator.data or {}).get("vacations") or []
        today = dt_util.now().date()
        for vacation in vacations:
            if not isinstance(vacation, dict):
                continue
            start = parse_flexible_date(vacation.get("startDate"))
            end = parse_flexible_date(vacation.get("endDate"))
            if start and end and start <= today <= end:
                return vacation
        return None

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return self._current_vacation() is not None

    @property
    def extra_state_attributes(self) -> dict:
        vacation = self._current_vacation()
        if not vacation:
            return {}
        return {
            "vacation_id": vacation.get("vacationID"),
            "start_date": vacation.get("startDate"),
            "end_date": vacation.get("endDate"),
            "consumption_daily_limit": vacation.get("consumptionDailyLimit"),
        }

    async def async_turn_on(self, **kwargs) -> None:
        if self._current_vacation() is not None:
            # Already active for today — the portal rejects a second
            # overlapping vacation with a 400 "Vacation already exists".
            return
        start = dt_util.start_of_local_day()
        end = start + timedelta(days=VACATION_DEFAULT_DAYS)
        try:
            await self.coordinator.client.async_create_vacation(start, end, None)
        except MyWaterAdvisorError as err:
            raise HomeAssistantError(f"Could not enable vacation mode: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        vacation = self._current_vacation()
        vacation_id = vacation.get("vacationID") if vacation else None
        if vacation_id is None:
            return
        try:
            await self.coordinator.client.async_cancel_vacation(vacation_id)
        except MyWaterAdvisorError as err:
            raise HomeAssistantError(f"Could not disable vacation mode: {err}") from err
        await self.coordinator.async_request_refresh()
