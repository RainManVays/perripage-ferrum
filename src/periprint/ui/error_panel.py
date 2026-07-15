from collections.abc import Callable

import customtkinter as ctk


class ErrorPanel(ctk.CTkFrame):
    """In-window print-error view — same "no Toplevel" principle as
    SettingsPanel/ScanPanel (docs/stage5-ux-plan.md's post-launch UX
    fixes: "если эта форма ошибки — то то же самое", i.e. an error is not
    exempt from the "nothing spawns a new window" rule either). MainWindow
    swaps this in over whatever was showing (normally the expanded state)
    when a job pauses on error, and swaps back out once the user picks
    Reconnect or Cancel."""

    def __init__(self, master: ctk.CTkBaseClass, **kwargs):
        super().__init__(master, **kwargs)
        self._on_reconnect: Callable[[], None] | None = None
        self._on_cancel: Callable[[], None] | None = None

        ctk.CTkLabel(self, text="Ошибка печати", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=16, pady=(16, 8)
        )
        self.message_label = ctk.CTkLabel(self, text="", wraplength=500, justify="left")
        self.message_label.pack(padx=16, pady=8, fill="both", expand=True)
        self.bind(
            "<Configure>", lambda event: self.message_label.configure(wraplength=event.width - 32)
        )

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=(0, 16))
        ctk.CTkButton(button_row, text="Переподключиться", command=self._handle_reconnect).pack(
            side="left", padx=6
        )
        ctk.CTkButton(button_row, text="Отменить", command=self._handle_cancel).pack(
            side="left", padx=6
        )

    def show_error(
        self, message: str, on_reconnect: Callable[[], None], on_cancel: Callable[[], None]
    ) -> None:
        self.message_label.configure(text=message)
        self._on_reconnect = on_reconnect
        self._on_cancel = on_cancel

    def _handle_reconnect(self) -> None:
        if self._on_reconnect:
            self._on_reconnect()

    def _handle_cancel(self) -> None:
        if self._on_cancel:
            self._on_cancel()
