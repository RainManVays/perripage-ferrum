import queue
import threading
from typing import Any

import customtkinter as ctk

from periprint.infra.config_store import ConfigStore
from periprint.infra.peripage_client import PeripageClient, PeripageConnectionError
from periprint.models.printer_profile import PrinterProfile
from periprint.services.events import EventType
from periprint.services.printer_manager import PrinterManager
from periprint.ui.preview_panel import PreviewPanel
from periprint.ui.printer_panel import PrinterPanel
from periprint.ui.queue_panel import QueuePanel
from periprint.ui.settings_dialog import SettingsDialog


class MainWindow(ctk.CTk):
    def __init__(
        self,
        printer_manager: PrinterManager | None = None,
        config_store: ConfigStore | None = None,
    ) -> None:
        super().__init__()
        self.title("PeriPrint")
        self.geometry("900x600")

        self._printer_manager = printer_manager or PrinterManager()
        self._config_store = config_store or ConfigStore()
        self._config = self._config_store.load()
        self._settings_dialog: SettingsDialog | None = None
        self._client: PeripageClient | None = None
        self._active_profile: PrinterProfile | None = None
        self._event_queue: queue.Queue[tuple[EventType, Any]] = queue.Queue()

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.printer_panel = PrinterPanel(
            self,
            on_open_settings=self._open_settings,
            on_connect_toggle=self._handle_connect_toggle,
        )
        self.printer_panel.grid(row=0, column=0, sticky="ew")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.queue_panel = QueuePanel(body)
        self.queue_panel.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        self.preview_panel = PreviewPanel(body)
        self.preview_panel.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)

        self.status_bar = ctk.CTkLabel(self, text="Статус: готово", anchor="w")
        self.status_bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

        self._refresh_active_profile()
        self.after(100, self._poll_events)

    def _open_settings(self) -> None:
        if self._settings_dialog is None or not self._settings_dialog.winfo_exists():
            self._settings_dialog = SettingsDialog(
                self,
                self._printer_manager,
                on_profiles_changed=self._refresh_active_profile,
            )
        else:
            self._settings_dialog.focus()

    def _refresh_active_profile(self) -> None:
        profiles = self._printer_manager.list_profiles()
        active = None
        if self._config.active_printer_id:
            active = self._printer_manager.get_profile(self._config.active_printer_id)
        if active is None and profiles:
            active = profiles[0]

        self._active_profile = active
        if active is None:
            self.printer_panel.set_status("Принтер: не выбран")
            self.printer_panel.set_connect_button(text="Подключить", enabled=False)
        elif self._client is not None and self._client.is_connected():
            self.printer_panel.set_status(f"Принтер: {active.name} ● Connected")
            self.printer_panel.set_connect_button(text="Отключить", enabled=True)
        else:
            self.printer_panel.set_status(f"Принтер: {active.name} ● Disconnected")
            self.printer_panel.set_connect_button(text="Подключить", enabled=True)

    def _handle_connect_toggle(self) -> None:
        if self._active_profile is None:
            return
        if self._client is not None and self._client.is_connected():
            self._disconnect_async()
        else:
            self._connect_async()

    def _connect_async(self) -> None:
        profile = self._active_profile
        assert profile is not None
        self.printer_panel.set_status(f"Принтер: {profile.name} ● Connecting...")
        self.printer_panel.set_connect_button(text="Подключение...", enabled=False)

        def worker() -> None:
            client = PeripageClient(
                mac=profile.mac,
                model=profile.model,
                concentration=profile.default_concentration,
            )
            try:
                client.reconnect()
                self._event_queue.put((EventType.CONNECTION_STATUS, ("connected", client)))
            except PeripageConnectionError as exc:
                self._event_queue.put((EventType.CONNECTION_ERROR, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect_async(self) -> None:
        client = self._client
        if client is None:
            return
        self.printer_panel.set_connect_button(text="Отключение...", enabled=False)

        def worker() -> None:
            client.disconnect()
            self._event_queue.put((EventType.CONNECTION_STATUS, ("disconnected", None)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        while not self._event_queue.empty():
            event_type, payload = self._event_queue.get_nowait()
            if event_type == EventType.CONNECTION_STATUS:
                status, client = payload
                if status == "connected":
                    self._client = client
                    if self._active_profile is not None:
                        self._config.active_printer_id = self._active_profile.id
                        self._config_store.save(self._config)
                elif status == "disconnected":
                    self._client = None
                self._refresh_active_profile()
            elif event_type == EventType.CONNECTION_ERROR:
                self._client = None
                if self._active_profile is not None:
                    self.printer_panel.set_status(f"Принтер: {self._active_profile.name} ● Error")
                self.printer_panel.set_connect_button(text="Подключить", enabled=True)
                self.status_bar.configure(text=f"Статус: ошибка подключения — {payload}")
        if self.winfo_exists():
            self.after(100, self._poll_events)
