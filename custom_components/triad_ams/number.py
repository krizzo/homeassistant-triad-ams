# Touched by AI 2026-07-10 (Claude Code): new platform exposing the extended
# device settings reverse-engineered from packet captures.
"""
Number platform for Triad AMS.

Exposes per-output audio settings (balance, tone shelves, room EQ, volume
limits, delays, 2.1 crossover, test tone level) and per-input settings
(gain, delay) as Home Assistant number entities. Advanced entities are
disabled by default to keep the UI manageable on large matrices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfFrequency,
    UnitOfTime,
)
from homeassistant.exceptions import HomeAssistantError

from .connection import ROOM_EQ_BAND_COUNT
from .entity import TriadSettingsEntity
from .exceptions import TransientDeviceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import TriadCoordinator

_LOGGER = logging.getLogger(__name__)

_DB_UNIT = "dB"


@dataclass(frozen=True, kw_only=True)
class TriadNumberSpec:
    """Describes one Triad AMS number setting."""

    key: str
    name: str
    min_value: float
    max_value: float
    step: float
    unit: str | None = None
    icon: str | None = None
    mode: NumberMode = NumberMode.SLIDER
    enabled_default: bool = False
    getter: Callable[[TriadCoordinator, int], Awaitable[float]] | None = None
    setter: Callable[[TriadCoordinator, int, float], Awaitable[None]]


def _output_specs() -> list[TriadNumberSpec]:
    """Build the number specs for a single output channel."""
    specs = [
        TriadNumberSpec(
            key="balance",
            name="balance",
            min_value=-12,
            max_value=12,
            step=0.5,
            unit=_DB_UNIT,
            icon="mdi:pan-horizontal",
            enabled_default=True,
            getter=lambda c, ch: c.get_output_balance(ch),
            setter=lambda c, ch, v: c.set_output_balance(ch, v),
        ),
        TriadNumberSpec(
            key="bass_gain",
            name="bass",
            min_value=-12,
            max_value=12,
            step=0.5,
            unit=_DB_UNIT,
            icon="mdi:speaker",
            enabled_default=True,
            getter=lambda c, ch: c.get_output_shelf(ch, "low", "gain"),
            setter=lambda c, ch, v: c.set_output_shelf(ch, "low", "gain", v),
        ),
        TriadNumberSpec(
            key="treble_gain",
            name="treble",
            min_value=-12,
            max_value=12,
            step=0.5,
            unit=_DB_UNIT,
            icon="mdi:speaker",
            enabled_default=True,
            getter=lambda c, ch: c.get_output_shelf(ch, "high", "gain"),
            setter=lambda c, ch, v: c.set_output_shelf(ch, "high", "gain", v),
        ),
        TriadNumberSpec(
            key="bass_frequency",
            name="bass frequency",
            min_value=20,
            max_value=2000,
            step=1,
            unit=UnitOfFrequency.HERTZ,
            icon="mdi:sine-wave",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_output_shelf(ch, "low", "frequency"),
            setter=lambda c, ch, v: c.set_output_shelf(ch, "low", "frequency", v),
        ),
        TriadNumberSpec(
            key="treble_frequency",
            name="treble frequency",
            min_value=20,
            max_value=20000,
            step=1,
            unit=UnitOfFrequency.HERTZ,
            icon="mdi:sine-wave",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_output_shelf(ch, "high", "frequency"),
            setter=lambda c, ch, v: c.set_output_shelf(ch, "high", "frequency", v),
        ),
        TriadNumberSpec(
            key="bass_q",
            name="bass Q",
            min_value=0.1,
            max_value=15,
            step=0.01,
            icon="mdi:tune-variant",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_output_shelf(ch, "low", "q"),
            setter=lambda c, ch, v: c.set_output_shelf(ch, "low", "q", v),
        ),
        TriadNumberSpec(
            key="treble_q",
            name="treble Q",
            min_value=0.1,
            max_value=15,
            step=0.01,
            icon="mdi:tune-variant",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_output_shelf(ch, "high", "q"),
            setter=lambda c, ch, v: c.set_output_shelf(ch, "high", "q", v),
        ),
        TriadNumberSpec(
            key="max_volume",
            name="max volume",
            min_value=0,
            max_value=100,
            step=1,
            unit=PERCENTAGE,
            icon="mdi:volume-vibrate",
            getter=_get_max_volume_pct,
            setter=_set_max_volume_pct,
        ),
        TriadNumberSpec(
            key="turn_on_volume",
            name="turn-on volume",
            min_value=0,
            max_value=100,
            step=1,
            unit=PERCENTAGE,
            icon="mdi:volume-medium",
            getter=_get_turn_on_volume_pct,
            setter=_set_turn_on_volume_pct,
        ),
        TriadNumberSpec(
            key="delay",
            name="audio delay",
            min_value=0,
            max_value=80,
            step=1,
            unit=UnitOfTime.MILLISECONDS,
            icon="mdi:timer-outline",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_output_delay(ch),
            setter=lambda c, ch, v: c.set_output_delay(ch, int(v)),
        ),
        TriadNumberSpec(
            key="crossover_frequency",
            name="crossover frequency",
            min_value=20,
            max_value=2000,
            step=1,
            unit=UnitOfFrequency.HERTZ,
            icon="mdi:sine-wave",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_crossover_frequency(ch),
            setter=lambda c, ch, v: c.set_crossover_frequency(ch, int(v)),
        ),
        TriadNumberSpec(
            key="sub_volume_offset",
            name="sub volume offset",
            min_value=-12,
            max_value=0,
            step=0.5,
            unit=_DB_UNIT,
            icon="mdi:volume-low",
            getter=lambda c, ch: c.get_sub_volume_offset(ch),
            setter=lambda c, ch, v: c.set_sub_volume_offset(ch, v),
        ),
        TriadNumberSpec(
            key="test_tone_volume",
            name="test tone volume",
            min_value=-24,
            max_value=0,
            step=0.5,
            unit=_DB_UNIT,
            icon="mdi:volume-low",
            setter=lambda c, ch, v: c.set_test_tone_volume(ch, v),
        ),
    ]
    specs.extend(_room_eq_specs())
    return specs


def _room_eq_specs() -> list[TriadNumberSpec]:
    """Build the 12-band room EQ number specs for a single output channel."""

    def _make(band: int) -> list[TriadNumberSpec]:
        return [
            TriadNumberSpec(
                key=f"eq{band}_frequency",
                name=f"EQ band {band} frequency",
                min_value=20,
                max_value=20000,
                step=1,
                unit=UnitOfFrequency.HERTZ,
                icon="mdi:sine-wave",
                mode=NumberMode.BOX,
                getter=lambda c, ch, b=band: c.get_room_eq(ch, b, "frequency"),
                setter=lambda c, ch, v, b=band: c.set_room_eq(ch, b, "frequency", v),
            ),
            TriadNumberSpec(
                key=f"eq{band}_gain",
                name=f"EQ band {band} gain",
                min_value=-12,
                max_value=12,
                step=0.1,
                unit=_DB_UNIT,
                icon="mdi:equalizer",
                getter=lambda c, ch, b=band: c.get_room_eq(ch, b, "gain"),
                setter=lambda c, ch, v, b=band: c.set_room_eq(ch, b, "gain", v),
            ),
            TriadNumberSpec(
                key=f"eq{band}_q",
                name=f"EQ band {band} Q",
                min_value=0.5,
                max_value=15,
                step=0.1,
                icon="mdi:tune-variant",
                mode=NumberMode.BOX,
                getter=lambda c, ch, b=band: c.get_room_eq(ch, b, "q"),
                setter=lambda c, ch, v, b=band: c.set_room_eq(ch, b, "q", v),
            ),
        ]

    specs: list[TriadNumberSpec] = []
    for band in range(1, ROOM_EQ_BAND_COUNT + 1):
        specs.extend(_make(band))
    return specs


def _input_specs() -> list[TriadNumberSpec]:
    """Build the number specs for a single input channel."""
    return [
        TriadNumberSpec(
            key="gain",
            name="gain",
            min_value=-12,
            max_value=12,
            step=0.5,
            unit=_DB_UNIT,
            icon="mdi:volume-plus",
            enabled_default=True,
            getter=lambda c, ch: c.get_input_gain(ch),
            setter=lambda c, ch, v: c.set_input_gain(ch, v),
        ),
        TriadNumberSpec(
            key="delay",
            name="audio delay",
            min_value=0,
            max_value=80,
            step=1,
            unit=UnitOfTime.MILLISECONDS,
            icon="mdi:timer-outline",
            mode=NumberMode.BOX,
            getter=lambda c, ch: c.get_input_delay(ch),
            setter=lambda c, ch, v: c.set_input_delay(ch, int(v)),
        ),
    ]


async def _get_max_volume_pct(c: TriadCoordinator, ch: int) -> float:
    """Read the max volume as a 0..100 percentage."""
    return await c.get_output_max_volume(ch) * 100


async def _set_max_volume_pct(c: TriadCoordinator, ch: int, value: float) -> None:
    """Write the max volume from a 0..100 percentage."""
    await c.set_output_max_volume(ch, value / 100)


async def _get_turn_on_volume_pct(c: TriadCoordinator, ch: int) -> float:
    """Read the turn-on volume as a 0..100 percentage."""
    return await c.get_output_turn_on_volume(ch) * 100


async def _set_turn_on_volume_pct(c: TriadCoordinator, ch: int, value: float) -> None:
    """Write the turn-on volume from a 0..100 percentage."""
    await c.set_output_turn_on_volume(ch, value / 100)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Triad AMS number entities from a config entry."""
    coordinator: TriadCoordinator = entry.runtime_data
    active_outputs: list[int] = entry.options.get("active_outputs", [])
    active_inputs: list[int] = entry.options.get("active_inputs", [])

    entities: list[TriadNumber] = [
        TriadNumber(entry, coordinator, "output", ch, spec)
        for ch in sorted(active_outputs)
        for spec in _output_specs()
    ]
    entities.extend(
        TriadNumber(entry, coordinator, "input", ch, spec)
        for ch in sorted(active_inputs)
        for spec in _input_specs()
    )
    async_add_entities(entities)


class TriadNumber(TriadSettingsEntity, NumberEntity):
    """A number entity backed by a Triad AMS device setting."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: TriadCoordinator,
        channel_type: str,
        channel: int,
        spec: TriadNumberSpec,
    ) -> None:
        """Initialize the number entity from its spec."""
        super().__init__(
            entry,
            coordinator,
            f"{channel_type}_{channel}_{spec.key}",
            f"{channel_type.capitalize()} {channel} {spec.name}",
        )
        self._channel = channel
        self._spec = spec
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_mode = spec.mode
        self._attr_icon = spec.icon
        self._attr_entity_registry_enabled_default = spec.enabled_default
        self._attr_native_value = None

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the device and update optimistically."""
        try:
            await self._spec.setter(self.coordinator, self._channel, value)
        except (OSError, TransientDeviceError) as err:
            msg = f"Failed to set {self.name}: {err}"
            raise HomeAssistantError(msg) from err
        self._attr_native_value = value
        self.async_write_ha_state()

    async def _async_refresh_value(self) -> None:
        """Read the current value from the device."""
        if self._spec.getter is None:
            return
        self._attr_native_value = await self._spec.getter(
            self.coordinator, self._channel
        )
