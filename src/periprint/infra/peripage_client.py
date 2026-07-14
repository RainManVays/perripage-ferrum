from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from periprint.infra.raw_printer_protocol import RawPrinter
from periprint.models.enums import PrinterModel

if TYPE_CHECKING:
    import PIL.Image

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_BACKOFF_BASE_SECONDS = 1.0


class PeripageConnectionError(Exception):
    """Raised when connect/reconnect fails after all retry attempts."""


def _patch_socket_send_recv(raw_socket: Any) -> None:
    """WORKAROUND (verified on this project's tested python3-bluez build,
    see docs/BLUETOOTH_SETUP.md and docs/hardware-notes.md): PyBluez's own
    BluetoothSocket.send()/.recv() raise OSError(14, "Bad address") even
    though the underlying fd works fine. Bypass PyBluez's C-level send/recv
    and talk to the fd directly. `settimeout()` (called by peripage.Printer
    .connect()) leaves the fd non-blocking, so os.set_blocking(True) is
    required too or a plain os.read() raises BlockingIOError instead of
    waiting for data."""
    fd = raw_socket.fileno()
    os.set_blocking(fd, True)
    raw_socket.send = lambda data: os.write(fd, data)
    raw_socket.recv = lambda n: os.read(fd, n)


def _default_printer_factory(mac: str, model: PrinterModel) -> RawPrinter:
    import peripage  # lazy: only needed once we actually talk to hardware

    printer_type = peripage.PrinterType[model.value]
    return peripage.Printer(mac, printer_type)


class PeripageClient:
    """Thin wrapper over a RawPrinter (peripage.Printer by default) adding
    explicit connect/disconnect/is_connected, retrying reconnect, and the
    PyBluez send/recv workaround. No chunking/queueing here — that's
    PrintJobManager/DocumentPipeline (Stage 3/4)."""

    def __init__(
        self,
        mac: str,
        model: PrinterModel,
        concentration: int = 2,
        printer_factory: Callable[[str, PrinterModel], RawPrinter] = _default_printer_factory,
    ) -> None:
        self._mac = mac
        self._model = model
        self._concentration = concentration
        self._printer_factory = printer_factory
        self._printer: RawPrinter | None = None

    def is_connected(self) -> bool:
        return self._printer is not None and self._printer.isConnected()

    def connect(self) -> None:
        """Single connection attempt, no retry. Always re-runs reset() +
        setConcentration() after the socket is up — required after any
        (re)connect or the printer won't respond to print/query commands."""
        printer = self._printer_factory(self._mac, self._model)
        printer.connect()

        raw_socket = getattr(printer, "sock", None)
        if raw_socket is not None and hasattr(raw_socket, "fileno"):
            _patch_socket_send_recv(raw_socket)

        printer.reset()
        printer.setConcentration(self._concentration, wait=True)
        self._printer = printer

    def reconnect(self) -> None:
        """Reconnect with exponential backoff, up to MAX_RECONNECT_ATTEMPTS.
        Use this (not bare connect()) for anything user-facing — the initial
        connect from the UI included — so transient failures self-heal."""
        last_error: Exception | None = None
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            try:
                self.connect()
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Connect attempt %d/%d failed: %s", attempt, MAX_RECONNECT_ATTEMPTS, exc
                )
                if attempt < MAX_RECONNECT_ATTEMPTS:
                    time.sleep(RECONNECT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        raise PeripageConnectionError(
            f"Failed to connect after {MAX_RECONNECT_ATTEMPTS} attempts"
        ) from last_error

    def disconnect(self) -> None:
        if self._printer is not None:
            self._printer.disconnect()
            self._printer = None

    def print_image(self, image: PIL.Image.Image, delay: float = 0.05) -> None:
        if self._printer is None:
            raise RuntimeError("Not connected")
        self._printer.printImage(image, delay=delay)

    def print_break(self, size: int = 60) -> None:
        if self._printer is None:
            raise RuntimeError("Not connected")
        self._printer.printBreak(size)

    def get_battery_percent(self) -> int:
        if self._printer is None:
            raise RuntimeError("Not connected")
        return self._printer.getDeviceBattery()
