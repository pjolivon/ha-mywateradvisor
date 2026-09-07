"""Number platform for MyWaterAdvisor."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import MyWaterAdvisorError
from .const import DOMAIN
from .sensor import _MyWaterAdvisorEntity
from .util import stable_entry_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            MyWaterAdvisorBillingCycleLimitNumber(coordinator, entry),
            MyWaterAdvisorWaterPriceNumber(coordinator, entry),
        ]
    )


class MyWaterAdvisorBillingCycleLimitNumber(_MyWaterAdvisorEntity, NumberEntity):
    """Editable billing-cycle consumption budget, in gallons.

    To clear the limit entirely (rather than set it to 0), call the
    mywateradvisor.clear_billing_cycle_limit service instead.
    """

    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_native_min_value = 0
    _attr_native_max_value = 1000000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:tune"
    _attr_name = "Billing Cycle Limit"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_billing_cycle_limit"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("monthly_limit")

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.async_set_monthly_limit(value)
        except MyWaterAdvisorError as err:
            raise HomeAssistantError(f"Could not set billing cycle limit: {err}") from err
        await self.coordinator.async_request_refresh()


class MyWaterAdvisorWaterPriceNumber(_MyWaterAdvisorEntity, NumberEntity):
    """Editable $/gallon rate used to compute the cost backfill statistic
    (mywateradvisor:<account>_water_cost). Purely local — unlike the billing
    cycle limit above, this isn't backed by the portal (it doesn't report a
    dollar cost), so it's just persisted and read back by the coordinator.
    """

    _attr_native_unit_of_measurement = "USD/gal"
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.00000001
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:cash"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Price Per Gallon"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_water_price_per_gallon"

    @property
    def native_value(self) -> float:
        return self.coordinator.water_price_per_gallon

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_water_price_per_gallon(value)
