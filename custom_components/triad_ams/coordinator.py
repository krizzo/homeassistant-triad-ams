# Touched by AI 2026-07-10 (Claude Code): added passthroughs for the extended
# settings API (input/output controls, DSP, room EQ, crossover, device-level).
"""
Coordinator for Triad AMS.

Fresh, minimal implementation that:
- Sequences commands through a single worker
- Enforces a minimum delay between commands
- Avoids race conditions via a single queue
- Drops transport on device-side errors (raised by connection)
- Propagates errors to callers without internal retries
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .models import TriadAmsOutput

from .connection import TriadConnection
from .const import CONNECTION_TIMEOUT, NETWORK_EXCEPTIONS, SHUTDOWN_TIMEOUT
from .exceptions import TransientDeviceError

_LOGGER = logging.getLogger(__name__)


@dataclass
class _Command:
    """A queued coordinator command."""

    op: Callable[[TriadConnection], Awaitable[Any]]
    future: asyncio.Future


@dataclass
class TriadCoordinatorConfig:
    """Configuration for TriadCoordinator initialization."""

    host: str
    port: int
    input_count: int
    min_send_interval: float = 0.15
    poll_interval: float = 30.0
    protocol_debug: bool = False


class TriadCoordinator:
    """Single-queue, single-worker command coordinator."""

    def __init__(
        self,
        config: TriadCoordinatorConfig,
        *,
        connection: TriadConnection | None = None,
    ) -> None:
        """Initialize a paced, single-worker queue."""
        host_val = config.host
        port_val = config.port
        input_count_val = config.input_count
        min_send_interval = config.min_send_interval
        poll_interval = config.poll_interval

        self._host = host_val
        self._port = port_val
        self._input_count = input_count_val
        self._conn = (
            connection
            if connection is not None
            else TriadConnection(
                host_val, port_val, protocol_debug=config.protocol_debug
            )
        )
        self._queue: asyncio.Queue[_Command] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._last_send_time: float = 0.0
        self._min_send_interval = max(0.0, min_send_interval)
        self._poll_interval = max(1.0, poll_interval)
        # Weak set of outputs to poll; avoids retaining entities
        self._outputs: weakref.WeakSet[TriadAmsOutput] = weakref.WeakSet()
        self._poll_index: int = 0
        # Track active outputs per zone (zone -> set of output numbers)
        # Zones are 1-based and mapped in groups of 8 outputs (clamped 1..3).
        self._zone_active_outputs: dict[int, set[int]] = {1: set(), 2: set(), 3: set()}
        # Availability tracking (Silver requirement)
        self._available: bool = True
        self._availability_listeners: weakref.WeakSet[Callable[[bool], None]] = (
            weakref.WeakSet()
        )
        # Track input link unsubscribe functions for cleanup
        self._input_link_unsubs: list[Callable[[], None]] = []

    @property
    def input_link_unsubs(self) -> list[Callable[[], None]]:
        """Return the list of input link unsubscribe functions."""
        return self._input_link_unsubs

    @property
    def input_count(self) -> int:
        """Public accessor for the configured input count."""
        return self._input_count

    @property
    def host(self) -> str:
        """Return the host address of the coordinator."""
        return self._host

    @property
    def port(self) -> int:
        """Return the port number of the coordinator."""
        return self._port

    @property
    def outputs(self) -> weakref.WeakSet[TriadAmsOutput]:
        """Return the weak set of registered outputs."""
        return self._outputs

    @property
    def is_available(self) -> bool:
        """Return True if the coordinator is available (connected to device)."""
        return self._available

    def add_availability_listener(
        self, callback: Callable[[bool], None]
    ) -> Callable[[], None]:
        """
        Register callback for availability changes.

        Returns an unsubscribe function.
        """
        self._availability_listeners.add(callback)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._availability_listeners.discard(callback)

        return _unsub

    def add_input_link_unsub(self, unsub: Callable[[], None]) -> None:
        """Register an input link unsubscribe function for cleanup."""
        self._input_link_unsubs.append(unsub)

    def clear_input_link_unsubs(self) -> None:
        """Clear all input link unsubscribe functions."""
        self._input_link_unsubs.clear()

    def set_protocol_debug(self, *, enabled: bool) -> None:
        """Enable or disable protocol-level logging on the connection."""
        self._conn.set_protocol_debug(enabled=enabled)

    def _notify_availability_listeners(self, *, is_available: bool) -> None:
        """Notify all listeners of availability change."""
        for cb in list(self._availability_listeners):
            try:
                cb(is_available=is_available)
            except Exception:
                _LOGGER.exception("Error in availability listener")

    async def start(self) -> None:
        """Start the single worker."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker(), name="triad_worker")
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._run_poll(), name="triad_poll")

    async def stop(self) -> None:
        """Stop the worker and cancel pending commands."""
        # Close connection first to make any in-flight network calls fail immediately
        # This helps tasks stuck in network I/O respond to cancellation faster
        self._conn.close_nowait()
        if self._worker is not None:
            self._worker.cancel()
            # Drain queue and cancel futures
            while not self._queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    cmd = self._queue.get_nowait()
                    if not cmd.future.done():
                        cmd.future.set_exception(asyncio.CancelledError())
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._worker, timeout=SHUTDOWN_TIMEOUT)
            self._worker = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            # Wait for polling task to finish, but with a timeout to avoid hanging
            # if the task is stuck in a network call
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._poll_task, timeout=SHUTDOWN_TIMEOUT)
            self._poll_task = None

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        await self._conn.disconnect()

    # Registration for rolling poll
    def register_output(self, output: TriadAmsOutput) -> None:
        """Register an output for lightweight rolling polling."""
        self._outputs.add(output)

    async def _ensure_connection(self) -> None:
        """Ensure connection is established and update availability state."""
        was_available = self._available
        await asyncio.wait_for(self._conn.connect(), timeout=CONNECTION_TIMEOUT)
        # If we were unavailable and now connected, mark as available
        if not was_available:
            self._available = True
            _LOGGER.info("Triad AMS device available")
            self._notify_availability_listeners(is_available=True)

    async def _run_worker(self) -> None:  # noqa: PLR0912
        """Worker: dequeue, pace, ensure connection, execute, propagate result/error."""
        while True:
            cmd = await self._queue.get()
            try:
                # Enforce pacing
                now = asyncio.get_running_loop().time()
                delay = self._last_send_time + self._min_send_interval - now
                if delay > 0:
                    await asyncio.sleep(delay)

                # Execute
                await self._ensure_connection()
                result = await cmd.op(self._conn)
                self._last_send_time = asyncio.get_running_loop().time()
                if not cmd.future.done():
                    cmd.future.set_result(result)
            except asyncio.CancelledError:
                # Coordinator is shutting down; don't treat as a network failure.
                if not cmd.future.done():
                    cmd.future.cancel()
                raise
            except TransientDeviceError as exc:
                # Application-layer device shrug (empty / malformed response)
                # on a healthy TCP socket. Do NOT drop the connection or flip
                # availability — propagate to the caller so it can decide
                # whether to retry, skip, or surface. See issue #102.
                _LOGGER.debug(
                    "Transient device error; propagating without reconnect: %s",
                    exc,
                )
                if not cmd.future.done():
                    cmd.future.set_exception(exc)
            except NETWORK_EXCEPTIONS as exc:
                # Log, drop transport, attempt quick reconnect, and propagate error.
                _LOGGER.warning(
                    "Command failed; dropping and reopening connection: %s", exc
                )
                self._conn.close_nowait()
                # Mark as unavailable and notify listeners
                if self._available:
                    self._available = False
                    _LOGGER.warning("Triad AMS device unavailable: %s", exc)
                    self._notify_availability_listeners(is_available=False)
                # Best-effort immediate reconnect so subsequent commands are ready.
                try:
                    await asyncio.wait_for(
                        self._conn.connect(), timeout=CONNECTION_TIMEOUT
                    )
                    _LOGGER.info("Reconnected to Triad AMS after error")
                    # Mark as available again after successful reconnect
                    if not self._available:
                        self._available = True
                        _LOGGER.info("Triad AMS device available")
                        self._notify_availability_listeners(is_available=True)
                except (TimeoutError, OSError) as reconnect_exc:
                    _LOGGER.warning(
                        "Reconnect attempt failed (will retry on next command): %s",
                        reconnect_exc,
                    )
                if not cmd.future.done():
                    cmd.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _run_poll(self) -> None:
        """Round-robin poll: refresh one output every poll interval."""
        try:
            while True:
                outputs = [o for o in list(self._outputs) if o is not None]
                if not outputs:
                    await asyncio.sleep(self._poll_interval)
                    continue
                # Choose next output in a stable order
                self._poll_index = self._poll_index % len(outputs)
                target = outputs[self._poll_index]
                self._poll_index += 1
                try:
                    await target.refresh_and_notify()
                    # After refreshing the output, reconcile the coordinator's
                    # zone active sets with the polled device state. This ensures
                    # that external changes (or other controllers) are reflected
                    # and that trigger zone commands are sent when zones move
                    # between empty and non-empty.
                    try:
                        zone = self._zone_for_output(target.number)
                        active = self._zone_active_outputs.setdefault(zone, set())
                        if target.has_source:
                            was_empty = len(active) == 0
                            if target.number not in active:
                                active.add(target.number)
                                if was_empty and len(active) == 1:
                                    await self.set_trigger_zone(zone=zone, on=True)
                        elif target.number in active:
                            active.discard(target.number)
                            if len(active) == 0:
                                await self.set_trigger_zone(zone=zone, on=False)
                    except (OSError, ValueError, KeyError, AttributeError):
                        # Catch specific exceptions to prevent one error from breaking
                        # polling. This is a debug-level handler where we want to
                        # continue polling
                        _LOGGER.debug("Failed to reconcile zone sets after refresh")
                except asyncio.CancelledError:
                    # Task was cancelled during refresh, propagate immediately
                    raise
                except (OSError, TimeoutError, ValueError, KeyError) as e:
                    # Catch specific exceptions to prevent one error from breaking
                    # polling. This is a debug-level handler where we want to continue
                    _LOGGER.debug("Rolling poll refresh failed for output: %s", e)
                except Exception:  # noqa: BLE001
                    # Catch any other exceptions (e.g., from test mocks) to prevent
                    # polling from breaking. This is a debug-level handler.
                    _LOGGER.debug("Rolling poll refresh failed for output")
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            # Task was cancelled, exit cleanly
            _LOGGER.debug("Polling task cancelled")
            raise

    async def _execute(self, op: Callable[[TriadConnection], Awaitable[Any]]) -> Any:
        """Enqueue a command and await its result or error."""
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Command(op=op, future=future))
        return await future

    # Public API
    async def set_output_volume(self, output_channel: int, percentage: float) -> None:
        """Set volume."""
        await self._execute(lambda c: c.set_output_volume(output_channel, percentage))

    async def get_output_volume(self, output_channel: int) -> float:
        """Get volume (0..1)."""
        return await self._execute(lambda c: c.get_output_volume(output_channel))

    async def get_output_volume_from_device(self, output_channel: int) -> float:
        """Explicit device read (testing)."""
        return await self.get_output_volume(output_channel)

    async def set_output_mute(self, output_channel: int, *, mute: bool) -> None:
        """Set mute state."""
        await self._execute(lambda c: c.set_output_mute(output_channel, mute=mute))

    async def get_output_mute(self, output_channel: int) -> bool:
        """Get mute state."""
        return await self._execute(lambda c: c.get_output_mute(output_channel))

    async def volume_step_up(self, output_channel: int, *, large: bool = False) -> None:
        """Step volume up."""
        await self._execute(lambda c: c.volume_step_up(output_channel, large=large))

    async def volume_step_down(
        self, output_channel: int, *, large: bool = False
    ) -> None:
        """Step volume down."""
        await self._execute(lambda c: c.volume_step_down(output_channel, large=large))

    async def set_output_to_input(
        self, output_channel: int, input_channel: int
    ) -> None:
        """
        Route output to input and update zone active set.

        This enqueues a single operation that performs the device routing and
        then updates the coordinator's zone set. If the output makes the zone
        transition from empty -> non-empty, the trigger zone ON command is
        issued on the same connection to preserve sequencing.
        """

        async def _op(c: TriadConnection) -> None:  # type: ignore[name-defined]
            await c.set_output_to_input(output_channel, input_channel)
            # Compute zone and add this output to the active set
            zone = self._zone_for_output(output_channel)
            active = self._zone_active_outputs.setdefault(zone, set())
            was_empty = len(active) == 0
            active.add(output_channel)
            if was_empty and len(active) == 1:
                await c.set_trigger_zone(zone=zone, on=True)

        await self._execute(_op)

    async def get_output_source(self, output_channel: int) -> int | None:
        """Get routed input (1-based) or None."""
        return await self._execute(lambda c: c.get_output_source(output_channel))

    async def disconnect_output(self, output_channel: int) -> None:
        """
        Disconnect output and update zone active set.

        After the device disconnect command succeeds, remove the output from
        the zone active set and issue a trigger zone OFF command only when the
        set becomes empty.
        """

        async def _op(c: TriadConnection) -> None:  # type: ignore[name-defined]
            await c.disconnect_output(output_channel, self._input_count)
            zone = self._zone_for_output(output_channel)
            active = self._zone_active_outputs.get(zone)
            if active and output_channel in active:
                active.discard(output_channel)
                if len(active) == 0:
                    await c.set_trigger_zone(zone=zone, on=False)

        await self._execute(_op)

    async def set_trigger_zone(self, zone: int, *, on: bool) -> None:
        """
        Send a trigger zone on/off command (passes through to device).

        Zone active tracking is handled by `set_output_to_input` and
        `disconnect_output`; this method provides a direct passthrough for
        manual or legacy calls.
        """
        await self._execute(lambda c: c.set_trigger_zone(zone=zone, on=on))

    # ---- Extended settings API (input controls) ----
    async def set_input_gain(self, input_channel: int, gain_db: float) -> None:
        """Set input gain in dB."""
        await self._execute(lambda c: c.set_input_gain(input_channel, gain_db))

    async def get_input_gain(self, input_channel: int) -> float:
        """Get input gain in dB."""
        return await self._execute(lambda c: c.get_input_gain(input_channel))

    async def set_input_delay(self, input_channel: int, delay_ms: int) -> None:
        """Set input audio delay in ms."""
        await self._execute(lambda c: c.set_input_delay(input_channel, delay_ms))

    async def get_input_delay(self, input_channel: int) -> int:
        """Get input audio delay in ms."""
        return await self._execute(lambda c: c.get_input_delay(input_channel))

    async def get_input_audio_sense(self, input_channel: int) -> bool:
        """Return True if audio is detected on the input."""
        return await self._execute(lambda c: c.get_input_audio_sense(input_channel))

    # ---- Extended settings API (output controls) ----
    async def set_output_max_volume(
        self, output_channel: int, percentage: float
    ) -> None:
        """Set output max volume (0..1)."""
        await self._execute(
            lambda c: c.set_output_max_volume(output_channel, percentage)
        )

    async def get_output_max_volume(self, output_channel: int) -> float:
        """Get output max volume (0..1)."""
        return await self._execute(lambda c: c.get_output_max_volume(output_channel))

    async def set_output_turn_on_volume(
        self, output_channel: int, percentage: float
    ) -> None:
        """Set output turn-on volume (0..1)."""
        await self._execute(
            lambda c: c.set_output_turn_on_volume(output_channel, percentage)
        )

    async def get_output_turn_on_volume(self, output_channel: int) -> float:
        """Get output turn-on volume (0..1)."""
        return await self._execute(
            lambda c: c.get_output_turn_on_volume(output_channel)
        )

    async def set_output_balance(self, output_channel: int, balance_db: float) -> None:
        """Set output balance in dB (-12 left .. +12 right)."""
        await self._execute(lambda c: c.set_output_balance(output_channel, balance_db))

    async def get_output_balance(self, output_channel: int) -> float:
        """Get output balance in dB (-12 left .. +12 right)."""
        return await self._execute(lambda c: c.get_output_balance(output_channel))

    async def set_output_loudness(self, output_channel: int, *, on: bool) -> None:
        """Enable or disable loudness for an output."""
        await self._execute(lambda c: c.set_output_loudness(output_channel, on=on))

    async def get_output_loudness(self, output_channel: int) -> bool:
        """Return True if loudness is enabled for an output."""
        return await self._execute(lambda c: c.get_output_loudness(output_channel))

    async def set_output_delay(self, output_channel: int, delay_ms: int) -> None:
        """Set output audio delay in ms."""
        await self._execute(lambda c: c.set_output_delay(output_channel, delay_ms))

    async def get_output_delay(self, output_channel: int) -> int:
        """Get output audio delay in ms."""
        return await self._execute(lambda c: c.get_output_delay(output_channel))

    async def set_output_mode(self, output_channel: int, mode: int) -> None:
        """Set the DSP output mode (OUTPUT_MODE_* constant)."""
        await self._execute(lambda c: c.set_output_mode(output_channel, mode))

    async def get_output_mode(self, output_channel: int) -> int | None:
        """Get the DSP output mode (OUTPUT_MODE_* constant) or None."""
        return await self._execute(lambda c: c.get_output_mode(output_channel))

    # ---- Extended settings API (DSP: shelf filters and room EQ) ----
    async def set_output_shelf(
        self, output_channel: int, shelf: str, param: str, value: float
    ) -> None:
        """Set a low/high shelf parameter (frequency Hz, gain dB, or Q)."""
        await self._execute(
            lambda c: c.set_output_shelf(output_channel, shelf, param, value)
        )

    async def get_output_shelf(
        self, output_channel: int, shelf: str, param: str
    ) -> float:
        """Get a low/high shelf parameter (frequency Hz, gain dB, or Q)."""
        return await self._execute(
            lambda c: c.get_output_shelf(output_channel, shelf, param)
        )

    async def set_room_eq(
        self, output_channel: int, band: int, param: str, value: float
    ) -> None:
        """Set a room EQ band parameter (frequency Hz, gain dB, or Q)."""
        await self._execute(lambda c: c.set_room_eq(output_channel, band, param, value))

    async def get_room_eq(self, output_channel: int, band: int, param: str) -> float:
        """Get a room EQ band parameter (frequency Hz, gain dB, or Q)."""
        return await self._execute(lambda c: c.get_room_eq(output_channel, band, param))

    async def set_room_eq_lock(self, output_channel: int, *, locked: bool) -> None:
        """Lock or unlock the room EQ for an output."""
        await self._execute(lambda c: c.set_room_eq_lock(output_channel, locked=locked))

    async def get_room_eq_lock(self, output_channel: int) -> bool:
        """Return True if the room EQ is locked for an output."""
        return await self._execute(lambda c: c.get_room_eq_lock(output_channel))

    # ---- Extended settings API (2.1 crossover and test tone) ----
    async def set_crossover_frequency(
        self, output_channel: int, frequency_hz: int
    ) -> None:
        """Set the 2.1 crossover frequency in Hz."""
        await self._execute(
            lambda c: c.set_crossover_frequency(output_channel, frequency_hz)
        )

    async def get_crossover_frequency(self, output_channel: int) -> int:
        """Get the 2.1 crossover frequency in Hz."""
        return await self._execute(lambda c: c.get_crossover_frequency(output_channel))

    async def set_crossover_type(self, output_channel: int, crossover: int) -> None:
        """Set the 2.1 crossover filter type (CROSSOVER_TYPES value)."""
        await self._execute(lambda c: c.set_crossover_type(output_channel, crossover))

    async def get_crossover_type(self, output_channel: int) -> int | None:
        """Get the 2.1 crossover filter type (CROSSOVER_TYPES value) or None."""
        return await self._execute(lambda c: c.get_crossover_type(output_channel))

    async def set_sub_volume_offset(
        self, output_channel: int, offset_db: float
    ) -> None:
        """Set the 2.1 sub volume offset in dB (-12..0)."""
        await self._execute(
            lambda c: c.set_sub_volume_offset(output_channel, offset_db)
        )

    async def get_sub_volume_offset(self, output_channel: int) -> float:
        """Get the 2.1 sub volume offset in dB (-12..0)."""
        return await self._execute(lambda c: c.get_sub_volume_offset(output_channel))

    async def set_test_tone_volume(self, output_channel: int, volume_db: float) -> None:
        """Set the test tone volume in dB (-24..0)."""
        await self._execute(lambda c: c.set_test_tone_volume(output_channel, volume_db))

    # ---- Extended settings API (device-level) ----
    async def reboot_device(self) -> None:
        """Reboot the device."""
        await self._execute(lambda c: c.reboot_device())

    async def get_firmware_version(self) -> str:
        """Get the device firmware version string."""
        return await self._execute(lambda c: c.get_firmware_version())

    async def get_mac_address(self) -> str:
        """Get the device MAC address."""
        return await self._execute(lambda c: c.get_mac_address())

    async def get_power_status(self) -> str:
        """Get the device power status text."""
        return await self._execute(lambda c: c.get_power_status())

    def _zone_for_output(self, output: int) -> int:
        """
        Return the 1-based zone for an output, clamped to 1..3.

        Zones are grouped in blocks of 8 outputs as used elsewhere in the
        integration (see `TriadAmsOutput` initialization).
        """
        zone_raw = (output - 1) // 8 + 1
        return max(1, min(zone_raw, 3))
