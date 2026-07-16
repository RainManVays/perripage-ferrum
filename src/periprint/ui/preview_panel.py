from collections.abc import Callable

import customtkinter as ctk
import PIL.Image

from periprint.models.enums import PageFormat, PaperType

# Human-readable labels for the dropdown — PaperType's own names are
# code-style identifiers, not something to show a user directly.
_PAPER_TYPE_LABELS = {
    PaperType.CONTINUOUS_ROLL: "Рулонная (обычная)",
    PaperType.FOLDED_BLACK_MARK: "Складная с чёрной меткой",
    PaperType.ADHESIVE_GAP: "Самоклеящаяся с зазором",
    PaperType.PERFORATED: "Перфорированная",
}
_PAPER_TYPE_BY_LABEL = {label: paper_type for paper_type, label in _PAPER_TYPE_LABELS.items()}

# docs/stage5-ux-plan.md M5.5 — imposition (see PageFormat's own docstring
# for why the enum values aren't literally named A5/A6). UI copy uses the
# user's own vocabulary even though the internal names are generic. Not
# "2/4 части" — a source shorter than one A5/A6 page becomes a single
# piece, only genuinely tall content splits across more than one.
_PAGE_FORMAT_LABELS = {
    PageFormat.NATIVE: "Обычный (во всю ширину)",
    PageFormat.HALF: "А5 (148×210мм)",
    PageFormat.QUARTER: "А6 (105×148мм)",
    PageFormat.CUSTOM: "Свой размер",
}
_PAGE_FORMAT_BY_LABEL = {label: fmt for fmt, label in _PAGE_FORMAT_LABELS.items()}

_ROTATION_LABELS = {0: "0°", 90: "90°", 180: "180°", 270: "270°"}
_ROTATION_BY_LABEL = {label: degrees for degrees, label in _ROTATION_LABELS.items()}


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
        self._current_pil_image: PIL.Image.Image | None = None

        title = ctk.CTkLabel(self, text="ПРЕВЬЮ", font=ctk.CTkFont(weight="bold"))
        title.pack(anchor="w", padx=8, pady=(8, 0))

        self.preview_area = ctk.CTkLabel(
            self,
            text="(нет документа)",
            fg_color=("gray90", "gray15"),
            height=220,
        )
        self.preview_area.pack(fill="both", expand=True, padx=8, pady=8)
        # The old logic scaled to a fixed 260px width regardless of the
        # panel's actual size — on a bigger window the preview stayed tiny
        # ("превью должно максимально растягиваться... внутри блока
        # превью"). Re-fitting on every <Configure> (widget resize) makes it
        # track the real available area instead of a hardcoded constant.
        self.preview_area.bind("<Configure>", lambda _event: self._refresh_preview_fit())

        settings_title = ctk.CTkLabel(
            self, text="Настройки печати:", font=ctk.CTkFont(weight="bold")
        )
        settings_title.pack(anchor="w", padx=8, pady=(8, 0))

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
            text="Дизеринг (полутона точками — для фото; выключить для чёткого текста)",
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

        page_format_row = ctk.CTkFrame(self, fg_color="transparent")
        page_format_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(page_format_row, text="Формат:").pack(side="left")
        self.page_format_var = ctk.StringVar(value=_PAGE_FORMAT_LABELS[PageFormat.NATIVE])
        ctk.CTkOptionMenu(
            page_format_row,
            variable=self.page_format_var,
            values=list(_PAGE_FORMAT_LABELS.values()),
            command=self._handle_page_format_changed,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(page_format_row, text="Поворот:").pack(side="left", padx=(16, 0))
        self.rotation_var = ctk.StringVar(value=_ROTATION_LABELS[0])
        ctk.CTkOptionMenu(
            page_format_row,
            variable=self.rotation_var,
            values=list(_ROTATION_LABELS.values()),
            width=70,
            command=lambda _choice: self._handle_settings_changed(),
        ).pack(side="left", padx=(8, 0))

        # Only shown for page_format=CUSTOM (see _handle_page_format_changed)
        # — packed here so it takes its place in the vertical stack right
        # away, but immediately pack_forget()'d since NATIVE is the default.
        self.custom_size_row = ctk.CTkFrame(self, fg_color="transparent")
        ctk.CTkLabel(self.custom_size_row, text="Размер куска, мм:").pack(side="left")
        self.custom_width_entry = ctk.CTkEntry(self.custom_size_row, width=60)
        self.custom_width_entry.insert(0, "100")
        self.custom_width_entry.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(self.custom_size_row, text="×").pack(side="left", padx=(4, 4))
        self.custom_height_entry = ctk.CTkEntry(self.custom_size_row, width=60)
        self.custom_height_entry.insert(0, "150")
        self.custom_height_entry.pack(side="left")
        for entry in (self.custom_width_entry, self.custom_height_entry):
            entry.bind("<FocusOut>", lambda _e: self._handle_settings_changed())
            entry.bind("<Return>", lambda _e: self._handle_settings_changed())
        self.custom_size_row.pack(fill="x", padx=8, pady=(0, 8))
        self.custom_size_row.pack_forget()

        self.page_range_row = ctk.CTkFrame(self, fg_color="transparent")
        page_range_row = self.page_range_row
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

    def get_page_format(self) -> PageFormat:
        return _PAGE_FORMAT_BY_LABEL[self.page_format_var.get()]

    def get_rotation_degrees(self) -> int:
        return _ROTATION_BY_LABEL[self.rotation_var.get()]

    def get_custom_tile_width_mm(self) -> float:
        try:
            value = float(self.custom_width_entry.get().strip())
        except ValueError:
            return 100.0
        return value if value > 0 else 100.0

    def get_custom_tile_height_mm(self) -> float:
        try:
            value = float(self.custom_height_entry.get().strip())
        except ValueError:
            return 150.0
        return value if value > 0 else 150.0

    def _handle_page_format_changed(self, _choice: str) -> None:
        # The width/height-in-mm entries are only meaningful for CUSTOM —
        # shown/hidden rather than always visible-but-ignored, same spirit
        # as dropzone.py's hover feedback: a control that's inert but still
        # on screen invites confusion about whether it's doing anything.
        if self.get_page_format() == PageFormat.CUSTOM:
            self.custom_size_row.pack(
                fill="x", padx=8, pady=(0, 8), before=self.page_range_row
            )
        else:
            self.custom_size_row.pack_forget()
        self._handle_settings_changed()

    def _handle_settings_changed(self) -> None:
        if self._on_settings_changed:
            self._on_settings_changed()

    def show_preview(self, image: PIL.Image.Image) -> None:
        self._current_pil_image = image
        self._refresh_preview_fit()

    def _refresh_preview_fit(self) -> None:
        """Recomputes the displayed image size to fill as much of
        preview_area's *actual current* size as possible while preserving
        aspect ratio (upscaling small images, not just downscaling large
        ones) — called both from show_preview() and on every <Configure>
        so resizing the window/panel keeps the preview maximized."""
        image = self._current_pil_image
        if image is None:
            return

        area_width = self.preview_area.winfo_width()
        area_height = self.preview_area.winfo_height()
        # Before the widget is first laid out by the geometry manager,
        # winfo_width()/height() report a stale 1x1 placeholder — skip
        # fitting until a real <Configure> event reports actual space.
        if area_width <= 1 or area_height <= 1:
            return

        scale = min(area_width / image.width, area_height / image.height)
        display_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))

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
        # Workaround for a real customtkinter bug (traced through its
        # source, not guessed): CTkLabel._update_image() only calls
        # self._label.configure(image=...) when the new image is a
        # CTkImage or otherwise not None — passing image=None takes
        # neither branch, so the *raw* underlying tkinter.Label never
        # actually has its own `image` option cleared, even though
        # CTkLabel's own bookkeeping (self._image) correctly becomes
        # None. Once Python garbage-collects the real PhotoImage that
        # raw option was pointing to, the raw label is left holding a
        # dangling image *name* reference — and Tk raises
        # `_tkinter.TclError: image "pyimageN" doesn't exist` on the
        # *next* .configure() call of any kind on that label, not just
        # another image change. Reproduced directly: two show_message()
        # calls in a row (e.g. an invalid page range triggering a render
        # error twice in succession) crashed the whole app on the second
        # call. Clearing the raw label's image option to "" (Tk's own
        # convention for "no image", not Python None) side-steps
        # customtkinter's broken path entirely.
        self._current_pil_image = None
        self._preview_image_ref = None
        self.preview_area._label.configure(image="")
        self.preview_area.configure(text=text)
