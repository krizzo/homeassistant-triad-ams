# Touched by AI 2026-07-10 (Claude Code): new platform exposing the extended
# device settings reverse-engineered from packet captures.
"""
Switch platform for Triad AMS.

Exposes per-output loudness compensation, room EQ lock, and the built-in
test tone (implemented by switching the output DSP mode to "Test Signal").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .connection import OUTPUT_MODE_STEREO, OUTPUT_MODE_TEST
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
    """Set up Triad AMS switch entities from a config entry."""
    coordinator: TriadCoordinator = entry.runtime_data
    active_outputs: list[int] = entry.options.get("active_outputs", [])

    entities: list[TriadSwitch] = []
    for ch in sorted(active_outputs):
        entities.append(TriadLoudnessSwitch(entry, coordinator, ch))
        entities.append(TriadRoomEqLockSwitch(entry, coordinator, ch))
        entities.append(TriadTestToneSwitch(entry, coordinator, ch))
    async_add_entities(entities)


class TriadSwitch(TriadSettingsEntity, SwitchEntity):
    """Base switch entity for a per-output boolean setting."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: TriadCoordinator,
        channel: int,
        key: str,
        name: str,
    ) -> None:
        """Initialize the switch entity."""
        super().__init__(
            entry, coordinator, f"output_{channel}_{key}", f"Output {channel} {name}"
        )
        self._channel = channel
        self._attr_is_on = None

    async def _write_state(self, *, on: bool) -> None:
        """Write the state to the device. Overridden by subclasses."""
        raise NotImplementedError

    async def _read_state(self) -> bool:
        """Read the state from the device. Overridden by subclasses."""
        raise NotImplementedError

    async def _async_set_state(self, *, on: bool) -> None:
        """Write the state and update optimistically."""
        try:
            await self._write_state(on=on)
        except (OSError, TransientDeviceError) as err:
            msg = f"Failed to set {self.name}: {err}"
            raise HomeAssistantError(msg) from err
        self._attr_is_on = on
        self.async_write_ha_state()

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set_state(on=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set_state(on=False)

    async def _async_refresh_value(self) -> None:
        """Read the current state from the device."""
        self._attr_is_on = await self._read_state()


class TriadLoudnessSwitch(TriadSwitch):
    """Switch entity for per-output loudness compensation."""

    _attr_icon = "mdi:surround-sound"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self, entry: ConfigEntry, coordinator: TriadCoordinator, channel: int
    ) -> None:
        """Initialize the loudness switch."""
        super().__init__(entry, coordinator, channel, "loudness", "loudness")

    async def _write_state(self, *, on: bool) -> None:
        await self.coordinator.set_output_loudness(self._channel, on=on)

    async def _read_state(self) -> bool:
        return await self.coordinator.get_output_loudness(self._channel)


class TriadRoomEqLockSwitch(TriadSwitch):
    """Switch entity for the per-output room EQ lock."""

    _attr_icon = "mdi:lock-outline"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, entry: ConfigEntry, coordinator: TriadCoordinator, channel: int
    ) -> None:
        """Initialize the room EQ lock switch."""
        super().__init__(entry, coordinator, channel, "eq_lock", "room EQ lock")

    async def _write_state(self, *, on: bool) -> None:
        await self.coordinator.set_room_eq_lock(self._channel, locked=on)

    async def _read_state(self) -> bool:
        return await self.coordinator.get_room_eq_lock(self._channel)


class TriadTestToneSwitch(TriadSwitch):
    """
    Switch entity for the per-output test tone.

    The device has no discrete test tone command; the tone is enabled by
    setting the output DSP mode to "Test Signal". Turning the switch off
    restores the mode that was active before the tone was enabled (falling
    back to stereo).
    """

    _attr_icon = "mdi:waveform"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, entry: ConfigEntry, coordinator: TriadCoordinator, channel: int
    ) -> None:
        """Initialize the test tone switch."""
        super().__init__(entry, coordinator, channel, "test_tone", "test tone")
        self._previous_mode: int | None = None

    async def _write_state(self, *, on: bool) -> None:
        if on:
            # Remember the current mode so it can be restored afterwards
            current = await self.coordinator.get_output_mode(self._channel)
            if current is not None and current != OUTPUT_MODE_TEST:
                self._previous_mode = current
            await self.coordinator.set_output_mode(self._channel, OUTPUT_MODE_TEST)
        else:
            restore = self._previous_mode
            if restore is None or restore == OUTPUT_MODE_TEST:
                restore = OUTPUT_MODE_STEREO
            await self.coordinator.set_output_mode(self._channel, restore)

    async def _read_state(self) -> bool:
        mode = await self.coordinator.get_output_mode(self._channel)
        return mode == OUTPUT_MODE_TEST
