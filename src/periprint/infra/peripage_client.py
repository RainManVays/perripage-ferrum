from __future__ import annotations

import logging
import os
import time
import zlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from periprint.infra.printer_status_listener import PrinterStatusListener
from periprint.infra.raw_printer_protocol import RawPrinter
from periprint.models.enums import PaperType, PrinterModel

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
    try:
        raw_socket.send = lambda data: os.write(fd, data)
        raw_socket.recv = lambda n: os.read(fd, n)
    except AttributeError:
        # Only PyBluez's specific BluetoothSocket needs this workaround.
        # Other socket-like objects (e.g. the stdlib socket.socket used in
        # tests, via socket.socketpair()) don't allow monkeypatching
        # instance methods and are assumed to already have working
        # send()/recv() of their own.
        logger.debug("Socket object does not support the send/recv patch; skipping")


def _default_printer_factory(mac: str, model: PrinterModel) -> RawPrinter:
    import peripage  # lazy: only needed once we actually talk to hardware

    printer_type = peripage.PrinterType[model.value]
    return peripage.Printer(mac, printer_type)


def _pack_1bpp_for_printer(image: PIL.Image.Image) -> bytes:
    """Our DocumentPipeline normalizes documents to mode '1' with
    background=on(255)/content=off(0) — see infra/renderers/base.py::
    normalize_to_1bit, which does no inversion. The printer's wire format
    is the opposite: bit=1 means "fire this dot" (content), bit=0 means
    blank. peripage.Printer.printImage() inverts internally before
    packing; anything that sends raw row bytes directly (bypassing
    printImage(), as the Phase 4/5 methods below do) must replicate that
    inversion itself or print a negative image."""
    import PIL.ImageOps

    inverted = PIL.ImageOps.invert(image.convert("L"))
    return inverted.convert("1").tobytes()


class PeripageClient:
    """Thin wrapper over a RawPrinter (peripage.Printer by default) adding
    explicit connect/disconnect/is_connected, retrying reconnect, and the
    PyBluez send/recv workaround. No chunking/queueing here — that's
    PrintJobManager/DocumentPipeline (Stage 3/4).

    Also exposes protocol capabilities the peripage library doesn't, found
    by decompiling the official Android app — see
    docs/bluetooth-protocol-trace-analysis.md and
    docs/printer-protocol-implementation-plan.md for the full derivation
    and per-method confidence/risk notes: raw concentration (no 0-2
    clamp), paper type selection, an honest 16-bit-height image send (no
    artificial 255-row-per-chunk slicing), the experimental zlib-compressed
    0x1f protocol, and a background listener for the printer's own
    spontaneous status pushes (out of paper, cover open, overheat, and the
    abort/resume-print signal a real HCI trace showed the printer sends
    mid-job)."""

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
        self._status_listener: PrinterStatusListener | None = None

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
        self.stop_status_listening()
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

    # -- Status listening (docs/printer-protocol-implementation-plan.md Phase 1) --

    def start_status_listening(self, on_event: Callable[[str, int], None]) -> None:
        """Starts a background listener for spontaneous printer status
        packets (paper/cover/overheat/battery, and — the most important
        one — abort/resume signals the printer itself sends mid-print; see
        docs/bluetooth-protocol-trace-analysis.md §4/§7.3). Only safe to
        run while not also making blocking askPrinter()-style query calls
        on the same connection — both would race to read the same
        incoming bytes on the fd. Intended to be started right before
        sending image data and stopped right after (see
        services/job_manager.py), not held open for the whole connection
        lifetime."""
        if self._printer is None:
            raise RuntimeError("Not connected")
        raw_socket = getattr(self._printer, "sock", None)
        if raw_socket is None:
            return
        self.stop_status_listening()
        self._status_listener = PrinterStatusListener(raw_socket, on_event)
        self._status_listener.start()

    def stop_status_listening(self) -> None:
        if self._status_listener is not None:
            self._status_listener.stop()
            self._status_listener = None

    # -- docs/printer-protocol-implementation-plan.md Phase 2 --

    def set_concentration_raw(self, value: int) -> None:
        """Bypasses peripage.Printer.setConcentration()'s hardcoded 0-2
        clamp: the official app's decompiled code sends this same opcode
        with no range check at all, and a live trace observed value 4 on
        the wire — see docs/bluetooth-protocol-trace-analysis.md §2 step 6.
        Confirmed only that the command is accepted without error, NOT
        that higher values visibly improve print density — verify on real
        hardware before making this the default."""
        if self._printer is None:
            raise RuntimeError("Not connected")
        value = max(0, min(255, value))
        self._printer.tellPrinter(bytes.fromhex("10ff1000") + bytes([value]))

    # -- docs/printer-protocol-implementation-plan.md Phase 3 --

    def choose_paper_type(self, paper_type: PaperType) -> None:
        """Opcode 10ff1003 — likely only meaningful for label-class
        printers (this project's A40 uses plain continuous roll paper),
        but cheap to expose. See
        docs/bluetooth-protocol-trace-analysis.md §7.2/§7.3."""
        if self._printer is None:
            raise RuntimeError("Not connected")
        self._printer.tellPrinter(bytes.fromhex("10ff1003") + bytes([int(paper_type)]))

    # -- docs/printer-protocol-implementation-plan.md Phase 4 --

    def print_image_no_height_limit(self, image: PIL.Image.Image, delay: float = 0.001) -> None:
        """Sends one image as a single legacy-protocol (0x1d7630) command
        with an honest 16-bit height, instead of going through
        peripage.Printer.printImage()/printRowBytesList(), which silently
        re-slices anything taller than 255 rows into multiple
        reset()+header+data groups — an artifact of the library encoding
        height in 1 byte, not a firmware limit (the library's own docstring
        already says as much; confirmed by the decompiled app, whose
        height field is a full 16 bits — see
        docs/bluetooth-protocol-trace-analysis.md §7.4). One reset() for
        the whole image, matching the official app's behavior, instead of
        one per 255-row slice."""
        if self._printer is None:
            raise RuntimeError("Not connected")
        row_bytes = self._printer.getRowBytes()
        packed = _pack_1bpp_for_printer(image)
        height = len(packed) // row_bytes

        self._printer.reset()
        header = (
            bytes.fromhex("1d763000")
            + bytes([row_bytes % 256, row_bytes // 256])
            + bytes([height % 256, height // 256])
        )
        self._printer.tellPrinter(header)
        for i in range(height):
            self._printer.tellPrinter(packed[i * row_bytes : (i + 1) * row_bytes])
            if delay:
                time.sleep(delay)

    # -- docs/printer-protocol-implementation-plan.md Phase 5 --

    def print_image_fast(self, image: PIL.Image.Image) -> None:
        """CONFIRMED BROKEN on real hardware (2026-07-15) — DO NOT USE.

        Tested live against the same A40 unit used throughout this project:
        sending a zlib.compress()-produced payload via this method made the
        printer crash/power off (recovered fine after a manual restart, no
        lasting damage, confirmed via a normal print through
        print_image_no_height_limit() afterward). Root cause not
        identified — leading suspicion is that the firmware's embedded
        inflate implementation expects a smaller deflate window than
        zlib.compress()'s default 32KB (window bits=15) and doesn't handle
        that gracefully, but this is a guess, not a diagnosis. The header
        fields themselves (opcode, row_bytes, height, payload_len) are
        confirmed correct against the decompiled official app's format —
        see docs/bluetooth-protocol-trace-analysis.md §3 — so the bug is
        specifically in the compressed payload not being decodable by this
        firmware's decompressor, not in how we frame it.

        Kept in the codebase (unused by any default code path) as a
        documented dead end so nobody re-attempts this without first
        addressing the above — e.g. by trying raw_deflate with a smaller
        explicit window via zlib.compressobj(wbits=...), or by capturing
        another real HCI trace of the official app and byte-diffing its
        actual compressed output against ours on identical input."""
        if self._printer is None:
            raise RuntimeError("Not connected")
        row_bytes = self._printer.getRowBytes()
        packed = _pack_1bpp_for_printer(image)
        height = len(packed) // row_bytes
        compressed = zlib.compress(packed, 6)[2:]  # drop the 2-byte zlib header, as the app does
        header = (
            b"\x1f\x00"
            + row_bytes.to_bytes(2, "big")
            + height.to_bytes(2, "big")
            + len(compressed).to_bytes(4, "big")
        )
        self._printer.tellPrinter(header + compressed)
