import queue
import threading
import uuid
from datetime import datetime
from tkinter import filedialog
from typing import Any

import customtkinter as ctk

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
from periprint.ui.error_dialog import ErrorDialog
from periprint.ui.preview_panel import PreviewPanel
from periprint.ui.printer_panel import PrinterPanel
from periprint.ui.queue_panel import QueuePanel
from periprint.ui.settings_dialog import SettingsDialog

_DEFAULT_PREVIEW_MODEL = PrinterModel.A40
_DEFAULT_CHUNK_HEIGHT_PX = 220
_FINISHED_STATUSES = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)

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


class MainWindow(ctk.CTk):
    def __init__(
        self,
        printer_manager: PrinterManager | None = None,
        config_store: ConfigStore | None = None,
        history_store: HistoryStore | None = None,
    ) -> None:
        super().__init__()
        self.title("PeriPrint")
        # Tall enough that the settings panel (grown with Stage 5's paper
        # type/page mode/page range/copies controls) doesn't clip at the
        # bottom — window is still resizable by the user either way.
        self.geometry("900x700")

        self._printer_manager = printer_manager or PrinterManager()
        self._config_store = config_store or ConfigStore()
        self._config = self._config_store.load()
        self._history_store = history_store or HistoryStore()
        self._settings_dialog: SettingsDialog | None = None
        self._error_dialog: ErrorDialog | None = None
        self._client: PeripageClient | None = None
        self._active_profile: PrinterProfile | None = None
        self._event_queue: queue.Queue[tuple[EventType, Any]] = queue.Queue()
        self._pipeline = DocumentPipeline()
        self._current_document: DocumentItem | None = None
        self._job_manager = PrintJobManager(
            self._pipeline, self._event_queue, client_provider=lambda: self._client
        )
        self._job_awaiting_reconnect_id: str | None = None

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

        self.queue_panel = QueuePanel(
            body,
            on_select_file=self._handle_select_file,
            on_print_all=self._handle_print_all,
            on_clear=self._handle_clear_queue,
        )
        self.queue_panel.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        self.preview_panel = PreviewPanel(
            body, on_settings_changed=self._handle_preview_settings_changed
        )
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
        path = filedialog.askopenfilename(
            title="Выбрать файл для печати",
            filetypes=[
                ("Поддерживаемые файлы", "*.png *.jpg *.jpeg *.bmp *.txt *.pdf"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return

        kind = detect_document_kind(path)
        if kind is None:
            self.status_bar.configure(text=f"Статус: формат файла не поддерживается — {path}")
            return

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
        self._render_and_show_preview()

        width_px, canvas_width_px, chunk_height_px = self._resolve_render_target()
        job = PrintJob(
            id=str(uuid.uuid4()),
            document=document,
            printer_profile_id=self._active_profile.id if self._active_profile else "",
        )
        self._job_manager.enqueue(job, width_px, chunk_height_px, canvas_width_px)
        self.queue_panel.set_jobs(self._job_manager.list_jobs())

    def _handle_print_all(self) -> None:
        self._job_manager.start()
        self.status_bar.configure(text="Статус: печать очереди запущена")

    def _handle_clear_queue(self) -> None:
        self._job_manager.clear_queue()
        self.queue_panel.set_jobs(self._job_manager.list_jobs())

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
        total_chunks = sum(len(page.chunks) for page in rendered.pages)
        self.status_bar.configure(
            text=f"Статус: превью готово — {len(rendered.pages)} стр., {total_chunks} чанков"
        )

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
                    if self._job_awaiting_reconnect_id is not None:
                        self._job_manager.retry_job(self._job_awaiting_reconnect_id)
                        self._job_awaiting_reconnect_id = None
                elif status == "disconnected":
                    self._client = None
                self._refresh_active_profile()
            elif event_type == EventType.CONNECTION_ERROR:
                self._client = None
                if self._active_profile is not None:
                    self.printer_panel.set_status(f"Принтер: {self._active_profile.name} ● Error")
                self.printer_panel.set_connect_button(text="Подключить", enabled=True)
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

    def _handle_print_progress(self, job: PrintJob) -> None:
        self.queue_panel.set_jobs(self._job_manager.list_jobs())

        if job.status == JobStatus.PRINTING and job.total_chunks:
            self.status_bar.configure(
                text=f"Статус: печать — чанк {job.completed_chunks}/{job.total_chunks}"
            )
        elif job.status in _FINISHED_STATUSES:
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
        elif job.status == JobStatus.PAUSED_ERROR and self._error_dialog is None:
            self._error_dialog = ErrorDialog(
                self,
                message=(
                    f"Не удалось отправить чанк {job.completed_chunks + 1}/{job.total_chunks}.\n"
                    f"{job.error_message}"
                ),
                on_reconnect=lambda: self._handle_error_reconnect(job.id),
                on_cancel=lambda: self._handle_error_cancel(job.id),
            )

    def _handle_error_reconnect(self, job_id: str) -> None:
        self._error_dialog = None
        self._job_awaiting_reconnect_id = job_id
        self._connect_async()

    def _handle_error_cancel(self, job_id: str) -> None:
        self._error_dialog = None
        self._job_manager.cancel_job(job_id)
