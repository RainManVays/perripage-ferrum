from collections.abc import Callable

import customtkinter as ctk

from periprint.infra.bt.bluetoothctl_backend import DiscoveredDevice
from periprint.models.enums import PrinterModel
from periprint.services.printer_manager import PrinterManager
from periprint.ui.scan_dialog import ScanDialog


class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        printer_manager: PrinterManager,
        on_profiles_changed: Callable[[], None] | None = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self.title("Настройки → Принтеры")
        self.geometry("420x480")
        self._printer_manager = printer_manager
        self._on_profiles_changed = on_profiles_changed

        ctk.CTkLabel(self, text="Принтеры", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(12, 0)
        )

        self.profiles_list = ctk.CTkTextbox(self, height=140)
        self.profiles_list.pack(fill="x", padx=12, pady=8)
        self._refresh_profiles_list()

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=12, pady=8)

        self.scan_button = ctk.CTkButton(form, text="Найти принтер", command=self._handle_scan)
        self.scan_button.pack(fill="x", pady=(0, 8))

        self.name_entry = ctk.CTkEntry(form, placeholder_text="Имя (напр. Мой A40)")
        self.name_entry.pack(fill="x", pady=(0, 4))

        self.mac_entry = ctk.CTkEntry(form, placeholder_text="MAC (напр. 28:D4:1E:01:34:C4)")
        self.mac_entry.pack(fill="x", pady=(0, 4))

        self.model_var = ctk.StringVar(value=PrinterModel.A40.value)
        self.model_menu = ctk.CTkOptionMenu(
            form, values=[model.value for model in PrinterModel], variable=self.model_var
        )
        self.model_menu.pack(fill="x", pady=(0, 4))

        self.save_button = ctk.CTkButton(form, text="Сохранить профиль", command=self._handle_save)
        self.save_button.pack(fill="x", pady=(4, 0))

    def _refresh_profiles_list(self) -> None:
        self.profiles_list.configure(state="normal")
        self.profiles_list.delete("1.0", "end")
        profiles = self._printer_manager.list_profiles()
        if not profiles:
            self.profiles_list.insert("1.0", "(нет сохранённых профилей)")
        else:
            lines = [f"{p.name} — {p.mac} ({p.model.value})" for p in profiles]
            self.profiles_list.insert("1.0", "\n".join(lines))
        self.profiles_list.configure(state="disabled")

    def _handle_scan(self) -> None:
        ScanDialog(self, on_device_selected=self._handle_device_selected)

    def _handle_device_selected(self, device: DiscoveredDevice) -> None:
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, device.name)
        self.mac_entry.delete(0, "end")
        self.mac_entry.insert(0, device.mac)

    def _handle_save(self) -> None:
        name = self.name_entry.get().strip()
        mac = self.mac_entry.get().strip()
        if not name or not mac:
            return
        model = PrinterModel(self.model_var.get())
        self._printer_manager.add_profile(name=name, mac=mac, model=model)
        self.name_entry.delete(0, "end")
        self.mac_entry.delete(0, "end")
        self._refresh_profiles_list()
        if self._on_profiles_changed:
            self._on_profiles_changed()
