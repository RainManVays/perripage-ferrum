from collections.abc import Callable

import customtkinter as ctk
import PIL.Image

from periprint.models.enums import PaperType

_MAX_PREVIEW_WIDTH_PX = 260

# Human-readable labels for the dropdown — PaperType's own names are
# code-style identifiers, not something to show a user directly.
_PAPER_TYPE_LABELS = {
    PaperType.CONTINUOUS_ROLL: "Рулонная (обычная)",
    PaperType.FOLDED_BLACK_MARK: "Складная с чёрной меткой",
    PaperType.ADHESIVE_GAP: "Самоклеящаяся с зазором",
    PaperType.PERFORATED: "Перфорированная",
}
_PAPER_TYPE_BY_LABEL = {label: paper_type for paper_type, label in _PAPER_TYPE_LABELS.items()}


class PreviewPanel(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTkBaseClass,
        on_settings_changed: Callable[[], None] | None = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._on_settings_changed = on_settings_changed
        self._preview_image_ref: ctk.CTkImage | None = None

        title = ctk.CTkLabel(self, text="ПРЕВЬЮ", font=ctk.CTkFont(weight="bold"))
        title.pack(anchor="w", padx=8, pady=(8, 0))

        self.preview_area = ctk.CTkLabel(
            self,
            text="(нет документа)",
            fg_color=("gray90", "gray15"),
            height=220,
        )
        self.preview_area.pack(fill="both", expand=True, padx=8, pady=8)

        settings_title = ctk.CTkLabel(
            self, text="Настройки печати:", font=ctk.CTkFont(weight="bold")
        )
        settings_title.pack(anchor="w", padx=8, pady=(8, 0))

        self.concentration_slider = ctk.CTkSlider(self, from_=0, to=2, number_of_steps=2)
        self.concentration_slider.set(1)
        self.concentration_slider.pack(fill="x", padx=8, pady=(4, 0))

        self.break_slider = ctk.CTkSlider(self, from_=0, to=255)
        self.break_slider.set(60)
        self.break_slider.pack(fill="x", padx=8, pady=(4, 0))

        self.fit_mode_var = ctk.StringVar(value="fit_width")
        fit_row = ctk.CTkFrame(self, fg_color="transparent")
        fit_row.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkRadioButton(
            fit_row,
            text="по ширине",
            variable=self.fit_mode_var,
            value="fit_width",
            command=self._handle_settings_changed,
        ).pack(side="left")
        ctk.CTkRadioButton(
            fit_row,
            text="как есть",
            variable=self.fit_mode_var,
            value="actual_size",
            command=self._handle_settings_changed,
        ).pack(side="left", padx=(8, 0))

        self.dithering_var = ctk.BooleanVar(value=True)
        self.dithering_checkbox = ctk.CTkCheckBox(
            self,
            text="Дизеринг",
            variable=self.dithering_var,
            command=self._handle_settings_changed,
        )
        self.dithering_checkbox.pack(anchor="w", padx=(8, 0), pady=(8, 0))

        paper_type_row = ctk.CTkFrame(self, fg_color="transparent")
        paper_type_row.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkLabel(paper_type_row, text="Тип бумаги:").pack(side="left")
        self.paper_type_var = ctk.StringVar(value=_PAPER_TYPE_LABELS[PaperType.CONTINUOUS_ROLL])
        ctk.CTkOptionMenu(
            paper_type_row,
            variable=self.paper_type_var,
            values=list(_PAPER_TYPE_LABELS.values()),
            command=lambda _choice: self._handle_settings_changed(),
        ).pack(side="left", padx=(8, 0))

        self.page_mode_var = ctk.StringVar(value="full_page")
        page_mode_row = ctk.CTkFrame(self, fg_color="transparent")
        page_mode_row.pack(fill="x", padx=8, pady=8)
        ctk.CTkRadioButton(
            page_mode_row,
            text="целиком по формату",
            variable=self.page_mode_var,
            value="full_page",
            command=self._handle_settings_changed,
        ).pack(side="left")
        ctk.CTkRadioButton(
            page_mode_row,
            text="по длине контента",
            variable=self.page_mode_var,
            value="content_length",
            command=self._handle_settings_changed,
        ).pack(side="left", padx=(8, 0))

        page_range_row = ctk.CTkFrame(self, fg_color="transparent")
        page_range_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(page_range_row, text="Страницы:").pack(side="left")
        self.page_range_entry = ctk.CTkEntry(page_range_row, placeholder_text="все, напр. 2-4,7")
        self.page_range_entry.pack(side="left", padx=(8, 0), fill="x", expand=True)
        # Entries don't have a built-in "value changed" command like
        # radio/checkbox widgets — re-render on FocusOut/Enter, not every
        # keystroke: an in-progress range like "2-" is invalid syntax
        # (utils/page_range.py) and would just show a transient render
        # error while the user is still typing.
        self.page_range_entry.bind("<FocusOut>", lambda _e: self._handle_settings_changed())
        self.page_range_entry.bind("<Return>", lambda _e: self._handle_settings_changed())

        copies_row = ctk.CTkFrame(self, fg_color="transparent")
        copies_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(copies_row, text="Копий:").pack(side="left")
        self.copies_entry = ctk.CTkEntry(copies_row, width=60)
        self.copies_entry.insert(0, "1")
        self.copies_entry.pack(side="left", padx=(8, 0))
        self.copies_entry.bind("<FocusOut>", lambda _e: self._handle_settings_changed())
        self.copies_entry.bind("<Return>", lambda _e: self._handle_settings_changed())

    def get_paper_type(self) -> PaperType:
        return _PAPER_TYPE_BY_LABEL[self.paper_type_var.get()]

    def get_page_range(self) -> str:
        return self.page_range_entry.get().strip()

    def get_copies(self) -> int:
        try:
            value = int(self.copies_entry.get().strip())
        except ValueError:
            return 1
        return max(1, value)

    def _handle_settings_changed(self) -> None:
        if self._on_settings_changed:
            self._on_settings_changed()

    def show_preview(self, image: PIL.Image.Image) -> None:
        if image.width > _MAX_PREVIEW_WIDTH_PX:
            ratio = _MAX_PREVIEW_WIDTH_PX / image.width
            display_size = (_MAX_PREVIEW_WIDTH_PX, max(1, round(image.height * ratio)))
        else:
            display_size = (image.width, image.height)

        # CTkImage resizes via plain PIL .resize() with no resample filter.
        # PIL can't properly interpolate mode "1" (1-bit) images, so
        # downscaling the already-dithered raster directly produces moiré
        # noise. Converting to "L" first lets the resize average the
        # dithered dots into smooth gray — closer to how the real printout
        # reads from a normal viewing distance anyway.
        display_image = image.convert("L") if image.mode == "1" else image

        self._preview_image_ref = ctk.CTkImage(
            light_image=display_image, dark_image=display_image, size=display_size
        )
        self.preview_area.configure(image=self._preview_image_ref, text="")

    def show_message(self, text: str) -> None:
        self._preview_image_ref = None
        self.preview_area.configure(image=None, text=text)
