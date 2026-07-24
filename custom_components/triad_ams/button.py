# Touched by AI 2026-07-10 (Claude Code): new platform exposing the extended
# device settings reverse-engineered from packet captures.
"""
Button platform for Triad AMS.

Exposes a device reboot button. Factory reset is deliberately not exposed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .entity import TriadSettingsEntity
from .exceptions import TransientDeviceError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import TriadCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Triad AMS button entities from a config entry."""
    coordinator: TriadCoordinator = entry.runtime_data
    async_add_entities([TriadRebootButton(entry, coordinator)])


class TriadRebootButton(TriadSettingsEntity, ButtonEntity):
    """Button entity that reboots the Triad AMS device."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, coordinator: TriadCoordinator) -> None:
        """Initialize the reboot button."""
        super().__init__(entry, coordinator, "reboot", "Reboot")

    async def async_press(self) -> None:
        """Send the reboot command to the device."""
        try:
            await self.coordinator.reboot_device()
        except (OSError, TransientDeviceError) as err:
            msg = f"Failed to reboot device: {err}"
            raise HomeAssistantError(msg) from err
