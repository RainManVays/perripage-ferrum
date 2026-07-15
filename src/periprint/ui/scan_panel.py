import queue
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from periprint.infra.bt.bluetoothctl_backend import DiscoveredDevice
from periprint.infra.bt.scanner import BluetoothScanner
from periprint.services.events import EventType


class ScanPanel(ctk.CTkFrame):
    """In-window device scan view — same "no Toplevel" principle as
    SettingsPanel (docs/stage5-ux-plan.md's post-launch UX fixes).
    MainWindow swaps this in over SettingsPanel while a scan is running,
    then swaps back once a device is picked or the user backs out."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        on_device_selected: Callable[[DiscoveredDevice], None],
        on_back: Callable[[], None],
        scanner: BluetoothScanner | None = None,
        **kwargs: Any,
    ):
        super().__init__(master, **kwargs)
        self._on_device_selected = on_device_selected
        self._on_back = on_back
        self._scanner = scanner or BluetoothScanner()
        self._event_queue: queue.Queue[tuple[EventType, Any]] = queue.Queue()
        self._devices: list[DiscoveredDevice] = []
        # Own polling loop, not MainWindow's shared _poll_events() — needs
        # an explicit stop when the panel is hidden (back/select), or a
        # self.after() chain would keep firing in the background against
        # a widget nobody's looking at.
        self._polling = False

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 0))
        ctk.CTkButton(header, text="← Назад", width=90, command=self._handle_back).pack(
            side="left"
        )
        ctk.CTkLabel(header, text="Найти принтер", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(8, 0)
        )

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
            button_row, text="Ввести MAC вручную", command=self._handle_back
        )
        self.manual_button.pack(side="right")

    def start_scan(self) -> None:
        self.status_label.configure(text="Сканирование (10-15с)...")
        self.rescan_button.configure(state="disabled")
        for child in self.device_list.winfo_children():
            child.destroy()
        self._scanner.scan_async(self._event_queue)
        if not self._polling:
            self._polling = True
            self.after(100, self._poll_events)

    def stop_polling(self) -> None:
        self._polling = False

    def _poll_events(self) -> None:
        if not self._polling:
            return
        while not self._event_queue.empty():
            event_type, payload = self._event_queue.get_nowait()
            if event_type == EventType.SCAN_RESULT:
                self._show_devices(payload)
            elif event_type == EventType.SCAN_ERROR:
                self._show_error(payload)
        if self.winfo_exists() and self._polling:
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
        self.stop_polling()
        self._on_device_selected(device)

    def _handle_back(self) -> None:
        self.stop_polling()
        self._on_back()
