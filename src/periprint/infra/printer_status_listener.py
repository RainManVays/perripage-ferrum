from __future__ import annotations

import logging
import os
import select
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Tag/sub-tag meanings reverse-engineered from the official Peripage Android
# app (decompiled, not just inferred from a live trace) — see
# docs/bluetooth-protocol-trace-analysis.md §7.3 for the full derivation and
# confidence notes. Only (0xFD, 1) and (0xFD, 2) were actually observed on
# the wire in our own capture; the 0xFF family is confirmed by code only —
# log raw bytes rather than silently trusting this table when integrating
# against a different printer/firmware.
STATUS_MEANINGS: dict[tuple[int, int], str] = {
    (0xFF, 1): "out_of_paper",
    (0xFF, 2): "cover_open",
    (0xFF, 3): "overheat",
    (0xFF, 4): "low_battery",
    (0xFF, 5): "cover_closed",
    (0xFF, 6): "low_mileage",
    (0xFD, 1): "abort_print",
    (0xFD, 2): "resume_print",
}

# Statuses that mean "something is actually wrong, printing cannot safely
# continue" — used by PrintJobManager to decide whether to pause a job.
# cover_closed/resume_print are resolutions, not problems; deliberately
# excluded.
PAUSE_WORTHY_STATUSES = frozenset(
    {"out_of_paper", "cover_open", "overheat", "low_battery", "low_mileage", "abort_print"}
)


class PrinterStatusListener:
    """Background thread reading spontaneous 2-byte status packets the
    printer can push at any time (not just in response to a query) — e.g.
    "abort_print" when the user opens the cover mid-job. Reads the same raw
    fd that PeripageClient's send/recv workaround already uses (see
    infra/peripage_client.py::_patch_socket_send_recv) via select() so it
    never blocks waiting for data that may never come.

    Must not run concurrently with a blocking askPrinter() call on the same
    fd — both would race to read the same incoming bytes. Callers should
    only run this while actively sending image data (which is send-only,
    no recv), not around query calls."""

    def __init__(self, sock: object, on_event: Callable[[str, int], None]) -> None:
        self._fd = sock.fileno()  # type: ignore[attr-defined]
        self._on_event = on_event
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
            except OSError:
                return
            if not ready:
                continue
            try:
                data = os.read(self._fd, 2)
            except OSError:
                return
            if len(data) != 2:
                continue
            tag, sub = data[0], data[1]
            if tag == 0xFE:
                self._on_event("paper_type_mismatch", sub)
                continue
            meaning = STATUS_MEANINGS.get((tag, sub))
            if meaning is not None:
                self._on_event(meaning, sub)
            else:
                logger.info("Unrecognized printer status packet: tag=%#x sub=%#x", tag, sub)
