# Touched by AI 2026-07-10 (Claude Code): new tests for the extended
# protocol commands reverse-engineered from the Control4 driver.
"""
Unit tests for the extended TriadConnection protocol commands.

Wire frames and response strings in these tests are taken verbatim from
packet captures of the official Control4 driver controlling an AMS-16.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.triad_ams.connection import (
    CROSSOVER_TYPES,
    OUTPUT_MODE_21_STEREO,
    OUTPUT_MODE_DSP_BYPASS,
    OUTPUT_MODE_MONO,
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_TEST,
    TriadConnection,
)
from custom_components.triad_ams.exceptions import TransientDeviceError
from tests.conftest import create_async_mock_method


def _wired_connection(*responses: str) -> tuple[TriadConnection, MagicMock]:
    """Return a connection with a mocked transport and canned responses."""
    conn = TriadConnection("192.0.2.100", 52000)
    reader = MagicMock()
    pending = [r.encode() + b"\x00" for r in responses]
    reader.readuntil = create_async_mock_method(
        side_effect=lambda *_args, **_kwargs: pending.pop(0)
    )
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = create_async_mock_method()
    conn._reader = reader
    conn._writer = writer
    return conn, writer


def _sent_bytes(writer: MagicMock) -> bytes:
    """Return the bytes written by the last command."""
    return bytes(writer.write.call_args[0][0])


class TestInputSettings:
    """Tests for input gain, delay, and audio sense."""

    async def test_set_input_gain_encodes_offset(self) -> None:
        """+12 dB on input 1 encodes as 0x30 (pcap-verified)."""
        conn, writer = _wired_connection("Set In[1] input gain : 12")
        await conn.set_input_gain(1, 12)
        assert _sent_bytes(writer) == bytes.fromhex("ff550402040030")

    async def test_set_input_gain_negative(self) -> None:
        """-12 dB on input 16 encodes as 0x00 (pcap-verified)."""
        conn, writer = _wired_connection("Set In[16] input gain : -12")
        await conn.set_input_gain(16, -12)
        assert _sent_bytes(writer) == bytes.fromhex("ff550402040f00")

    async def test_get_input_gain_parses_db(self) -> None:
        """Gain query parses the dB value from the response."""
        conn, _ = _wired_connection("Get In[16] input gain : -12")
        assert await conn.get_input_gain(16) == -12

    async def test_set_input_delay_frame(self) -> None:
        """Delay of 80 ms on input 1 uses the FF56 frame (pcap-verified)."""
        conn, writer = _wired_connection("Set Input[1] Delay 80")
        await conn.set_input_delay(1, 80)
        assert _sent_bytes(writer) == bytes.fromhex("ff560402040050")

    async def test_get_input_delay_parses_ms(self) -> None:
        """Delay query parses the millisecond value."""
        conn, _ = _wired_connection("Get Input[16] Delay 0")
        assert await conn.get_input_delay(16) == 0

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ("Get Input[10] Audio Detect Detected", True),
            ("Get Input[10] Audio Detect Undetected", False),
        ],
    )
    async def test_get_input_audio_sense(
        self, response: str, *, expected: bool
    ) -> None:
        """Audio sense parses Detected vs Undetected."""
        conn, writer = _wired_connection(response)
        assert await conn.get_input_audio_sense(10) is expected
        assert _sent_bytes(writer) == bytes.fromhex("ff56040203f509")


class TestOutputLevels:
    """Tests for max volume, turn-on volume, balance, and loudness."""

    async def test_set_output_max_volume_frame(self) -> None:
        """Max volume 100% encodes as 0x64."""
        conn, writer = _wired_connection("Set Out[13] Max Volume to 0")
        await conn.set_output_max_volume(13, 1.0)
        assert _sent_bytes(writer) == bytes.fromhex("ff5504031f0c64")

    async def test_get_output_turn_on_volume_parses_db(self) -> None:
        """Turn-on volume query maps dB through the volume LUT."""
        conn, _ = _wired_connection("Get Out[13] Turn on Vol : -23.1")
        value = await conn.get_output_turn_on_volume(13)
        assert 0.0 < value < 1.0

    async def test_set_output_balance_frame(self) -> None:
        """Balance -12 dB (full left) encodes as 0x00."""
        conn, writer = _wired_connection("Set Out[13] Balance to Bal L 12")
        await conn.set_output_balance(13, -12)
        assert _sent_bytes(writer) == bytes.fromhex("ff550403310c00")

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ("Get Out[13] Balance : Bal Center", 0.0),
            ("Get Out[13] Balance : Bal L 12", -12.0),
            ("Get Out[13] Balance : Bal R 6", 6.0),
        ],
    )
    async def test_get_output_balance_parses(
        self, response: str, expected: float
    ) -> None:
        """Balance query parses center/left/right responses."""
        conn, _ = _wired_connection(response)
        assert await conn.get_output_balance(13) == expected

    async def test_set_output_loudness_frames(self) -> None:
        """Loudness on/off use opcodes 0x1A/0x1B (pcap-verified)."""
        conn, writer = _wired_connection("Set Out[13] Loudness On")
        await conn.set_output_loudness(13, on=True)
        assert _sent_bytes(writer) == bytes.fromhex("ff5503031a0c")
        conn, writer = _wired_connection("Set Out[13] Loudness Off")
        await conn.set_output_loudness(13, on=False)
        assert _sent_bytes(writer) == bytes.fromhex("ff5503031b0c")

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ("Get Out[13] Loudness status : On", True),
            ("Get Out[13] Loudness status : Off", False),
        ],
    )
    async def test_get_output_loudness(self, response: str, *, expected: bool) -> None:
        """Loudness query parses On/Off."""
        conn, _ = _wired_connection(response)
        assert await conn.get_output_loudness(13) is expected


class TestOutputMode:
    """Tests for the DSP output mode."""

    async def test_set_output_mode_frame(self) -> None:
        """Setting test signal mode sends mode byte 5."""
        conn, writer = _wired_connection("Set Out[13] Select:Test Signal")
        await conn.set_output_mode(13, OUTPUT_MODE_TEST)
        assert _sent_bytes(writer) == bytes.fromhex("ff5604030b0c05")

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ("Get Out[13] DSP Stereo", OUTPUT_MODE_STEREO),
            ("Get Out[13] DSP Bypass Stereo", OUTPUT_MODE_DSP_BYPASS),
            ("Get Out[13] Mono Sum", OUTPUT_MODE_MONO),
            ("Get Out[13] Test Signal", OUTPUT_MODE_TEST),
            ("Get Out[13] 2.1 Stereo", OUTPUT_MODE_21_STEREO),
        ],
    )
    async def test_get_output_mode_parses(self, response: str, expected: int) -> None:
        """Mode query maps the observed response texts."""
        conn, _ = _wired_connection(response)
        assert await conn.get_output_mode(13) == expected


class TestShelfFilters:
    """Tests for the bass/treble shelf filters."""

    async def test_set_low_shelf_frequency_frame(self) -> None:
        """Low shelf 100 Hz on output 13 (pcap-verified frame)."""
        conn, writer = _wired_connection("Set Out[13] Low Shelf Frequency to 100 Hz")
        await conn.set_output_shelf(13, "low", "frequency", 100)
        assert _sent_bytes(writer) == bytes.fromhex("ff560703030c00000064")

    async def test_set_low_shelf_gain_frame(self) -> None:
        """Low shelf +12 dB encodes gain*100 as int32 (pcap-verified)."""
        conn, writer = _wired_connection("Set Out[13] Low Shelf gain to 12")
        await conn.set_output_shelf(13, "low", "gain", 12)
        assert _sent_bytes(writer) == bytes.fromhex("ff5607030d0c000004b0")

    async def test_set_low_shelf_gain_negative_frame(self) -> None:
        """Low shelf -12 dB encodes as two's-complement int32 (pcap-verified)."""
        conn, writer = _wired_connection("Set Out[13] Low Shelf gain to -12")
        await conn.set_output_shelf(13, "low", "gain", -12)
        assert _sent_bytes(writer) == bytes.fromhex("ff5607030d0cfffffb50")

    async def test_get_high_shelf_q_parses(self) -> None:
        """Shelf Q query parses a decimal value."""
        conn, _ = _wired_connection("Get Out[13] High Shelf Q 0.58")
        assert await conn.get_output_shelf(13, "high", "q") == 0.58


class TestRoomEq:
    """Tests for the 12-band room EQ."""

    async def test_set_room_eq_gain_frame(self) -> None:
        """Band 10 gain 2 dB uses sub-opcode 0x91 (pcap-verified)."""
        conn, writer = _wired_connection("Set Out[13] Band 10 Gain to 2")
        await conn.set_room_eq(13, 10, "gain", 2)
        assert _sent_bytes(writer) == bytes.fromhex("ff560705910c000000c8")

    async def test_get_room_eq_frequency_parses_last_number(self) -> None:
        """Band frequency parses the trailing Hz value, not the band number."""
        conn, _ = _wired_connection("Get Out[13] Band 10 Freq : 3000 Hz")
        assert await conn.get_room_eq(13, 10, "frequency") == 3000

    async def test_room_eq_band_out_of_range(self) -> None:
        """Bands outside 1..12 are rejected."""
        conn, _ = _wired_connection()
        with pytest.raises(ValueError, match="EQ band"):
            await conn.set_room_eq(13, 13, "gain", 0)

    async def test_set_room_eq_lock_frame(self) -> None:
        """EQ lock uses opcode 0x08 with a boolean byte."""
        conn, writer = _wired_connection("Set Out[13] Lock Room EQ Off")
        await conn.set_room_eq_lock(13, locked=False)
        assert _sent_bytes(writer) == bytes.fromhex("ff560403080c00")


class TestCrossover:
    """Tests for the 2.1 crossover settings."""

    async def test_set_crossover_frequency_frame(self) -> None:
        """Crossover 80 Hz encodes as int32."""
        conn, writer = _wired_connection("Set Out[13] Crossover Frequency to 80 Hz")
        await conn.set_crossover_frequency(13, 80)
        assert _sent_bytes(writer) == bytes.fromhex("ff560703050c00000050")

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (
                "Get Out[13] Crossover Type 12dB/Oct Butterworth",
                CROSSOVER_TYPES["butterworth_12"],
            ),
            (
                "Get Out[13] Crossover Type 24dB/Oct Linkwitz-Riley",
                CROSSOVER_TYPES["linkwitz_riley_24"],
            ),
        ],
    )
    async def test_get_crossover_type_parses(
        self, response: str, expected: int
    ) -> None:
        """Crossover type parses slope and family from the response."""
        conn, _ = _wired_connection(response)
        assert await conn.get_crossover_type(13) == expected

    async def test_set_sub_volume_offset_frame(self) -> None:
        """Sub offset -12 dB encodes as 0x00."""
        conn, writer = _wired_connection("Set Out[13] Low Pass Gain Offset to -12")
        await conn.set_sub_volume_offset(13, -12)
        assert _sent_bytes(writer) == bytes.fromhex("ff560403070c00")

    async def test_get_sub_volume_offset_parses(self) -> None:
        """Sub offset query parses the dB value."""
        conn, _ = _wired_connection("Get Out[13] Low Pass Gain Offset -10.5")
        assert await conn.get_sub_volume_offset(13) == -10.5


class TestPaddedResponses:
    """Tests for fixed-width null-padded response handling."""

    async def test_consecutive_commands_skip_response_padding(self) -> None:
        """
        Leftover null padding must not desync subsequent commands.

        The AMS-16 v2 pads every response to 150 bytes with nulls; the
        reader must skip the padding of response N when reading response
        N+1 (observed live: without this, every command after the first
        sees an "empty" response).
        """
        frames = ["Get Power status : Working"] + [""] * 123 + ["Fw version : V1.0054"]
        conn, _ = _wired_connection(*frames)
        assert await conn.get_power_status() == "Working"
        assert await conn.get_firmware_version() == "1.0054"


class TestTestToneAndDevice:
    """Tests for test tone volume and device-level commands."""

    async def test_set_test_tone_volume_frame(self) -> None:
        """Test tone -15 dB encodes as (v+24)*2 = 18 (0x12)."""
        conn, writer = _wired_connection("Set Out[13] Test Gain -15")
        await conn.set_test_tone_volume(13, -15)
        assert _sent_bytes(writer) == bytes.fromhex("ff560404010c12")

    async def test_reboot_device_frame(self) -> None:
        """Reboot sends the pcap-verified frame."""
        conn, writer = _wired_connection("Reboot Command")
        await conn.reboot_device()
        assert _sent_bytes(writer) == bytes.fromhex("ff550306b400")

    async def test_get_firmware_version_parses(self) -> None:
        """Firmware query strips the leading V."""
        conn, writer = _wired_connection("Fw version : V1.0054")
        assert await conn.get_firmware_version() == "1.0054"
        assert _sent_bytes(writer) == bytes.fromhex("ff55030665")

    async def test_get_mac_address_parses(self) -> None:
        """MAC query extracts the colon-separated address."""
        conn, _ = _wired_connection("Get MAC Add 00:0F:FF:61:C8:E9")
        assert await conn.get_mac_address() == "00:0F:FF:61:C8:E9"

    async def test_get_mac_address_unparseable_raises(self) -> None:
        """Garbage MAC responses raise TransientDeviceError."""
        conn, _ = _wired_connection("Get MAC Add garbage")
        with pytest.raises(TransientDeviceError):
            await conn.get_mac_address()

    async def test_get_power_status_parses(self) -> None:
        """Power status query returns the status token."""
        conn, writer = _wired_connection("Get Power status : Working")
        assert await conn.get_power_status() == "Working"
        assert _sent_bytes(writer) == bytes.fromhex("ff55030101f5")
