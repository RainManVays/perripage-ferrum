from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# Verified against a real system (BlueZ 5.72): `bluetoothctl` emits ANSI
# color codes around event tags like [NEW]/[CHG] even when stdout is a
# subprocess pipe, not a real tty. Must strip before matching.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# `bluetoothctl devices` output: plain "Device MAC Name" lines, no tag.
_DEVICES_LIST_LINE_RE = re.compile(r"^Device (?P<mac>[0-9A-Fa-f:]{17}) (?P<name>.+)$")

# `bluetoothctl scan on` output mixes several tags for the SAME "Device MAC
# ..." shape: [NEW] announces an actual device name, but [CHG] reports a
# property change (e.g. "[CHG] Device AA:BB LegacyPairing: yes") using an
# identical textual layout. A regex that doesn't anchor on [NEW] will treat
# "LegacyPairing: yes" as if it were the device's name and clobber the real
# one for any device bluetoothd already knew about — verified live on real
# hardware (see docs/hardware-notes.md). Only match [NEW] lines here.
_NEW_DEVICE_LINE_RE = re.compile(r"^\[NEW\] Device (?P<mac>[0-9A-Fa-f:]{17}) (?P<name>.+)$")


@dataclass(frozen=True)
class DiscoveredDevice:
    mac: str
    name: str


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _parse_devices_list_output(output: str) -> dict[str, DiscoveredDevice]:
    devices: dict[str, DiscoveredDevice] = {}
    for line in output.splitlines():
        match = _DEVICES_LIST_LINE_RE.match(line.strip())
        if match:
            mac = match.group("mac")
            devices[mac] = DiscoveredDevice(mac=mac, name=match.group("name").strip())
    return devices


def _parse_new_device_lines(output: str) -> dict[str, DiscoveredDevice]:
    devices: dict[str, DiscoveredDevice] = {}
    for line in _strip_ansi(output).splitlines():
        match = _NEW_DEVICE_LINE_RE.match(line.strip())
        if match:
            mac = match.group("mac")
            devices[mac] = DiscoveredDevice(mac=mac, name=match.group("name").strip())
    return devices


def list_known_devices() -> list[DiscoveredDevice]:
    """Devices bluetoothd already knows about (paired or previously seen) —
    cheap, no scanning required. A device bluetoothd has already cached may
    not re-emit a `[NEW]` line during a later scan, so this must be merged
    with fresh discovery results in `scan_for_devices`."""
    result = subprocess.run(
        ["bluetoothctl", "devices"], capture_output=True, text=True, timeout=10, check=False
    )
    return list(_parse_devices_list_output(result.stdout).values())


def scan_for_devices(timeout_seconds: int = 12) -> list[DiscoveredDevice]:
    """Power on the adapter and run a one-shot timed discovery via
    `bluetoothctl --timeout N scan on`, merged with already-known devices.
    Blocks for ~timeout_seconds — call from a background thread."""
    subprocess.run(
        ["bluetoothctl", "power", "on"], capture_output=True, text=True, timeout=10, check=False
    )

    known = {device.mac: device for device in list_known_devices()}

    result = subprocess.run(
        ["bluetoothctl", "--timeout", str(timeout_seconds), "scan", "on"],
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 10,
        check=False,
    )
    combined_output = result.stdout + result.stderr
    if "No default controller available" in combined_output:
        raise RuntimeError("Bluetooth adapter not available — is Bluetooth enabled?")

    discovered = _parse_new_device_lines(result.stdout)

    # `known` (from `bluetoothctl devices`) wins on conflict: it reflects
    # bluetoothd's settled view of the device's name, whereas a [NEW] line
    # during THIS scan is only ever seen for devices bluetoothd did NOT
    # already have cached (see docstring above) — but merge defensively in
    # this order in case that assumption ever doesn't hold on some BlueZ
    # version.
    merged = dict(discovered)
    merged.update(known)
    return list(merged.values())
