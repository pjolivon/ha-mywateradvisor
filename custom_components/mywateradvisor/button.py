"""Button platform for MyWaterAdvisor."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .sensor import _MyWaterAdvisorEntity
from .util import stable_entry_id


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MyWaterAdvisorResyncButton(coordinator, entry)])


class MyWaterAdvisorResyncButton(_MyWaterAdvisorEntity, ButtonEntity):
    """One-time recovery for consumption data older than this coordinator's
    own history — see MyWaterAdvisorCoordinator.async_resync_statistics.

    Disabled by default and diagnostic-categorized: this is only needed
    right after a reported gap (a new install, an outage, a fix like
    1.3.0's), not a routine control, so it shouldn't clutter the device
    card for the common case. Mirrors the mywateradvisor.resync_statistics
    service for anyone who'd rather click than find it under
    Developer Tools > Actions.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:database-sync-outline"
    _attr_name = "Resync Statistics"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_resync_statistics"

    async def async_press(self) -> None:
        await self.coordinator.async_resync_statistics()
