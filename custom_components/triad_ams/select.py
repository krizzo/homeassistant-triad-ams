# Touched by AI 2026-07-10 (Claude Code): new platform exposing the extended
# device settings reverse-engineered from packet captures.
"""
Select platform for Triad AMS.

Exposes the per-output DSP mode (stereo / mono / DSP bypass / 2.1 variants /
test signal) and the 2.1 crossover filter type as select entities.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .connection import (
    CROSSOVER_TYPES,
    OUTPUT_MODE_21_MONO,
    OUTPUT_MODE_21_STEREO,
    OUTPUT_MODE_DSP_BYPASS,
    OUTPUT_MODE_MONO,
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_TEST,
)
from .entity import TriadSettingsEntity
from .exceptions import TransientDeviceError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import TriadCoordinator

_LOGGER = logging.getLogger(__name__)

OUTPUT_MODE_LABELS: dict[str, int] = {
    "Stereo": OUTPUT_MODE_STEREO,
    "Mono": OUTPUT_MODE_MONO,
    "DSP Bypass Stereo": OUTPUT_MODE_DSP_BYPASS,
    "2.1 Stereo": OUTPUT_MODE_21_STEREO,
    "2.1 Mono": OUTPUT_MODE_21_MONO,
    "Test Signal": OUTPUT_MODE_TEST,
}

CROSSOVER_TYPE_LABELS: dict[str, int] = {
    "Butterworth 12 dB/Oct": CROSSOVER_TYPES["butterworth_12"],
    "Butterworth 24 dB/Oct": CROSSOVER_TYPES["butterworth_24"],
    "Butterworth 48 dB/Oct": CROSSOVER_TYPES["butterworth_48"],
    "Linkwitz-Riley 12 dB/Oct": CROSSOVER_TYPES["linkwitz_riley_12"],
    "Linkwitz-Riley 24 dB/Oct": CROSSOVER_TYPES["linkwitz_riley_24"],
    "Linkwitz-Riley 48 dB/Oct": CROSSOVER_TYPES["linkwitz_riley_48"],
}


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Triad AMS select entities from a config entry."""
    coordinator: TriadCoordinator = entry.runtime_data
    active_outputs: list[int] = entry.options.get("active_outputs", [])

    entities: list[TriadSelect] = []
    for ch in sorted(active_outputs):
        entities.append(TriadOutputModeSelect(entry, coordinator, ch))
        entities.append(TriadCrossoverTypeSelect(entry, coordinator, ch))
    async_add_entities(entities)


class TriadSelect(TriadSettingsEntity, SelectEntity):
    """Base select entity mapping labels to device values."""

    _attr_entity_category = EntityCategory.CONFIG
    _labels: dict[str, int]

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: TriadCoordinator,
        channel: int,
        key: str,
        name: str,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(
            entry, coordinator, f"output_{channel}_{key}", f"Output {channel} {name}"
        )
        self._channel = channel
        self._attr_options = list(self._labels)
        self._attr_current_option = None

    def _label_for_value(self, value: int | None) -> str | None:
        """Return the label for a device value, or None if unknown."""
        for label, val in self._labels.items():
            if val == value:
                return label
        return None

    async def _write_value(self, value: int) -> None:
        """Write the device value. Overridden by subclasses."""
        raise NotImplementedError

    async def _read_value(self) -> int | None:
        """Read the device value. Overridden by subclasses."""
        raise NotImplementedError

    async def async_select_option(self, option: str) -> None:
        """Write the selected option to the device."""
        try:
            await self._write_value(self._labels[option])
        except (OSError, TransientDeviceError) as err:
            msg = f"Failed to set {self.name}: {err}"
            raise HomeAssistantError(msg) from err
        self._attr_current_option = option
        self.async_write_ha_state()

    async def _async_refresh_value(self) -> None:
        """Read the current option from the device."""
        self._attr_current_option = self._label_for_value(await self._read_value())


class TriadOutputModeSelect(TriadSelect):
    """Select entity for the per-output DSP mode."""

    _labels = OUTPUT_MODE_LABELS
    _attr_icon = "mdi:surround-sound"

    def __init__(
        self, entry: ConfigEntry, coordinator: TriadCoordinator, channel: int
    ) -> None:
        """Initialize the output mode select."""
        super().__init__(entry, coordinator, channel, "mode", "mode")

    async def _write_value(self, value: int) -> None:
        await self.coordinator.set_output_mode(self._channel, value)

    async def _read_value(self) -> int | None:
        return await self.coordinator.get_output_mode(self._channel)


class TriadCrossoverTypeSelect(TriadSelect):
    """Select entity for the per-output 2.1 crossover filter type."""

    _labels = CROSSOVER_TYPE_LABELS
    _attr_icon = "mdi:swap-vertical-variant"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, entry: ConfigEntry, coordinator: TriadCoordinator, channel: int
    ) -> None:
        """Initialize the crossover type select."""
        super().__init__(
            entry, coordinator, channel, "crossover_type", "crossover type"
        )

    async def _write_value(self, value: int) -> None:
        await self.coordinator.set_crossover_type(self._channel, value)

    async def _read_value(self) -> int | None:
        return await self.coordinator.get_crossover_type(self._channel)
