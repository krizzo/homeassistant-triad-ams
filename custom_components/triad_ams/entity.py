# Touched by AI 2026-07-10 (Claude Code): new file for the extended
# settings entities (number/select/switch/button/binary_sensor platforms).
"""
Shared base entity for Triad AMS settings entities.

Settings entities (number/select/switch/...) execute device commands through
the coordinator queue. They read their value once when added to Home
Assistant, update optimistically after successful writes, and can be
re-synced on demand via ``homeassistant.update_entity``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .exceptions import TransientDeviceError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import TriadCoordinator

_LOGGER = logging.getLogger(__name__)


class TriadSettingsEntity(Entity):
    """Base class for Triad AMS settings entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: TriadCoordinator,
        unique_suffix: str,
        name: str,
    ) -> None:
        """
        Initialize a settings entity.

        Args:
            entry: The config entry owning this entity.
            coordinator: The command coordinator for device I/O.
            unique_suffix: Suffix appended to the entry id for the unique id.
            name: Entity name suffix (device name is prepended by HA).

        """
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_available = coordinator.is_available
        # Group all entities under the single device for this config entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Triad",
            "model": "Audio Matrix",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to availability changes and schedule an initial read."""
        self.async_on_remove(
            self.coordinator.add_availability_listener(self._handle_availability)
        )
        # Fetch the initial value without blocking platform setup
        self.async_schedule_update_ha_state(force_refresh=True)

    def _handle_availability(self, *, is_available: bool) -> None:
        """Handle coordinator availability changes."""
        if self._attr_available == is_available:
            return
        self._attr_available = is_available
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh the entity value from the device (best-effort)."""
        try:
            await self._async_refresh_value()
        except TransientDeviceError:
            _LOGGER.debug("Transient error refreshing %s; skipping", self.entity_id)
        except OSError:
            _LOGGER.debug(
                "Failed to refresh %s from device", self.entity_id, exc_info=True
            )

    async def _async_refresh_value(self) -> None:
        """Read the current value from the device. Overridden by platforms."""
