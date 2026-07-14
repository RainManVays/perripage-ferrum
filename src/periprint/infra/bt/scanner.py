from __future__ import annotations

import queue
import re
import threading
from typing import Any

from periprint.infra.bt.bluetoothctl_backend import scan_for_devices
from periprint.services.events import EventType

# Real Peripage devices show up under two naming conventions in the wild:
# the library/README's "PeriPage+XXXX", and this project's tested unit,
# "PPG_A40_XXXX" (see docs/hardware-notes.md). Both also advertise a "_BLE"
# twin that isn't usable via peripage's Bluetooth Classic SPP transport.
_PERIPAGE_NAME_RE = re.compile(r"^(peripage|ppg_)", re.IGNORECASE)


def is_peripage_device(name: str) -> bool:
    return bool(_PERIPAGE_NAME_RE.match(name)) and not name.upper().endswith("_BLE")


class BluetoothScanner:
    """Runs a timed discovery in a background thread and reports results
    through a thread-safe event queue, so the Tk mainloop never blocks."""

    def scan_async(
        self, event_queue: queue.Queue[tuple[EventType, Any]], timeout_seconds: int = 12
    ) -> None:
        thread = threading.Thread(
            target=self._scan_worker, args=(event_queue, timeout_seconds), daemon=True
        )
        thread.start()

    def _scan_worker(
        self, event_queue: queue.Queue[tuple[EventType, Any]], timeout_seconds: int
    ) -> None:
        try:
            devices = scan_for_devices(timeout_seconds)
            peripage_devices = [d for d in devices if is_peripage_device(d.name)]
            event_queue.put((EventType.SCAN_RESULT, peripage_devices))
        except Exception as exc:
            event_queue.put((EventType.SCAN_ERROR, str(exc)))
