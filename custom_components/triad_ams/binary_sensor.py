# Touched by AI 2026-07-10 (Claude Code): new platform exposing the extended
# device settings reverse-engineered from packet captures.
"""
Binary sensor platform for Triad AMS.

Exposes per-input audio sense (signal detection) as binary sensors. These
are disabled by default; when enabled they poll the device periodically
through the coordinator's paced command queue.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

from .entity import TriadSettingsEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import TriadCoordinator

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Triad AMS binary sensor entities from a config entry."""
    coordinator: TriadCoordinator = entry.runtime_data
    active_inputs: list[int] = entry.options.get("active_inputs", [])
    async_add_entities(
        TriadAudioSenseBinarySensor(entry, coordinator, ch)
        for ch in sorted(active_inputs)
    )


class TriadAudioSenseBinarySensor(TriadSettingsEntity, BinarySensorEntity):
    """Binary sensor reporting whether audio is detected on an input."""

    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_should_poll = True

    def __init__(
        self, entry: ConfigEntry, coordinator: TriadCoordinator, channel: int
    ) -> None:
        """Initialize the audio sense binary sensor."""
        super().__init__(
            entry,
            coordinator,
            f"input_{channel}_audio_sense",
            f"Input {channel} audio detected",
        )
        self._channel = channel
        self._attr_is_on = None

    async def _async_refresh_value(self) -> None:
        """Read the audio detection state from the device."""
        self._attr_is_on = await self.coordinator.get_input_audio_sense(self._channel)
