from collections.abc import Callable

import customtkinter as ctk

from periprint.infra.bt.bluetoothctl_backend import DiscoveredDevice
from periprint.models.enums import PrinterModel
from periprint.services.printer_manager import PrinterManager

_THEME_LABELS = {"dark": "Тёмная", "light": "Светлая"}
_THEME_BY_LABEL = {label: theme for theme, label in _THEME_LABELS.items()}


class SettingsPanel(ctk.CTkFrame):
    """In-window settings view (periprint-spec.md §7.2 items 3-4) — not a
    separate OS window. A desktop app spawning a Toplevel for something
    as ordinary as settings was flagged directly as a UX regression
    ("мы что в Windows XP?", docs/stage5-ux-plan.md's post-launch UX
    fixes); MainWindow swaps this in over the normal queue/preview body
    the same way it already swaps empty <-> expanded state, with an
    explicit "Назад" button rather than a window close button.

    Titled "Принтеры" but also carries the one Stage 5 M5.4 app-level
    setting implemented so far (theme) — a whole separate "Настройки →
    Приложение" view for a single toggle isn't worth it yet; split it out
    if/when more app-level settings (autostart, default chunk height,
    etc.) land."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        printer_manager: PrinterManager,
        on_back: Callable[[], None],
        on_profiles_changed: Callable[[], None] | None = None,
        on_scan_requested: Callable[[], None] | None = None,
        current_theme: str = "dark",
        on_theme_changed: Callable[[str], None] | None = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._printer_manager = printer_manager
        self._on_profiles_changed = on_profiles_changed
        self._on_scan_requested = on_scan_requested
        self._on_theme_changed = on_theme_changed

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 0))
        ctk.CTkButton(header, text="← Назад", width=90, command=on_back).pack(side="left")

        ctk.CTkLabel(self, text="Принтеры", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(16, 0)
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

        ctk.CTkLabel(self, text="Приложение", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(16, 0)
        )
        theme_row = ctk.CTkFrame(self, fg_color="transparent")
        theme_row.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(theme_row, text="Тема:").pack(side="left")
        self.theme_var = ctk.StringVar(value=_THEME_LABELS.get(current_theme, "Тёмная"))
        ctk.CTkOptionMenu(
            theme_row,
            variable=self.theme_var,
            values=list(_THEME_LABELS.values()),
            command=self._handle_theme_changed,
        ).pack(side="left", padx=(8, 0))

    def refresh(self) -> None:
        """Called each time MainWindow shows this panel — the profile
        list may have changed (a profile added from a different flow)
        since it was last visible."""
        self._refresh_profiles_list()

    def set_scanned_device(self, device: DiscoveredDevice) -> None:
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, device.name)
        self.mac_entry.delete(0, "end")
        self.mac_entry.insert(0, device.mac)

    def _handle_theme_changed(self, label: str) -> None:
        theme = _THEME_BY_LABEL[label]
        ctk.set_appearance_mode(theme)
        if self._on_theme_changed:
            self._on_theme_changed(theme)

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
        if self._on_scan_requested:
            self._on_scan_requested()

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
