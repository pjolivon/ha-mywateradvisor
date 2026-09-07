"""Shared helpers for MyWaterAdvisor."""
from __future__ import annotations

from datetime import date, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import slugify


def parse_flexible_date(value) -> date | None:
    """Parse a vacation start/end date in whatever shape the portal returns it."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        pass
    for fmt in ("%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def stable_entry_id(entry: ConfigEntry) -> str:
    """A per-account identifier that survives the config entry being removed
    and re-added — entry.entry_id does not; it's a fresh random ID every
    time. The config flow sets the entry's own unique_id to the account
    email (async_set_unique_id), so basing entity unique_ids and device
    identifiers on that instead keeps them — and their statistics, and any
    Energy Dashboard source pointing at them — intact across a remove/re-add,
    not just a restart.
    """
    return slugify(entry.unique_id) if entry.unique_id else entry.entry_id

