# Touched by AI 2026-07-10 (Claude Code): added FF56 framing helpers and
# extended protocol commands reverse-engineered from packet captures.
"""
Connection management for Triad AMS.

Provides async helpers to control and query device state.

Protocol notes:
    Frames start with ``FF 55`` (single-byte values) or ``FF 56`` (extended
    commands carrying 32-bit big-endian signed values), followed by a length
    byte covering the remaining payload. Queries append ``F5`` after the
    opcode. Responses are null-terminated ASCII strings. The extended command
    set below was reverse-engineered from packet captures of third-party
    control software talking to an AMS-16.
"""

import asyncio
import contextlib
import logging
import re
import socket
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

from .const import (
    CONNECTION_TIMEOUT,
    DEVICE_COMMAND_DELAY,
    POST_CONNECT_DELAY,
    VOLUME_STEPS,
)
from .exceptions import TransientDeviceError
from .volume_lut import step_for_db

_LOGGER = logging.getLogger(__name__)

# Shelf filter opcodes for FF56 group 0x03.
_SHELF_OPCODES: dict[tuple[str, str], int] = {
    ("low", "frequency"): 0x03,
    ("low", "gain"): 0x0D,
    ("low", "q"): 0x04,
    ("high", "frequency"): 0x01,
    ("high", "gain"): 0x0C,
    ("high", "q"): 0x02,
}

# Room EQ band parameter offsets: sub-opcode = (band - 1) * 0x10 + offset.
_EQ_PARAM_OFFSETS: dict[str, int] = {"frequency": 0, "gain": 1, "q": 2}

# Output mode values for FF 56 .. 03 0B (per driver OUTPUT_MODES table).
OUTPUT_MODE_DSP_BYPASS = 0
OUTPUT_MODE_STEREO = 1
OUTPUT_MODE_MONO = 2
OUTPUT_MODE_21_STEREO = 3
OUTPUT_MODE_21_MONO = 4
OUTPUT_MODE_TEST = 5

# Crossover type values for FF 56 .. 03 06 (per driver CROSSOVER_TYPE table).
CROSSOVER_TYPES: dict[str, int] = {
    "butterworth_12": 0,
    "butterworth_24": 1,
    "butterworth_48": 2,
    "linkwitz_riley_12": 3,
    "linkwitz_riley_24": 4,
    "linkwitz_riley_48": 5,
}

ROOM_EQ_BAND_COUNT = 12

# Signed dB values (gain, balance) are encoded as (value + 12) * 2 => 0..0x30.
_DB_OFFSET_LIMIT = 12.0
# Test tone volume (-24..0 dB) is encoded as (value + 24) * 2 => 0..0x30.
_TEST_TONE_MIN_DB = -24.0
# Sub volume offset (-12..0 dB) is encoded as (value + 12) * 2 => 0..0x18.
_SUB_OFFSET_MIN_DB = -12.0

# Responses are padded to a fixed width with nulls (150 bytes observed on
# AMS-16 v2). Cap how many null-only frames a single read will skip so a
# null-flooding device cannot spin the reader; leftover padding from one
# response is well under this cap.
_MAX_NULL_FRAME_SKIPS = 256

ShelfName = Literal["low", "high"]
FilterParam = Literal["frequency", "gain", "q"]


class TriadConnection:
    """Manage a persistent connection to the Triad AMS device."""

    def __init__(self, host: str, port: int, *, protocol_debug: bool = False) -> None:
        """Initialize a persistent connection to the Triad AMS device."""
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._protocol_debug = protocol_debug

    def _log_protocol(self, msg: str, *args: object) -> None:
        """Emit protocol logs when enabled via options."""
        if self._protocol_debug:
            _LOGGER.debug(msg, *args)

    def set_protocol_debug(self, *, enabled: bool) -> None:
        """Enable or disable protocol-level logging."""
        self._protocol_debug = enabled

    @staticmethod
    def _summarize_bytes(data: bytes, *, max_bytes: int = 16) -> str:
        """Return a compact hex summary of a payload."""
        if not data:
            return "len=0"
        prefix = data[:max_bytes].hex()
        suffix = "..." if len(data) > max_bytes else ""
        return f"len={len(data)} hex={prefix}{suffix}"

    @staticmethod
    def _summarize_text(text: str, *, max_len: int = 80) -> str:
        """Return a compact, single-line summary of response text."""
        compact = " ".join(text.split())
        if len(compact) > max_len:
            compact = f"{compact[:max_len]}..."
        return compact

    async def connect(self) -> None:
        """Establish a connection to the Triad AMS device if not already connected."""
        if self._writer is not None:
            self._log_protocol("connect(): already connected; skipping")
            return
        self._log_protocol("connect(): begin to %s:%s", self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._log_protocol("connect(): connected to %s:%s", self.host, self.port)
        # Some devices need a short delay after connect before accepting commands
        await asyncio.sleep(POST_CONNECT_DELAY)
        self._log_protocol("connect(): ready (post-sleep)")

    async def disconnect(self) -> None:
        """Close the connection to the Triad AMS device if open."""
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._reader = None
            self._writer = None
            self._log_protocol("disconnect(): closed connection")

    def close_nowait(self) -> None:
        """Close the transport without awaiting shutdown (non-blocking)."""
        self._log_protocol(
            "close_nowait(): writer is %s", "present" if self._writer else "None"
        )
        # Note: We don't acquire the lock here because:
        # 1. This is called from coordinator.stop() which needs to interrupt
        #    in-flight operations
        # 2. The lock might be held by a network call that's stuck
        # 3. Setting _reader/_writer to None will cause subsequent operations
        #    to fail
        if self._writer is not None:
            with contextlib.suppress(Exception):
                # Shutdown the socket to interrupt any pending reads immediately
                # This causes reader.readuntil() to fail with ConnectionResetError
                socket_obj = self._writer.get_extra_info("socket")
                if socket_obj is not None:
                    socket_obj.shutdown(socket.SHUT_RDWR)
                self._writer.close()
        self._reader = None
        self._writer = None
        self._log_protocol("close_nowait(): cleared reader/writer")

    async def _ensure_connection_for_send(self) -> None:
        """Ensure connection is established before sending."""
        if self._writer is None or self._reader is None:
            self._log_protocol("_send_command(): transport missing; calling connect()")
            await self.connect()

    async def _write_command_bytes(
        self, writer: "StreamWriter", command: bytes
    ) -> None:
        """Write command bytes to the connection."""
        self._log_protocol("TX %s", self._summarize_bytes(command))
        writer.write(command)
        await writer.drain()
        # Add a very small delay for device tolerance
        await asyncio.sleep(DEVICE_COMMAND_DELAY)

    async def _read_response_bytes(self, reader: "StreamReader") -> bytes:
        """
        Read response bytes from the connection.

        The device pads every response to a fixed width (150 bytes on AMS-16
        v2 firmware) with trailing nulls. ``readuntil`` consumes only the
        first null, so the padding of one response is still buffered
        when the next response is read. Null-only frames are therefore
        skipped here (bounded by the timeout and a skip cap); hitting either
        bound with only nulls seen raises ``TransientDeviceError``.
        """
        # Check connection state before reading - if closed, fail immediately
        if self._reader is None or self._writer is None:
            msg = "Connection closed"
            raise OSError(msg)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECTION_TIMEOUT
        skipped_nulls = 0
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0 or skipped_nulls >= _MAX_NULL_FRAME_SKIPS:
                    self._log_protocol(
                        "RX only null padding (%d frames skipped)", skipped_nulls
                    )
                    msg = "Only null padding received from device"
                    raise TransientDeviceError(msg)
                response = await asyncio.wait_for(
                    reader.readuntil(b"\x00"), timeout=remaining
                )
                if not response.strip(b"\x00").strip():
                    # Null padding left over from a previous fixed-width
                    # response frame; skip it silently.
                    skipped_nulls += 1
                    continue
                break
        except (
            asyncio.CancelledError,
            ConnectionResetError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
        ):
            # If cancelled or connection was closed, check state and raise
            # appropriate error
            if self._reader is None or self._writer is None:
                msg = "Connection closed"
                raise OSError(msg) from None
            # Re-raise the original exception
            raise
        except TimeoutError:
            if skipped_nulls:
                # The device only sent padding within the timeout window;
                # treat as an application-layer shrug, not a dead socket.
                msg = "Only null padding received from device"
                raise TransientDeviceError(msg) from None
            raise
        except OSError:
            # Re-raise OSError as-is (might be from socket shutdown)
            raise
        self._log_protocol("RX %s", self._summarize_bytes(response))
        return response

    def _validate_response(
        self, text: str, _expect: str | None, command: bytes
    ) -> None:
        """Validate response text against expected pattern."""
        # Detect device-side command error or protocol desync (nulls).
        # The matrix firmware intermittently returns empty responses on
        # otherwise-healthy TCP connections (most often for `get_output_mute`,
        # also seen on volume / source queries). Treat these as transient
        # *application-layer* failures — propagate to the caller without
        # tearing down the socket. See issue #102.
        if text == "" or re.search(r"^command\s+error$", text, re.IGNORECASE):
            _LOGGER.debug(
                "Device returned error/empty response for command: %s",
                command.hex(),
            )
            msg = "Triad command error or empty response"
            raise TransientDeviceError(msg)

    async def _send_command(self, command: bytes, *, expect: str | None = None) -> str:
        """
        Send a command and return the response string.

        Adds a small inter-command delay, logs raw traffic, and applies a
        reasonable timeout to reads.
        """
        self._log_protocol("_send_command(): waiting for lock")
        async with self._lock:
            self._log_protocol("_send_command(): acquired lock")
            await self._ensure_connection_for_send()
            # Create local non-optional references for type checkers
            writer = cast("asyncio.StreamWriter", self._writer)
            reader = cast("asyncio.StreamReader", self._reader)
            await self._write_command_bytes(writer, command)
            response = await self._read_response_bytes(reader)
            text = response.decode(errors="replace").strip("\x00").strip()
            self._log_protocol("RX text=%s", self._summarize_text(text))
            # Detect device-side command rejection before the expect check so
            # a "Command error" (e.g. an out-of-range value) surfaces as a
            # transient application-layer failure instead of tearing down
            # the healthy TCP connection via the mismatch path below.
            self._validate_response(text, expect, command)
            # Evaluate the first (and only) frame. If it doesn't match the
            # expected pattern, allow exactly one skip for an unsolicited
            # AudioSense event, then re-evaluate the next frame.
            if (
                expect is not None
                and text
                and not re.search(expect, text, re.IGNORECASE)
            ):
                if re.search(
                    r"^AudioSense:Input\[\d+\]\s*:\s*(0|1)\s*$",
                    text,
                    re.IGNORECASE,
                ):
                    self._log_protocol(
                        "Skipping unsolicited AudioSense event: %s", text
                    )
                    response = await self._read_response_bytes(reader)
                    text = response.decode(errors="replace").strip("\x00").strip()
                    self._log_protocol("RX text=%s", self._summarize_text(text))
                # After optional skip, if still not matching -> error
                if text and not re.search(expect, text, re.IGNORECASE):
                    _LOGGER.warning("Unexpected response: %s", text)
                    self.close_nowait()
                    err_msg = "Unexpected response from device"
                    raise OSError(err_msg)
            self._validate_response(text, expect, command)
            return text

    async def send_raw(self, command: bytes) -> str:
        """
        Send a raw command and return the decoded response string.

        Intended for diagnostic/debug usage. Uses the same transport, lock,
        and parsing behavior as all other commands.
        """
        return await self._send_command(command)

    async def set_output_volume(self, output_channel: int, percentage: float) -> None:
        """
        Set volume immediately without debouncing.

        Args:
            output_channel: 1-based output channel index.
            percentage: Volume as a float (0.0 = off, 1.0 = max).
        Command: FF 55 04 03 1E <output> <value>  (output sent as 0-based)
        Value: 0x00 (off) to 0x64 (max)

        """
        # Clamp to device range 0..1.0 (0x00..0x64)
        capped = max(0.0, min(percentage, 1.0))

        # Quantize to nearest device step for consistency (0..VOLUME_STEPS)
        val = round(capped * VOLUME_STEPS)
        val = max(0, min(val, VOLUME_STEPS))
        cmd = bytearray.fromhex("FF5504031E") + bytes([output_channel - 1, val])
        resp = await self._send_command(cmd, expect=r"Output\s+Volume|Volume\s*:")
        _LOGGER.info("Set volume for output %d to %.2f", output_channel, capped)
        self._log_protocol(
            "Set volume response for output %d: %s", output_channel, resp
        )

    async def get_output_volume(self, output_channel: int) -> float:
        """
        Get the volume for a specific output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 55 04 03 1E F5 <output>
        Returns:
            float: Volume as a float (0.0 = off, 1.0 = max)

        """
        cmd = bytearray.fromhex("FF5504031EF5") + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Volume\s*:")
        # Prefer raw hex value if present (exact mapping to slider scale)
        m_hex = re.search(r"Volume\s*:\s*0x([0-9A-Fa-f]+)", resp)
        if m_hex:
            value = int(m_hex.group(1), 16)
            return max(0.0, min(1.0, value / VOLUME_STEPS))
        # Otherwise parse dB and map to nearest step using measured LUT
        m = re.search(r"Volume\s*:\s*(-?\d+(?:\.\d+)?)", resp)
        if m:
            db = float(m.group(1))
            step = step_for_db(db)
            return step / VOLUME_STEPS
        _LOGGER.warning("Could not parse output volume from response: %s", resp)
        return 0.0

    async def set_output_mute(self, output_channel: int, *, mute: bool) -> None:
        """
        Set mute state for an output channel.

        Args:
            output_channel: 1-based output channel index.
            mute: True to mute, False to unmute.

        Commands:
            Mute on:  FF 55 03 03 17 <output>
            Mute off: FF 55 03 03 18 <output>

        """
        base = "FF55030317" if mute else "FF55030318"
        cmd = bytearray.fromhex(base) + bytes([output_channel - 1])
        resp = await self._send_command(cmd)
        _LOGGER.info("Set mute for output %d to %s", output_channel, mute)
        self._log_protocol("Set mute response for output %d: %s", output_channel, resp)

    async def get_output_mute(self, output_channel: int) -> bool:
        """
        Return True if the output is muted.

        Command: FF 55 04 03 17 F5 <output>
        Response formats observed (case varies):
          - "Get Out[1] Mute status : Unmute"
          - "Get Out[5] Mute status : mute"
          - "Mute : On" / "Mute : Off"
          - "Muted" / "Unmuted"

        """
        cmd = bytearray.fromhex("FF55040317F5") + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Mute")
        # Try to capture the token after "Mute" or "Mute status"
        m = re.search(r"Mute(?:\s+status)?\s*:\s*([A-Za-z0-9]+)", resp, re.IGNORECASE)
        if m:
            token = m.group(1).strip().lower()
            true_tokens = {"on", "mute", "muted", "1", "true", "yes"}
            false_tokens = {"off", "unmute", "unmuted", "0", "false", "no"}
            if token in true_tokens:
                return True
            if token in false_tokens:
                return False
        # Fallback heuristics
        if re.search(r"\bmuted\b", resp, re.IGNORECASE):
            return True
        if re.search(r"\bunmuted|unmute\b", resp, re.IGNORECASE):
            return False
        _LOGGER.warning("Could not parse mute state from response: %s", resp)
        return False

    async def volume_step_up(self, output_channel: int, *, large: bool = False) -> None:
        """Step the output volume up (small or large step)."""
        cmd = bytearray.fromhex("FF55030315" if large else "FF55030313") + bytes(
            [output_channel - 1]
        )
        resp = await self._send_command(cmd, expect=r"(Input\s+Source|Audio\s+Off)")
        if large:
            _LOGGER.info("Volume step up (large) for output %d", output_channel)
            self._log_protocol(
                "Volume step up (large) response for output %d: %s",
                output_channel,
                resp,
            )
        else:
            self._log_protocol(
                "Volume step up (small) response for output %d: %s",
                output_channel,
                resp,
            )

    async def volume_step_down(
        self, output_channel: int, *, large: bool = False
    ) -> None:
        """Step the output volume down (small or large step)."""
        cmd = bytearray.fromhex("FF55030316" if large else "FF55030314") + bytes(
            [output_channel - 1]
        )
        resp = await self._send_command(cmd, expect=r"(Input\s+Source|Audio\s+Off)")
        if large:
            _LOGGER.info("Volume step down (large) for output %d", output_channel)
            self._log_protocol(
                "Volume step down (large) response for output %d: %s",
                output_channel,
                resp,
            )
        else:
            self._log_protocol(
                "Volume step down (small) response for output %d: %s",
                output_channel,
                resp,
            )

    async def set_output_to_input(
        self, output_channel: int, input_channel: int
    ) -> None:
        """
        Route a specific output channel to a given input channel.

        Args:
            output_channel: 1-based output channel index.
            input_channel: 1-based input channel index.
        Command: FF 55 04 03 1D <output> <input>

        """
        cmd = bytearray.fromhex("FF5504031D") + bytes(
            [output_channel - 1, input_channel - 1]
        )
        resp = await self._send_command(cmd, expect=r"Trigger|Set\s+.*")
        # Be tolerant of varying response strings
        _LOGGER.info("Set output %d to input %d", output_channel, input_channel)
        self._log_protocol(
            "Set output response for output %d -> input %d: %s",
            output_channel,
            input_channel,
            resp,
        )

    async def get_output_source(self, output_channel: int) -> int | None:
        """
        Get the input source currently routed to a specific output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 55 04 03 1D F5 <output>
        Returns:
            int | None: 1-based input channel, or None if Audio Off.

        """
        cmd = bytearray.fromhex("FF5504031DF5") + bytes([output_channel - 1])
        # Accept "Audio Off", "Input Source : input N" or device 'Set ...' echoes
        resp = await self._send_command(
            cmd, expect=r"(Audio\s+Off|Input\s+Source|Set\s+.*)"
        )
        if "Audio Off" in resp:
            return None
        m = re.search(r"input (\d+)", resp)
        if m:
            return int(m.group(1))
        _LOGGER.warning("Could not parse output source from response: %s", resp)
        return None

    async def set_trigger_zone(self, zone: int = 1, *, on: bool) -> None:
        """
        Set a trigger zone on or off.

        Args:
            zone: 1-based trigger zone index (1..3).
                Default 1 for backwards compatibility.
            on: True to enable, False to disable.

        Command mapping:
            Zone 1 on:  FF 55 03 05 50 00, Zone 1 off: FF 55 03 05 51 00
            Zone 2 on:  FF 55 03 05 50 01, Zone 2 off: FF 55 03 05 51 01
            Zone 3 on:  FF 55 03 05 50 02, Zone 3 off: FF 55 03 05 51 02

        The pattern is: FF 55 03 05 <base> <zone-1>
        where <base> is 0x50 for on or 0x51 for off.

        """
        # Normalize zone to 1..3
        zone = max(1, min(zone, 3))

        zone_byte = zone - 1  # 0 for zone 1, 1 for zone 2, 2 for zone 3
        # Build explicit hex command per observed device opcodes
        hex_zone = f"{zone_byte:02X}"
        if on:
            # Examples: zone1 on: FF5503055000, zone2 on: FF5503055001
            cmd = bytearray.fromhex(f"FF55030550{hex_zone}")
        else:
            # Examples: zone1 off: FF5503055100, zone2 off: FF5503055101
            cmd = bytearray.fromhex(f"FF55030551{hex_zone}")
        resp = await self._send_command(cmd, expect=r"Max\s+Volume|0x|dB|Set\s+.*")
        _LOGGER.info("Set trigger zone %d to %s", zone, on)
        self._log_protocol("Set trigger zone response for zone %d: %s", zone, resp)

    async def disconnect_output(self, output_channel: int, input_count: int) -> None:
        """
        Disconnect the output by routing it to an invalid input channel (off).

        Args:
            output_channel: 1-based output channel index.
            input_count: Total number of inputs (used to determine invalid input).

        Command: FF 55 04 03 1D <output> <invalid_input>

        """
        cmd = bytearray.fromhex("FF5504031D") + bytes([output_channel - 1, input_count])
        resp = await self._send_command(cmd, expect=r"Start\s+Vol|0x|dB|Set\s+.*")
        # Tolerate varied responses and log outcome
        if "Audio Off" in resp:
            _LOGGER.info("Disconnected output %d", output_channel)
        else:
            _LOGGER.info("Requested disconnect for output %d", output_channel)
        self._log_protocol(
            "Disconnect output response for %d: %s", output_channel, resp
        )

    # ------------------------------------------------------------------
    # Extended protocol helpers (FF 56 frames and value codecs)
    # ------------------------------------------------------------------

    @staticmethod
    def _ff56(*payload: int | bytes) -> bytearray:
        """Build an ``FF 56`` frame; the length byte covers the payload."""
        body = bytearray()
        for part in payload:
            if isinstance(part, bytes):
                body.extend(part)
            else:
                body.append(part)
        return bytearray((0xFF, 0x56, len(body))) + body

    @staticmethod
    def _encode_int32(value: float) -> bytes:
        """Encode a value as a signed 32-bit big-endian integer."""
        return round(value).to_bytes(4, "big", signed=True)

    @staticmethod
    def _encode_offset_db(value: float, *, minimum: float, maximum: float) -> int:
        """Encode a dB value as the device offset byte ``(v - min) * 2``."""
        capped = max(minimum, min(value, maximum))
        return round((capped - minimum) * 2)

    @staticmethod
    def _parse_last_number(resp: str) -> float | None:
        """Return the final numeric token in a response, if any."""
        matches = re.findall(r"-?\d+(?:\.\d+)?", resp)
        if not matches:
            return None
        return float(matches[-1])

    def _require_last_number(self, resp: str, what: str) -> float:
        """Return the final numeric token or raise ``TransientDeviceError``."""
        value = self._parse_last_number(resp)
        if value is None:
            _LOGGER.warning("Could not parse %s from response: %s", what, resp)
            msg = f"Unparseable {what} response"
            raise TransientDeviceError(msg)
        return value

    def _parse_db_volume(self, resp: str, what: str) -> float:
        """Parse a volume-style response (raw hex or dB) into 0.0..1.0."""
        m_hex = re.search(r":\s*0x([0-9A-Fa-f]+)", resp)
        if m_hex:
            value = int(m_hex.group(1), 16)
            return max(0.0, min(1.0, value / VOLUME_STEPS))
        db = self._require_last_number(resp, what)
        return step_for_db(db) / VOLUME_STEPS

    # ------------------------------------------------------------------
    # Input settings
    # ------------------------------------------------------------------

    async def set_input_gain(self, input_channel: int, gain_db: float) -> None:
        """
        Set the input gain for an input channel.

        Args:
            input_channel: 1-based input channel index.
            gain_db: Gain in dB (-12.0 .. +12.0, 0.5 dB steps).
        Command: FF 55 04 02 04 <input> <(gain+12)*2>

        """
        val = self._encode_offset_db(
            gain_db, minimum=-_DB_OFFSET_LIMIT, maximum=_DB_OFFSET_LIMIT
        )
        cmd = bytearray.fromhex("FF55040204") + bytes([input_channel - 1, val])
        resp = await self._send_command(cmd, expect=r"input\s+gain")
        self._log_protocol("Set input gain for input %d: %s", input_channel, resp)

    async def get_input_gain(self, input_channel: int) -> float:
        """
        Get the input gain in dB for an input channel.

        Args:
            input_channel: 1-based input channel index.
        Command: FF 55 04 02 04 F5 <input>
        Returns:
            float: Gain in dB (-12.0 .. +12.0).

        """
        cmd = bytearray.fromhex("FF55040204F5") + bytes([input_channel - 1])
        resp = await self._send_command(cmd, expect=r"input\s+gain")
        return self._require_last_number(resp, "input gain")

    async def set_input_delay(self, input_channel: int, delay_ms: int) -> None:
        """
        Set the audio delay for an input channel.

        Args:
            input_channel: 1-based input channel index.
            delay_ms: Delay in milliseconds (0..80).
        Command: FF 56 04 02 04 <input> <delay>

        """
        delay = max(0, min(int(delay_ms), 80))
        cmd = self._ff56(0x02, 0x04, input_channel - 1, delay)
        resp = await self._send_command(cmd, expect=r"Delay")
        self._log_protocol("Set input delay for input %d: %s", input_channel, resp)

    async def get_input_delay(self, input_channel: int) -> int:
        """
        Get the audio delay in milliseconds for an input channel.

        Args:
            input_channel: 1-based input channel index.
        Command: FF 56 04 02 04 F5 <input>
        Returns:
            int: Delay in milliseconds (0..80).

        """
        cmd = self._ff56(0x02, 0x04, 0xF5, input_channel - 1)
        resp = await self._send_command(cmd, expect=r"Delay")
        return int(self._require_last_number(resp, "input delay"))

    async def get_input_audio_sense(self, input_channel: int) -> bool:
        """
        Return True if audio is detected on the input channel.

        Args:
            input_channel: 1-based input channel index.
        Command: FF 56 04 02 03 F5 <input>
        Response: "Get Input[N] Audio Detect Detected|Undetected"

        """
        cmd = self._ff56(0x02, 0x03, 0xF5, input_channel - 1)
        resp = await self._send_command(cmd, expect=r"Audio\s+Detect")
        return bool(re.search(r"\bDetected\b", resp))

    # ------------------------------------------------------------------
    # Output level / tone settings
    # ------------------------------------------------------------------

    async def set_output_max_volume(
        self, output_channel: int, percentage: float
    ) -> None:
        """
        Set the maximum volume limit for an output channel.

        Args:
            output_channel: 1-based output channel index.
            percentage: Limit as a float (0.0..1.0 of the volume scale).
        Command: FF 55 04 03 1F <output> <value 0x00-0x64>

        """
        capped = max(0.0, min(percentage, 1.0))
        val = round(capped * VOLUME_STEPS)
        cmd = bytearray.fromhex("FF5504031F") + bytes([output_channel - 1, val])
        resp = await self._send_command(cmd, expect=r"Max\s+Volume")
        self._log_protocol("Set max volume for output %d: %s", output_channel, resp)

    async def get_output_max_volume(self, output_channel: int) -> float:
        """
        Get the maximum volume limit for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 55 04 03 1F F5 <output>
        Returns:
            float: Limit as a float (0.0..1.0 of the volume scale).

        """
        cmd = bytearray.fromhex("FF5504031FF5") + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Max\s+Volume")
        return self._parse_db_volume(resp, "max volume")

    async def set_output_turn_on_volume(
        self, output_channel: int, percentage: float
    ) -> None:
        """
        Set the turn-on (start) volume for an output channel.

        Args:
            output_channel: 1-based output channel index.
            percentage: Volume as a float (0.0..1.0 of the volume scale).
        Command: FF 55 04 03 33 <output> <value 0x00-0x64>

        """
        capped = max(0.0, min(percentage, 1.0))
        val = round(capped * VOLUME_STEPS)
        cmd = bytearray.fromhex("FF55040333") + bytes([output_channel - 1, val])
        resp = await self._send_command(cmd, expect=r"Turn\s+on\s+Vol|Start\s+Vol")
        self._log_protocol("Set turn-on volume for output %d: %s", output_channel, resp)

    async def get_output_turn_on_volume(self, output_channel: int) -> float:
        """
        Get the turn-on (start) volume for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 55 04 03 33 F5 <output>
        Returns:
            float: Volume as a float (0.0..1.0 of the volume scale).

        """
        cmd = bytearray.fromhex("FF55040333F5") + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Turn\s+on\s+Vol|Start\s+Vol")
        return self._parse_db_volume(resp, "turn-on volume")

    async def set_output_balance(self, output_channel: int, balance_db: float) -> None:
        """
        Set the left/right balance for an output channel.

        Args:
            output_channel: 1-based output channel index.
            balance_db: Balance in dB (-12.0 left .. +12.0 right, 0 center).
        Command: FF 55 04 03 31 <output> <(balance+12)*2>

        """
        val = self._encode_offset_db(
            balance_db, minimum=-_DB_OFFSET_LIMIT, maximum=_DB_OFFSET_LIMIT
        )
        cmd = bytearray.fromhex("FF55040331") + bytes([output_channel - 1, val])
        resp = await self._send_command(cmd, expect=r"Balance")
        self._log_protocol("Set balance for output %d: %s", output_channel, resp)

    async def get_output_balance(self, output_channel: int) -> float:
        """
        Get the left/right balance for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 55 04 03 31 F5 <output>
        Response: "... Balance : Bal Center" / "Bal L 12" / "Bal R 6"
        Returns:
            float: Balance in dB (-12.0 left .. +12.0 right, 0 center).

        """
        cmd = bytearray.fromhex("FF55040331F5") + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Bal")
        if re.search(r"Bal\s*Center", resp, re.IGNORECASE):
            return 0.0
        m = re.search(r"Bal\s*([LR])\s*(\d+(?:\.\d+)?)", resp, re.IGNORECASE)
        if m:
            magnitude = float(m.group(2))
            return -magnitude if m.group(1).upper() == "L" else magnitude
        _LOGGER.warning("Could not parse balance from response: %s", resp)
        msg = "Unparseable balance response"
        raise TransientDeviceError(msg)

    async def set_output_loudness(self, output_channel: int, *, on: bool) -> None:
        """
        Enable or disable loudness compensation for an output channel.

        Args:
            output_channel: 1-based output channel index.
            on: True to enable loudness.
        Commands: on: FF 55 03 03 1A <output>, off: FF 55 03 03 1B <output>

        """
        base = "FF5503031A" if on else "FF5503031B"
        cmd = bytearray.fromhex(base) + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Loudness")
        self._log_protocol("Set loudness for output %d: %s", output_channel, resp)

    async def get_output_loudness(self, output_channel: int) -> bool:
        """
        Return True if loudness compensation is enabled for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 55 04 03 1A F5 <output>
        Response: "Get Out[N] Loudness status : On|Off"

        """
        cmd = bytearray.fromhex("FF5504031AF5") + bytes([output_channel - 1])
        resp = await self._send_command(cmd, expect=r"Loudness")
        return bool(re.search(r":\s*On\b", resp, re.IGNORECASE))

    async def set_output_delay(self, output_channel: int, delay_ms: int) -> None:
        """
        Set the audio delay for an output channel.

        Args:
            output_channel: 1-based output channel index.
            delay_ms: Delay in milliseconds (0..80).
        Command: FF 56 04 03 09 <output> <delay>

        """
        delay = max(0, min(int(delay_ms), 80))
        cmd = self._ff56(0x03, 0x09, output_channel - 1, delay)
        resp = await self._send_command(cmd, expect=r"Delay")
        self._log_protocol("Set output delay for output %d: %s", output_channel, resp)

    async def get_output_delay(self, output_channel: int) -> int:
        """
        Get the audio delay in milliseconds for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 56 04 03 09 F5 <output>
        Returns:
            int: Delay in milliseconds (0..80).

        """
        cmd = self._ff56(0x03, 0x09, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Delay")
        return int(self._require_last_number(resp, "output delay"))

    async def set_output_mode(self, output_channel: int, mode: int) -> None:
        """
        Set the DSP output mode for an output channel.

        Args:
            output_channel: 1-based output channel index.
            mode: One of the ``OUTPUT_MODE_*`` constants (0=DSP bypass,
                1=stereo, 2=mono, 3=2.1 stereo, 4=2.1 mono, 5=test signal).
        Command: FF 56 04 03 0B <output> <mode>

        """
        cmd = self._ff56(0x03, 0x0B, output_channel - 1, mode)
        resp = await self._send_command(cmd, expect=r"Select|Stereo|Mono|Test|Bypass")
        self._log_protocol("Set output mode for output %d: %s", output_channel, resp)

    async def get_output_mode(self, output_channel: int) -> int | None:
        """
        Get the DSP output mode for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 56 04 03 0B F5 <output>
        Response: "Get Out[N] DSP Stereo" / "DSP Bypass Stereo" /
            "Mono Sum" / "Test Signal" (2.1 variants include "2.1").

        Returns:
            int | None: One of the ``OUTPUT_MODE_*`` constants, or None if
            the response text is not recognized.

        """
        cmd = self._ff56(0x03, 0x0B, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Stereo|Mono|Test|Bypass")
        lowered = resp.lower()
        if "bypass" in lowered:
            return OUTPUT_MODE_DSP_BYPASS
        if "test" in lowered:
            return OUTPUT_MODE_TEST
        if "2.1" in lowered:
            return OUTPUT_MODE_21_MONO if "mono" in lowered else OUTPUT_MODE_21_STEREO
        if "mono" in lowered:
            return OUTPUT_MODE_MONO
        if "stereo" in lowered:
            return OUTPUT_MODE_STEREO
        _LOGGER.warning("Could not parse output mode from response: %s", resp)
        return None

    # ------------------------------------------------------------------
    # Shelf filters (bass / treble tone controls)
    # ------------------------------------------------------------------

    async def set_output_shelf(
        self,
        output_channel: int,
        shelf: ShelfName,
        param: FilterParam,
        value: float,
    ) -> None:
        """
        Set a low/high shelf filter parameter for an output channel.

        Args:
            output_channel: 1-based output channel index.
            shelf: "low" (bass) or "high" (treble).
            param: "frequency" (Hz), "gain" (dB) or "q".
        Command: FF 56 07 03 <opcode> <output> <int32 value>
            value: Frequency in Hz, or gain/Q scaled by 100 on the wire.

        """
        opcode = _SHELF_OPCODES[(shelf, param)]
        raw = value if param == "frequency" else value * 100
        cmd = self._ff56(0x03, opcode, output_channel - 1, self._encode_int32(raw))
        resp = await self._send_command(cmd, expect=r"Shelf")
        self._log_protocol(
            "Set %s shelf %s for output %d: %s", shelf, param, output_channel, resp
        )

    async def get_output_shelf(
        self, output_channel: int, shelf: ShelfName, param: FilterParam
    ) -> float:
        """
        Get a low/high shelf filter parameter for an output channel.

        Args:
            output_channel: 1-based output channel index.
            shelf: "low" (bass) or "high" (treble).
            param: "frequency" (Hz), "gain" (dB) or "q".
        Command: FF 56 04 03 <opcode> F5 <output>
        Returns:
            float: Frequency in Hz, or gain in dB, or Q factor.

        """
        opcode = _SHELF_OPCODES[(shelf, param)]
        cmd = self._ff56(0x03, opcode, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Shelf")
        return self._require_last_number(resp, f"{shelf} shelf {param}")

    # ------------------------------------------------------------------
    # 12-band room EQ
    # ------------------------------------------------------------------

    @staticmethod
    def _eq_sub_opcode(band: int, param: FilterParam) -> int:
        """Return the room EQ sub-opcode for a 1-based band and parameter."""
        if not 1 <= band <= ROOM_EQ_BAND_COUNT:
            msg = f"EQ band must be 1..{ROOM_EQ_BAND_COUNT}, got {band}"
            raise ValueError(msg)
        return (band - 1) * 0x10 + _EQ_PARAM_OFFSETS[param]

    async def set_room_eq(
        self,
        output_channel: int,
        band: int,
        param: FilterParam,
        value: float,
    ) -> None:
        """
        Set a room EQ band parameter for an output channel.

        Args:
            output_channel: 1-based output channel index.
            band: 1-based EQ band (1..12).
            param: "frequency" (Hz), "gain" (dB) or "q".
        Command: FF 56 07 05 <(band-1)*0x10 + offset> <output> <int32 value>
            value: Frequency in Hz, or gain/Q scaled by 100 on the wire.

        """
        sub = self._eq_sub_opcode(band, param)
        raw = value if param == "frequency" else value * 100
        cmd = self._ff56(0x05, sub, output_channel - 1, self._encode_int32(raw))
        resp = await self._send_command(cmd, expect=r"Band")
        self._log_protocol(
            "Set EQ band %d %s for output %d: %s", band, param, output_channel, resp
        )

    async def get_room_eq(
        self, output_channel: int, band: int, param: FilterParam
    ) -> float:
        """
        Get a room EQ band parameter for an output channel.

        Args:
            output_channel: 1-based output channel index.
            band: 1-based EQ band (1..12).
            param: "frequency" (Hz), "gain" (dB) or "q".
        Command: FF 56 04 05 <(band-1)*0x10 + offset> F5 <output>
        Returns:
            float: Frequency in Hz, or gain in dB, or Q factor.

        """
        sub = self._eq_sub_opcode(band, param)
        cmd = self._ff56(0x05, sub, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Band")
        return self._require_last_number(resp, f"EQ band {band} {param}")

    async def set_room_eq_lock(self, output_channel: int, *, locked: bool) -> None:
        """
        Lock or unlock the room EQ settings for an output channel.

        Args:
            output_channel: 1-based output channel index.
            locked: True to lock the room EQ.
        Command: FF 56 04 03 08 <output> <01|00>

        """
        cmd = self._ff56(0x03, 0x08, output_channel - 1, 0x01 if locked else 0x00)
        resp = await self._send_command(cmd, expect=r"Room\s+EQ")
        self._log_protocol("Set EQ lock for output %d: %s", output_channel, resp)

    async def get_room_eq_lock(self, output_channel: int) -> bool:
        """
        Return True if the room EQ is locked for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 56 04 03 08 F5 <output>

        """
        cmd = self._ff56(0x03, 0x08, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Room\s+EQ")
        return bool(re.search(r"\bOn\b", resp, re.IGNORECASE))

    # ------------------------------------------------------------------
    # 2.1 crossover settings
    # ------------------------------------------------------------------

    async def set_crossover_frequency(
        self, output_channel: int, frequency_hz: int
    ) -> None:
        """
        Set the 2.1 crossover frequency for an output channel.

        Args:
            output_channel: 1-based output channel index.
            frequency_hz: Crossover frequency in Hz.
        Command: FF 56 07 03 05 <output> <int32 frequency>

        """
        cmd = self._ff56(
            0x03, 0x05, output_channel - 1, self._encode_int32(frequency_hz)
        )
        resp = await self._send_command(cmd, expect=r"Crossover")
        self._log_protocol(
            "Set crossover frequency for output %d: %s", output_channel, resp
        )

    async def get_crossover_frequency(self, output_channel: int) -> int:
        """
        Get the 2.1 crossover frequency in Hz for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 56 04 03 05 F5 <output>
        Returns:
            int: Crossover frequency in Hz.

        """
        cmd = self._ff56(0x03, 0x05, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Crossover")
        return int(self._require_last_number(resp, "crossover frequency"))

    async def set_crossover_type(self, output_channel: int, crossover: int) -> None:
        """
        Set the 2.1 crossover filter type for an output channel.

        Args:
            output_channel: 1-based output channel index.
            crossover: Value from ``CROSSOVER_TYPES`` (0..5).
        Command: FF 56 04 03 06 <output> <type>

        """
        cmd = self._ff56(0x03, 0x06, output_channel - 1, crossover)
        resp = await self._send_command(cmd, expect=r"Crossover")
        self._log_protocol("Set crossover type for output %d: %s", output_channel, resp)

    async def get_crossover_type(self, output_channel: int) -> int | None:
        """
        Get the 2.1 crossover filter type for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 56 04 03 06 F5 <output>
        Response: e.g. "Get Out[N] Crossover Type 12dB/Oct Butterworth"
        Returns:
            int | None: Value from ``CROSSOVER_TYPES``, or None if the
            response text is not recognized.

        """
        cmd = self._ff56(0x03, 0x06, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Crossover")
        slope = re.search(r"(12|24|48)\s*dB", resp, re.IGNORECASE)
        if slope is None:
            _LOGGER.warning("Could not parse crossover type from response: %s", resp)
            return None
        family = (
            "linkwitz_riley"
            if re.search(r"linkwitz", resp, re.IGNORECASE)
            else "butterworth"
        )
        return CROSSOVER_TYPES[f"{family}_{slope.group(1)}"]

    async def set_sub_volume_offset(
        self, output_channel: int, offset_db: float
    ) -> None:
        """
        Set the 2.1 subwoofer volume offset for an output channel.

        Args:
            output_channel: 1-based output channel index.
            offset_db: Offset in dB (-12.0 .. 0.0).
        Command: FF 56 04 03 07 <output> <(offset+12)*2>

        """
        val = self._encode_offset_db(offset_db, minimum=_SUB_OFFSET_MIN_DB, maximum=0.0)
        cmd = self._ff56(0x03, 0x07, output_channel - 1, val)
        resp = await self._send_command(cmd, expect=r"Gain\s+Offset|Offset")
        self._log_protocol(
            "Set sub volume offset for output %d: %s", output_channel, resp
        )

    async def get_sub_volume_offset(self, output_channel: int) -> float:
        """
        Get the 2.1 subwoofer volume offset in dB for an output channel.

        Args:
            output_channel: 1-based output channel index.
        Command: FF 56 04 03 07 F5 <output>
        Response: "Get Out[N] Low Pass Gain Offset -10.5"
        Returns:
            float: Offset in dB (-12.0 .. 0.0).

        """
        cmd = self._ff56(0x03, 0x07, 0xF5, output_channel - 1)
        resp = await self._send_command(cmd, expect=r"Gain\s+Offset|Offset")
        return self._require_last_number(resp, "sub volume offset")

    # ------------------------------------------------------------------
    # Test tone
    # ------------------------------------------------------------------

    async def set_test_tone_volume(self, output_channel: int, volume_db: float) -> None:
        """
        Set the test tone volume for an output channel.

        Args:
            output_channel: 1-based output channel index.
            volume_db: Volume in dB (-24.0 .. 0.0, 0.5 dB steps).
        Command: FF 56 04 04 01 <output> <(volume+24)*2>

        """
        val = self._encode_offset_db(volume_db, minimum=_TEST_TONE_MIN_DB, maximum=0.0)
        cmd = self._ff56(0x04, 0x01, output_channel - 1, val)
        resp = await self._send_command(cmd, expect=r"Test\s+Gain|Test")
        self._log_protocol(
            "Set test tone volume for output %d: %s", output_channel, resp
        )

    # ------------------------------------------------------------------
    # Device-level commands
    # ------------------------------------------------------------------

    async def reboot_device(self) -> None:
        """
        Reboot the device.

        Command: FF 55 03 06 B4 00
        Response: "Reboot Command"
        """
        cmd = bytearray.fromhex("FF550306B400")
        resp = await self._send_command(cmd, expect=r"Reboot")
        _LOGGER.info("Requested device reboot")
        self._log_protocol("Reboot response: %s", resp)

    async def get_firmware_version(self) -> str:
        """
        Get the device firmware version string.

        Command: FF 55 03 06 65
        Response: "Fw version : V1.0054"
        Returns:
            str: Firmware version (e.g. "1.0054").

        """
        cmd = bytearray.fromhex("FF55030665")
        resp = await self._send_command(cmd, expect=r"Fw\s+version")
        m = re.search(r"Fw\s+version\s*:\s*V?([\w.]+)", resp, re.IGNORECASE)
        if m:
            return m.group(1)
        _LOGGER.warning("Could not parse firmware version from response: %s", resp)
        msg = "Unparseable firmware version response"
        raise TransientDeviceError(msg)

    async def get_mac_address(self) -> str:
        """
        Get the device MAC address.

        Command: FF 55 03 08 80 F5
        Response: "Get MAC Add 00:0F:FF:61:C8:E9"
        Returns:
            str: MAC address in colon-separated form.

        """
        cmd = bytearray.fromhex("FF55030880F5")
        resp = await self._send_command(cmd, expect=r"MAC")
        m = re.search(r"((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", resp)
        if m:
            return m.group(1)
        _LOGGER.warning("Could not parse MAC address from response: %s", resp)
        msg = "Unparseable MAC address response"
        raise TransientDeviceError(msg)

    async def get_power_status(self) -> str:
        """
        Get the device power status text.

        Command: FF 55 03 01 01 F5
        Response: "Get Power status : Working"
        Returns:
            str: Power status token (e.g. "Working").

        """
        cmd = bytearray.fromhex("FF55030101F5")
        resp = await self._send_command(cmd, expect=r"Power\s+status")
        m = re.search(r"Power\s+status\s*:\s*(\w+)", resp, re.IGNORECASE)
        return m.group(1) if m else resp
