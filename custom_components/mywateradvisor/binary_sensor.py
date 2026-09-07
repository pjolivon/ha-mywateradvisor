"""Binary sensor platform for MyWaterAdvisor."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .sensor import _MyWaterAdvisorEntity
from .util import stable_entry_id


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MyWaterAdvisorLeakAlertSensor(coordinator, entry)])


class MyWaterAdvisorLeakAlertSensor(_MyWaterAdvisorEntity, BinarySensorEntity):
    """On when the portal has an active 'Suspected Leak' style alert."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_name = "Leak Alert"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{stable_entry_id(entry)}_leak_alert"

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        alerts = self.coordinator.data.get("alerts") or []
        return any(
            "leak" in (a.get("alertTypeName") or "").lower() for a in alerts if isinstance(a, dict)
        )

    @property
    def extra_state_attributes(self) -> dict:
        alerts = (self.coordinator.data or {}).get("alerts") or []
        leaks = [a for a in alerts if isinstance(a, dict) and "leak" in (a.get("alertTypeName") or "").lower()]
        return {"leak_alerts": leaks}
