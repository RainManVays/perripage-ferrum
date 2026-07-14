from __future__ import annotations

from typing import Any


class FakeRawPrinter:
    """Duck-types periprint.infra.raw_printer_protocol.RawPrinter without
    touching peripage/PyBluez, so PeripageClient's retry/reconnect logic can
    be tested without any real Bluetooth stack installed."""

    def __init__(self, fail_connects: int = 0) -> None:
        self.connected = False
        self.connect_calls = 0
        self.reset_calls = 0
        self.disconnect_calls = 0
        self.set_concentration_calls: list[tuple[int, bool]] = []
        self._fail_connects = fail_connects

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
        pass

    def printImage(self, img: Any, delay: float = 0.01) -> None:
        pass

    def getDeviceBattery(self) -> int:
        return 100
