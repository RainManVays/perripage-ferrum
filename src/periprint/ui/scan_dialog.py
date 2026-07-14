import queue
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from periprint.infra.bt.bluetoothctl_backend import DiscoveredDevice
from periprint.infra.bt.scanner import BluetoothScanner
from periprint.services.events import EventType


class ScanDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        on_device_selected: Callable[[DiscoveredDevice], None],
        scanner: BluetoothScanner | None = None,
        **kwargs: Any,
    ):
        super().__init__(master, **kwargs)
        self.title("Найти принтер")
        self.geometry("380x360")

        self._on_device_selected = on_device_selected
        self._scanner = scanner or BluetoothScanner()
        self._event_queue: queue.Queue[tuple[EventType, Any]] = queue.Queue()
        self._devices: list[DiscoveredDevice] = []

        self.status_label = ctk.CTkLabel(self, text="Сканирование...")
        self.status_label.pack(anchor="w", padx=12, pady=(12, 4))

        self.device_list = ctk.CTkScrollableFrame(self, label_text="Найденные устройства")
        self.device_list.pack(fill="both", expand=True, padx=12, pady=4)

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(fill="x", padx=12, pady=(4, 12))

        self.rescan_button = ctk.CTkButton(
            button_row, text="Сканировать ещё раз", command=self.start_scan
        )
        self.rescan_button.pack(side="left")

        self.manual_button = ctk.CTkButton(
            button_row, text="Ввести MAC вручную", command=self.destroy
        )
        self.manual_button.pack(side="right")

        self.start_scan()
        self.after(100, self._poll_events)

    def start_scan(self) -> None:
        self.status_label.configure(text="Сканирование (10-15с)...")
        self.rescan_button.configure(state="disabled")
        for child in self.device_list.winfo_children():
            child.destroy()
        self._scanner.scan_async(self._event_queue)

    def _poll_events(self) -> None:
        while not self._event_queue.empty():
            event_type, payload = self._event_queue.get_nowait()
            if event_type == EventType.SCAN_RESULT:
                self._show_devices(payload)
            elif event_type == EventType.SCAN_ERROR:
                self._show_error(payload)
        if self.winfo_exists():
            self.after(100, self._poll_events)

    def _show_devices(self, devices: list[DiscoveredDevice]) -> None:
        self._devices = devices
        self.rescan_button.configure(state="normal")
        if not devices:
            self.status_label.configure(text="Ничего не найдено")
            return
        self.status_label.configure(text=f"Найдено устройств: {len(devices)}")
        for device in devices:
            button = ctk.CTkButton(
                self.device_list,
                text=f"{device.name}\n{device.mac}",
                anchor="w",
                command=lambda d=device: self._handle_select(d),
            )
            button.pack(fill="x", pady=4)

    def _show_error(self, message: str) -> None:
        self.rescan_button.configure(state="normal")
        self.status_label.configure(text=f"Ошибка: {message}")

    def _handle_select(self, device: DiscoveredDevice) -> None:
        self._on_device_selected(device)
        self.destroy()
