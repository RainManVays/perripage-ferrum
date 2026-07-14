from collections.abc import Callable

import customtkinter as ctk


class PrinterPanel(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTk,
        on_open_settings: Callable[[], None] | None = None,
        on_connect_toggle: Callable[[], None] | None = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._on_open_settings = on_open_settings
        self._on_connect_toggle = on_connect_toggle

        self.title_label = ctk.CTkLabel(
            self, text="PeriPrint", font=ctk.CTkFont(size=16, weight="bold")
        )
        self.title_label.pack(side="left", padx=12, pady=8)

        self.status_label = ctk.CTkLabel(self, text="Принтер: не выбран")
        self.status_label.pack(side="left", padx=12, pady=8)

        self.connect_button = ctk.CTkButton(
            self, text="Подключить", command=self._handle_connect_toggle, state="disabled"
        )
        self.connect_button.pack(side="left", padx=12, pady=8)

        self.settings_button = ctk.CTkButton(
            self, text="⚙", width=32, command=self._handle_settings
        )
        self.settings_button.pack(side="right", padx=12, pady=8)

    def _handle_settings(self) -> None:
        if self._on_open_settings:
            self._on_open_settings()

    def _handle_connect_toggle(self) -> None:
        if self._on_connect_toggle:
            self._on_connect_toggle()

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def set_connect_button(self, *, text: str, enabled: bool) -> None:
        self.connect_button.configure(text=text, state="normal" if enabled else "disabled")
