import queue
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from typing import Any

import customtkinter as ctk
from tkinterdnd2 import TkinterDnD

from periprint.infra.bt.bluetoothctl_backend import DiscoveredDevice
from periprint.infra.config_store import ConfigStore
from periprint.infra.history_store import HistoryEntry, HistoryStore
from periprint.infra.peripage_client import PeripageClient, PeripageConnectionError
from periprint.models.document import DocumentItem, PrintSettings
from periprint.models.enums import JobStatus, PrinterModel
from periprint.models.job import PrintJob
from periprint.models.printer_profile import PrinterProfile
from periprint.models.printer_specs import NATIVE_WIDTH_PX, safe_content_width_px
from periprint.services.events import EventType
from periprint.services.job_manager import PrintJobManager
from periprint.services.pipeline import (
    DocumentPipeline,
    UnsupportedDocumentKindError,
    detect_document_kind,
)
from periprint.services.printer_manager import PrinterManager
from periprint.ui.empty_state_panel import EmptyStatePanel
from periprint.ui.error_panel import ErrorPanel
from periprint.ui.preview_panel import PreviewPanel
from periprint.ui.printer_panel import PrinterPanel
from periprint.ui.queue_panel import QueuePanel
from periprint.ui.scan_panel import ScanPanel
from periprint.ui.settings_panel import SettingsPanel

_DEFAULT_PREVIEW_MODEL = PrinterModel.A40
_DEFAULT_CHUNK_HEIGHT_PX = 220
_FINISHED_STATUSES = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
# Below this many observed seconds of progress, the rate estimate is too
# noisy to show (a single fast/slow chunk right after print starts would
# produce a wildly wrong ETA) — periprint-spec.md §7.1's own sketch shows
# an ETA next to chunk progress ("00:12 осталось (оценка)"), explicitly
# labeled as an estimate, not a promise.
_ETA_MIN_ELAPSED_SECONDS = 1.0


def _format_eta(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes:02d}:{secs:02d}"

# Reverse-engineered from the official app's decompiled code, not just
# inferred from a live trace — see
# docs/bluetooth-protocol-trace-analysis.md §7.3.
_PRINTER_STATUS_LABELS = {
    "out_of_paper": "бумага закончилась",
    "cover_open": "крышка открыта",
    "overheat": "перегрев",
    "low_battery": "низкий заряд батареи",
    "cover_closed": "крышка закрыта",
    "low_mileage": "недостаточный пробег картриджа/головки",
    "abort_print": "принтер запросил остановку печати",
    "resume_print": "принтер разрешил продолжить печать",
    "paper_type_mismatch": "несовпадение типа бумаги",
}


class MainWindow(ctk.CTk, TkinterDnD.DnDWrapper):
    """TkinterDnD.DnDWrapper mixin (not TkinterDnD.Tk, which would fight
    customtkinter's own Tk subclass) — this is the standard way to add
    tkinterdnd2 support to a customtkinter root: it only needs
    TkdndVersion set via _require(), the actual drop_target_register()/
    dnd_bind() methods come from the mixin and work on any descendant
    widget once the root has been initialized this way."""

    def __init__(
        self,
        printer_manager: PrinterManager | None = None,
        config_store: ConfigStore | None = None,
        history_store: HistoryStore | None = None,
    ) -> None:
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)
        self.title("PeriPrint")
        # Tall enough that the settings panel (grown with Stage 5's paper
        # type/page mode/page range/copies controls) doesn't clip at the
        # bottom — window is still resizable by the user either way.
        self.geometry("900x700")

        self._printer_manager = printer_manager or PrinterManager()
        self._config_store = config_store or ConfigStore()
        self._config = self._config_store.load()
        self._history_store = history_store or HistoryStore()
        self._client: PeripageClient | None = None
        self._active_profile: PrinterProfile | None = None
        self._battery_percent: int | None = None
        self._event_queue: queue.Queue[tuple[EventType, Any]] = queue.Queue()
        self._pipeline = DocumentPipeline()
        self._current_document: DocumentItem | None = None
        self._job_manager = PrintJobManager(
            self._pipeline, self._event_queue, client_provider=lambda: self._client
        )
        self._job_awaiting_reconnect_id: str | None = None
        # "empty" | "expanded" | "settings" | "scan" | "error" — which of
        # the mutually-exclusive full-window views is currently shown.
        # Nothing in this app spawns a separate OS window for settings/
        # scanning/errors (docs/stage5-ux-plan.md's post-launch UX fixes:
        # "мы что в Windows XP?") — every one of them is a view swapped
        # into the same grid cells via _show_*()/grid_forget(), same as
        # the empty <-> expanded switch already was.
        self._current_view = "empty"
        # job_id -> (perf-counter time, completed_chunks) observed the
        # first time each job was seen PRINTING this session — the basis
        # for the ETA estimate in the status bar. Not persisted/resumed
        # across a pause: a resumed job's rate starts fresh, since
        # whatever happened before the pause (e.g. a reconnect wait)
        # isn't representative of ongoing print speed.
        self._job_progress_start: dict[str, tuple[float, int]] = {}

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Two mutually-exclusive top bars: printer_panel (small, expanded
        # state) and empty_state_panel's own header (large icons, empty
        # state) — see _show_empty_state()/_show_expanded_state(). Both
        # exist from startup so _set_printer_status()/_set_connect_buttons()
        # can always keep both in sync regardless of which is visible;
        # switching states must never reveal stale connection info.
        self.printer_panel = PrinterPanel(
            self,
            on_open_settings=self._open_settings,
            on_connect_toggle=self._handle_connect_toggle,
        )

        self.empty_state_panel = EmptyStatePanel(
            self,
            on_open_settings=self._open_settings,
            on_connect_toggle=self._handle_connect_toggle,
            on_find_printer=self._open_settings,
            on_files_dropped=self._handle_files_dropped,
            on_select_file=self._handle_select_file,
        )

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        self.queue_panel = QueuePanel(
            self.body,
            on_select_file=self._handle_select_file,
            on_files_dropped=self._handle_files_dropped,
            on_print_all=self._handle_print_all,
            on_clear=self._handle_clear_queue,
            on_move_job=self._handle_move_job,
            on_stop_job=self._handle_stop_job,
            on_resume_job=self._handle_resume_job,
        )
        self.queue_panel.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        self.preview_panel = PreviewPanel(
            self.body, on_settings_changed=self._handle_preview_settings_changed
        )
        self.preview_panel.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)

        self.status_bar = ctk.CTkLabel(self, text="Статус: готово", anchor="w")

        self.settings_panel = SettingsPanel(
            self,
            self._printer_manager,
            on_back=self._close_settings,
            on_profiles_changed=self._refresh_active_profile,
            on_scan_requested=self._handle_scan_requested,
            current_theme=self._config.theme,
            on_theme_changed=self._handle_theme_changed,
        )
        self.scan_panel = ScanPanel(
            self,
            on_device_selected=self._handle_scan_device_selected,
            on_back=self._handle_scan_back,
        )
        self.error_panel = ErrorPanel(self)

        self._refresh_active_profile()
        self._show_empty_state()
        self.after(100, self._poll_events)

    def _hide_all_views(self) -> None:
        self.empty_state_panel.grid_forget()
        self.printer_panel.grid_forget()
        self.body.grid_forget()
        self.status_bar.grid_forget()
        self.settings_panel.grid_forget()
        self.scan_panel.grid_forget()
        self.error_panel.grid_forget()

    def _show_empty_state(self) -> None:
        self._hide_all_views()
        self.empty_state_panel.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self._current_view = "empty"

    def _show_expanded_state(self) -> None:
        self._hide_all_views()
        self.printer_panel.grid(row=0, column=0, sticky="ew")
        self.body.grid(row=1, column=0, sticky="nsew")
        self.status_bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        self._current_view = "expanded"

    def _show_previous_main_state(self) -> None:
        """Returns to whichever of empty/expanded actually matches the
        current queue state right now — not a literal "undo", since e.g.
        cancelling a job from the error view can itself empty the queue."""
        if self._job_manager.list_jobs():
            self._show_expanded_state()
        else:
            self._show_empty_state()

    def _open_settings(self) -> None:
        self.settings_panel.refresh()
        self._hide_all_views()
        self.settings_panel.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self._current_view = "settings"

    def _close_settings(self) -> None:
        self._show_previous_main_state()

    def _handle_scan_requested(self) -> None:
        self._hide_all_views()
        self.scan_panel.start_scan()
        self.scan_panel.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self._current_view = "scan"

    def _handle_scan_device_selected(self, device: DiscoveredDevice) -> None:
        self.settings_panel.set_scanned_device(device)
        self._return_to_settings()

    def _handle_scan_back(self) -> None:
        self._return_to_settings()

    def _return_to_settings(self) -> None:
        self._hide_all_views()
        self.settings_panel.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self._current_view = "settings"

    def _handle_theme_changed(self, theme: str) -> None:
        self._config.theme = theme
        self._config_store.save(self._config)

    def _refresh_active_profile(self) -> None:
        profiles = self._printer_manager.list_profiles()
        active = None
        if self._config.active_printer_id:
            active = self._printer_manager.get_profile(self._config.active_printer_id)
        if active is None and profiles:
            active = profiles[0]

        self._active_profile = active
        if active is None:
            self._set_printer_status("Принтер: не выбран")
            self._set_connect_buttons(text="Подключить", enabled=False)
        elif self._client is not None and self._client.is_connected():
            battery_suffix = (
                f" · 🔋{self._battery_percent}%" if self._battery_percent is not None else ""
            )
            self._set_printer_status(f"Принтер: {active.name} ● Connected{battery_suffix}")
            self._set_connect_buttons(text="Отключить", enabled=True)
        else:
            self._set_printer_status(f"Принтер: {active.name} ● Disconnected")
            self._set_connect_buttons(text="Подключить", enabled=True)

    def _set_printer_status(self, text: str) -> None:
        """Both panels get the status update regardless of which is
        currently visible — switching states (empty <-> expanded) must
        never show stale connection info from before the switch."""
        self.printer_panel.set_status(text)
        self.empty_state_panel.set_status(text)

    def _set_connect_buttons(self, *, text: str, enabled: bool) -> None:
        self.printer_panel.set_connect_button(text=text, enabled=enabled)
        self.empty_state_panel.set_connect_button(text=text, enabled=enabled)

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
        self._set_printer_status(f"Принтер: {profile.name} ● Connecting...")
        self._set_connect_buttons(text="Подключение...", enabled=False)

        def worker() -> None:
            client = PeripageClient(
                mac=profile.mac,
                model=profile.model,
                concentration=profile.default_concentration,
            )
            try:
                client.reconnect()
                # docs/stage5-ux-plan.md §0.2: fetched once per connect, not
                # polled on a timer — the official app itself only re-reads
                # this around print actions, not continuously (see
                # bluetooth-protocol-trace-analysis.md §2 step 2). A manual
                # "refresh" is just reconnecting (disconnect, then connect
                # again), not a dedicated always-on background thread.
                try:
                    battery = client.get_battery_percent()
                except Exception:
                    battery = None
                self._event_queue.put((EventType.CONNECTION_STATUS, ("connected", client, battery)))
            except PeripageConnectionError as exc:
                self._event_queue.put((EventType.CONNECTION_ERROR, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect_async(self) -> None:
        client = self._client
        if client is None:
            return
        self._set_connect_buttons(text="Отключение...", enabled=False)

        def worker() -> None:
            client.disconnect()
            self._event_queue.put((EventType.CONNECTION_STATUS, ("disconnected", None, None)))

        threading.Thread(target=worker, daemon=True).start()

    def _resolve_render_target(self) -> tuple[int, int, int]:
        """(content_width_px, canvas_width_px, chunk_height_px) — from the
        active profile if one is selected, else sane defaults so preview
        still works with no printer configured yet. content_width_px is
        the safe area content gets wrapped/fit into; canvas_width_px is the
        full native width the final image is padded out to (see
        printer_specs.SAFE_CONTENT_WIDTH_PX / DocumentPipeline)."""
        model = (
            self._active_profile.model
            if self._active_profile is not None
            else _DEFAULT_PREVIEW_MODEL
        )
        chunk_height_px = (
            self._active_profile.chunk_height_px
            if self._active_profile is not None
            else _DEFAULT_CHUNK_HEIGHT_PX
        )
        return safe_content_width_px(model), NATIVE_WIDTH_PX[model], chunk_height_px

    def _handle_select_file(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Выбрать файлы для печати",
            filetypes=[
                ("Поддерживаемые файлы", "*.png *.jpg *.jpeg *.bmp *.txt *.pdf"),
                ("Все файлы", "*.*"),
            ],
        )
        if not paths:
            return
        self._add_documents(list(paths))

    def _handle_files_dropped(self, paths: list[str]) -> None:
        self._add_documents(paths)

    def _add_documents(self, paths: list[str]) -> None:
        """Shared by both the file-picker dialog and real drag&drop
        (empty_state_panel/queue_panel, Stage 5 M5.4) — either can hand
        over more than one path at once, so this always takes a list, not
        a single path."""
        width_px, canvas_width_px, chunk_height_px = self._resolve_render_target()
        added_any = False
        for path in paths:
            kind = detect_document_kind(path)
            if kind is None:
                self.status_bar.configure(text=f"Статус: формат файла не поддерживается — {path}")
                continue

            document = DocumentItem(
                id=str(uuid.uuid4()),
                source_path=path,
                kind=kind,
                settings=PrintSettings(
                    fit_mode=self.preview_panel.fit_mode_var.get(),
                    dithering=self.preview_panel.dithering_var.get(),
                    paper_type=self.preview_panel.get_paper_type(),
                    page_mode=self.preview_panel.page_mode_var.get(),
                    page_range=self.preview_panel.get_page_range(),
                    copies=self.preview_panel.get_copies(),
                ),
            )
            self._current_document = document
            job = PrintJob(
                id=str(uuid.uuid4()),
                document=document,
                printer_profile_id=self._active_profile.id if self._active_profile else "",
            )
            self._job_manager.enqueue(job, width_px, chunk_height_px, canvas_width_px)
            added_any = True

        if not added_any:
            return
        # periprint-spec.md §7.4: transition to the expanded state happens
        # on the first accepted file, not before.
        if self._current_view != "expanded":
            self._show_expanded_state()
        self._render_and_show_preview()
        self.queue_panel.set_jobs(self._job_manager.list_jobs())

    def _handle_print_all(self) -> None:
        # Entry widgets (page_range/copies, see PreviewPanel) only commit
        # into PrintSettings on FocusOut/Return — clicking straight from
        # one of those fields to this button doesn't reliably fire that
        # first. Force a final sync here so whatever's currently typed is
        # what actually prints, not a stale/default value (was a real
        # reported bug: setting a page range then printing used all pages).
        self._handle_preview_settings_changed()
        self._job_manager.start()
        self.status_bar.configure(text="Статус: печать очереди запущена")

    def _handle_clear_queue(self) -> None:
        self._job_manager.clear_queue()
        jobs = self._job_manager.list_jobs()
        self.queue_panel.set_jobs(jobs)
        # periprint-spec.md §7.4: back to the empty state once the queue
        # is completely empty (everything printed *and* cleared) — not
        # merely "nothing left QUEUED", which clear_queue() alone doesn't
        # guarantee (a still-PRINTING/PAUSED_ERROR job is deliberately
        # left in place, see clear_queue()'s own docstring).
        if not jobs:
            self._show_empty_state()

    def _handle_move_job(self, job_id: str, delta: int) -> None:
        self._job_manager.move_job(job_id, delta)
        # move_job() already emits a PRINT_PROGRESS event the poll loop
        # will pick up too, but that's up to 100ms later (self.after(100,
        # ...)) — refreshing here as well makes the reorder buttons feel
        # immediate instead of visibly laggy.
        self.queue_panel.set_jobs(self._job_manager.list_jobs())

    def _handle_stop_job(self, job_id: str) -> None:
        self._job_manager.request_stop(job_id)

    def _handle_resume_job(self, job_id: str) -> None:
        # No point going through a whole reconnect cycle if we're already
        # connected — that's only needed when the pause was caused by (or
        # is now compounded by) a lost connection.
        if self._client is not None and self._client.is_connected():
            self._job_manager.retry_job(job_id)
        else:
            self._handle_error_reconnect(job_id)

    def _handle_preview_settings_changed(self) -> None:
        if self._current_document is None:
            return
        self._current_document.settings.fit_mode = self.preview_panel.fit_mode_var.get()
        self._current_document.settings.dithering = self.preview_panel.dithering_var.get()
        self._current_document.settings.paper_type = self.preview_panel.get_paper_type()
        self._current_document.settings.page_mode = self.preview_panel.page_mode_var.get()
        self._current_document.settings.page_range = self.preview_panel.get_page_range()
        self._current_document.settings.copies = self.preview_panel.get_copies()
        self._render_and_show_preview()

    def _render_and_show_preview(self) -> None:
        document = self._current_document
        if document is None:
            return
        width_px, canvas_width_px, chunk_height_px = self._resolve_render_target()
        try:
            rendered = self._pipeline.render_document(
                document, width_px, chunk_height_px, canvas_width_px
            )
        except UnsupportedDocumentKindError:
            self.preview_panel.show_message(f"Формат {document.kind} пока не поддерживается")
            return
        except Exception as exc:
            self.preview_panel.show_message(f"Ошибка рендера: {exc}")
            return

        self.preview_panel.show_preview(rendered.pages[0].image)

    def _poll_events(self) -> None:
        while not self._event_queue.empty():
            event_type, payload = self._event_queue.get_nowait()
            if event_type == EventType.CONNECTION_STATUS:
                status, client, battery = payload
                if status == "connected":
                    self._client = client
                    self._battery_percent = battery
                    if self._active_profile is not None:
                        self._config.active_printer_id = self._active_profile.id
                        self._config_store.save(self._config)
                    if self._job_awaiting_reconnect_id is not None:
                        self._job_manager.retry_job(self._job_awaiting_reconnect_id)
                        self._job_awaiting_reconnect_id = None
                elif status == "disconnected":
                    self._client = None
                    self._battery_percent = None
                self._refresh_active_profile()
            elif event_type == EventType.CONNECTION_ERROR:
                self._client = None
                self._battery_percent = None
                if self._active_profile is not None:
                    self._set_printer_status(f"Принтер: {self._active_profile.name} ● Error")
                self._set_connect_buttons(text="Подключить", enabled=True)
                self.status_bar.configure(text=f"Статус: ошибка подключения — {payload}")
            elif event_type == EventType.PRINT_PROGRESS:
                self._handle_print_progress(payload)
            elif event_type == EventType.PRINTER_STATUS:
                self._handle_printer_status(payload)
        if self.winfo_exists():
            self.after(100, self._poll_events)

    def _handle_printer_status(self, payload: tuple[str, str, int]) -> None:
        _job_id, meaning, _sub = payload
        label = _PRINTER_STATUS_LABELS.get(meaning, meaning)
        self.status_bar.configure(text=f"Статус принтера: {label}")

    def _progress_text(self, job: PrintJob) -> str:
        """"чанк" is an internal thermal-buffer detail — a real user
        thinks in page/image/document, essentially never in the printer's
        own internal slicing (docs/stage5-ux-plan.md's post-launch UX
        fixes). Shows a percentage (+ "стр. X/Y" when there's more than
        one page) and an ETA once enough of *this* job's own progress has
        been observed to estimate a rate from — periprint-spec.md §7.1's
        sketch ("00:12 осталось (оценка)"). Deliberately not assuming a
        fixed rate from the very first chunk: the first one after a
        (re)connect includes one-time setup (choose_paper_type, etc.), so
        timing it alone would skew the estimate."""
        percent = round(100 * job.completed_chunks / job.total_chunks)
        base = f"{percent}%"
        if job.total_pages > 1:
            base = f"{base} (стр. {job.current_page}/{job.total_pages})"

        now = time.monotonic()
        if job.id not in self._job_progress_start:
            self._job_progress_start[job.id] = (now, job.completed_chunks)
        start_time, start_chunks = self._job_progress_start[job.id]

        elapsed = now - start_time
        done_since_start = job.completed_chunks - start_chunks
        if elapsed < _ETA_MIN_ELAPSED_SECONDS or done_since_start <= 0:
            return base

        rate = done_since_start / elapsed  # chunks per second
        remaining_chunks = job.total_chunks - job.completed_chunks
        eta_seconds = remaining_chunks / rate
        return f"{base} · {_format_eta(eta_seconds)} осталось (оценка)"

    def _handle_print_progress(self, job: PrintJob) -> None:
        self.queue_panel.set_jobs(self._job_manager.list_jobs())

        if job.status == JobStatus.PRINTING and job.total_chunks:
            self.status_bar.configure(text=f"Статус: печать — {self._progress_text(job)}")
        elif job.status in _FINISHED_STATUSES:
            self._job_progress_start.pop(job.id, None)
            printer_name = self._active_profile.name if self._active_profile else "?"
            self._history_store.record(
                HistoryEntry(
                    id=job.id,
                    source_path=job.document.source_path,
                    printer_name=printer_name,
                    status=job.status.value,
                    created_at=job.created_at,
                    finished_at=datetime.now(),
                    error_message=job.error_message,
                )
            )
            self.status_bar.configure(
                text=f"Статус: {job.status.value} — {job.document.source_path}"
            )
        elif job.status == JobStatus.PAUSED_ERROR and self._current_view != "error":
            document_name = Path(job.document.source_path).name
            percent = (
                round(100 * job.completed_chunks / job.total_chunks) if job.total_chunks else 0
            )
            page_info = (
                f", страница {job.current_page} из {job.total_pages}" if job.total_pages > 1 else ""
            )
            self.error_panel.show_error(
                message=(
                    f"Не удалось напечатать «{document_name}»{page_info} "
                    f"(остановилось на {percent}%).\n{job.error_message}"
                ),
                on_reconnect=lambda: self._handle_error_reconnect(job.id),
                on_cancel=lambda: self._handle_error_cancel(job.id),
            )
            self._hide_all_views()
            self.error_panel.grid(row=0, column=0, rowspan=3, sticky="nsew")
            self._current_view = "error"

    def _handle_error_reconnect(self, job_id: str) -> None:
        self._show_previous_main_state()
        self._job_awaiting_reconnect_id = job_id
        self._connect_async()

    def _handle_error_cancel(self, job_id: str) -> None:
        self._show_previous_main_state()
        self._job_manager.cancel_job(job_id)
