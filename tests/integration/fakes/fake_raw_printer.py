from __future__ import annotations

from typing import Any


class FakeRawPrinter:
    """Duck-types periprint.infra.raw_printer_protocol.RawPrinter without
    touching peripage/PyBluez, so PeripageClient's retry/reconnect logic can
    be tested without any real Bluetooth stack installed."""

    def __init__(self, fail_connects: int = 0, row_bytes: int = 216) -> None:
        self.connected = False
        self.connect_calls = 0
        self.reset_calls = 0
        self.disconnect_calls = 0
        self.set_concentration_calls: list[tuple[int, bool]] = []
        self.print_image_calls = 0
        self.print_break_calls = 0
        self.tell_printer_calls: list[bytes] = []
        self.fail_print_image_on_call: int | None = None
        self._fail_connects = fail_connects
        self._row_bytes = row_bytes

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self._fail_connects:
            raise ConnectionRefusedError("simulated connect failure")
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def reset(self) -> None:
        self.reset_calls += 1

    def setConcentration(self, concentration: int, wait: bool = False) -> None:
        self.set_concentration_calls.append((concentration, wait))

    def printBreak(self, size: int = 0x40) -> None:
        self.print_break_calls += 1

    def printImage(self, img: Any, delay: float = 0.01) -> None:
        self.print_image_calls += 1
        if self.fail_print_image_on_call == self.print_image_calls:
            raise OSError("simulated mid-print connection drop")

    def getDeviceBattery(self) -> int:
        return 100

    def getRowBytes(self) -> int:
        return self._row_bytes

    def tellPrinter(self, byteseq: bytes) -> None:
        self.tell_printer_calls.append(bytes(byteseq))
