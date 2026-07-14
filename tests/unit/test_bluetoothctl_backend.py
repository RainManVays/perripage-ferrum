from periprint.infra.bt.bluetoothctl_backend import (
    DiscoveredDevice,
    _parse_devices_list_output,
    _parse_new_device_lines,
)

# Real ANSI-colored sample captured from `bluetoothctl --timeout N scan on`
# on a real machine (BlueZ 5.72) — see docs/hardware-notes.md. Includes a
# [CHG] property-change line for an already-known device (28:D4:1E:01:34:C4)
# using the exact same "Device MAC ..." shape as a [NEW] line — this is the
# real bug found live: a non-tag-aware parser clobbers the real device name
# ("PPG_A40_34C4") with the property text ("LegacyPairing: yes").
SCAN_OUTPUT_WITH_ANSI = (
    "SetDiscoveryFilter success\n"
    "Discovery started\n"
    "[\x1b[0;93mCHG\x1b[0m] Controller 08:BF:B8:54:AB:7E Discovering: yes\n"
    "[\x1b[0;92mNEW\x1b[0m] Device 28:D4:1E:01:34:C4 PPG_A40_34C4\n"
    "[\x1b[0;92mNEW\x1b[0m] Device E8:D4:1E:01:34:C4 PPG_A40_34C4_BLE\n"
    "[\x1b[0;92mNEW\x1b[0m] Device 00:D2:56:52:B3:83 00-D2-56-52-B3-83\n"
    "[\x1b[0;93mCHG\x1b[0m] Device 28:D4:1E:01:34:C4 LegacyPairing: yes\n"
)

DEVICES_OUTPUT_PLAIN = (
    "Device 28:D4:1E:01:34:C4 PPG_A40_34C4\n"
    "Device 30:92:DD:25:E2:41 T17\n"
    "Device 00:1E:7C:CC:5C:D4 HAYLOU S35 ANC\n"
)


def test_parse_new_lines_strips_ansi_and_extracts_mac_and_name() -> None:
    devices = _parse_new_device_lines(SCAN_OUTPUT_WITH_ANSI)

    assert devices["28:D4:1E:01:34:C4"] == DiscoveredDevice(
        mac="28:D4:1E:01:34:C4", name="PPG_A40_34C4"
    )
    assert devices["E8:D4:1E:01:34:C4"].name == "PPG_A40_34C4_BLE"
    assert len(devices) == 3


def test_parse_new_lines_ignores_chg_property_lines() -> None:
    """Regression test: a [CHG] line for an already-known device must not
    overwrite its real [NEW] name with property-change text."""
    devices = _parse_new_device_lines(SCAN_OUTPUT_WITH_ANSI)

    assert devices["28:D4:1E:01:34:C4"].name == "PPG_A40_34C4"
    assert "LegacyPairing" not in devices["28:D4:1E:01:34:C4"].name


def test_parse_devices_list_output() -> None:
    devices = _parse_devices_list_output(DEVICES_OUTPUT_PLAIN)

    assert set(devices.keys()) == {
        "28:D4:1E:01:34:C4",
        "30:92:DD:25:E2:41",
        "00:1E:7C:CC:5C:D4",
    }
    assert devices["30:92:DD:25:E2:41"].name == "T17"


def test_parse_empty_output() -> None:
    assert _parse_new_device_lines("") == {}
    assert _parse_devices_list_output("") == {}
