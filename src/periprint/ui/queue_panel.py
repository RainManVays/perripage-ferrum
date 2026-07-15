from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk
from tkinterdnd2 import DND_FILES

from periprint.models.enums import JobStatus
from periprint.models.job import PrintJob

_STATUS_LABELS = {
    JobStatus.QUEUED: "в очереди",
    JobStatus.RENDERING: "рендеринг...",
    JobStatus.PRINTING: "печать",
    JobStatus.PAUSED_ERROR: "ошибка — приостановлено",
    JobStatus.DONE: "готово",
    JobStatus.FAILED: "не удалось",
    JobStatus.CANCELLED: "отменено",
}
_ACTIVE_STATUSES = (JobStatus.RENDERING, JobStatus.PRINTING)


def _format_job_line(job: PrintJob) -> str:
    name = Path(job.document.source_path).name
    status = _STATUS_LABELS[job.status]
    if job.status == JobStatus.PRINTING and job.total_chunks:
        # Percentage + page number, never "chunk" — that's an internal
        # thermal-buffer detail a real user has no reason to know about
        # (docs/stage5-ux-plan.md's post-launch UX fixes).
        percent = round(100 * job.completed_chunks / job.total_chunks)
        if job.total_pages > 1:
            status = f"{status} {percent}% (стр. {job.current_page}/{job.total_pages})"
        else:
            status = f"{status} {percent}%"
    elif job.status == JobStatus.PAUSED_ERROR and job.error_message:
        status = f"{status}: {job.error_message}"
    return f"{name} — {status}"


class QueuePanel(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTkBaseClass,
        on_select_file: Callable[[], None] | None = None,
        on_files_dropped: Callable[[list[str]], None] | None = None,
        on_print_all: Callable[[], None] | None = None,
        on_clear: Callable[[], None] | None = None,
        on_move_job: Callable[[str, int], None] | None = None,
        on_stop_job: Callable[[str], None] | None = None,
        on_resume_job: Callable[[str], None] | None = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._on_move_job = on_move_job
        self._on_stop_job = on_stop_job
        self._on_resume_job = on_resume_job
        self._row_widgets: list[ctk.CTkFrame] = []

        # grid, not pack: the queue list and the dropzone previously split
        # space via pack's leftover-space rule, which handed the dropzone
        # only its fixed minimum (80px) and gave the list everything
        # else — reported as "очередь занимает слишком много места".
        # Equal-weight grid rows give them an even 50/50 split instead.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)

        title = ctk.CTkLabel(self, text="ОЧЕРЕДЬ ПЕЧАТИ", font=ctk.CTkFont(weight="bold"))
        title.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 0))

        # A scrollable frame of per-job rows (not a read-only CTkTextbox)
        # — needed so each job can carry its own move/stop/resume buttons.
        self.queue_list = ctk.CTkScrollableFrame(self)
        self.queue_list.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

        self._empty_label = ctk.CTkLabel(self.queue_list, text="(очередь пуста)")
        self._empty_label.pack(anchor="w")

        self.dropzone = ctk.CTkLabel(
            self,
            text="Перетащите файлы сюда\nили нажмите для выбора",
            fg_color=("gray85", "gray20"),
            corner_radius=8,
            cursor="hand2",
        )
        self.dropzone.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        if on_select_file is not None:
            self.dropzone.bind("<Button-1>", lambda _event: on_select_file())
        if on_files_dropped is not None:
            self.dropzone.drop_target_register(DND_FILES)
            self.dropzone.dnd_bind(
                "<<Drop>>",
                lambda event: on_files_dropped(list(self.dropzone.tk.splitlist(event.data))),
            )

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))

        self.print_all_button = ctk.CTkButton(button_row, text="Печать всё", command=on_print_all)
        self.print_all_button.pack(side="left", padx=(0, 8))

        self.clear_button = ctk.CTkButton(button_row, text="Очистить", command=on_clear)
        self.clear_button.pack(side="left")

    def set_jobs(self, jobs: list[PrintJob]) -> None:
        for row in self._row_widgets:
            row.destroy()
        self._row_widgets = []

        if not jobs:
            self._empty_label.pack(anchor="w")
            return
        self._empty_label.pack_forget()

        for job in jobs:
            row = ctk.CTkFrame(self.queue_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            # grid, not pack, within the row: pack's "first widget with
            # expand=True claims space greedily" rule fought with the
            # label's dynamic wraplength here — a long, wrapped error
            # message ended up claiming ~all of the row's width before
            # pack ever got to the trailing buttons, squeezing e.g.
            # "Продолжить" down to a 1px-wide, invisible-but-present
            # button (found by inspecting winfo_width() directly, not
            # visible at all in a screenshot). grid's column weights
            # reserve the button columns' natural width up front, giving
            # column 0 (the label) only whatever's actually left.
            row.grid_columnconfigure(0, weight=1)
            label = ctk.CTkLabel(row, text=_format_job_line(job), anchor="w", justify="left")
            label.grid(row=0, column=0, sticky="ew")
            # CTkLabel doesn't wrap by default — a long error message (a
            # printer status string, or an exception's str()) just ran
            # off the edge instead. wraplength needs an actual pixel
            # value, not "fill the available space" like pack/grid do
            # for geometry, so it's kept in sync via <Configure> as the
            # label's own allocated width changes (window resize, panel
            # split, etc.), not set once at creation time.
            label.bind(
                "<Configure>", lambda event, widget=label: widget.configure(wraplength=event.width)
            )

            column = 1

            # Only QUEUED jobs are meaningfully reorderable — see
            # PrintJobManager.move_job()'s own docstring for why swapping
            # past an already-started/finished job wouldn't do anything.
            if self._on_move_job is not None and job.status == JobStatus.QUEUED:
                ctk.CTkButton(
                    row,
                    text="▲",
                    width=28,
                    command=lambda job_id=job.id: self._on_move_job(job_id, -1),
                ).grid(row=0, column=column, padx=(4, 0))
                column += 1
                ctk.CTkButton(
                    row,
                    text="▼",
                    width=28,
                    command=lambda job_id=job.id: self._on_move_job(job_id, 1),
                ).grid(row=0, column=column, padx=(4, 0))
                column += 1

            if self._on_stop_job is not None and job.status in _ACTIVE_STATUSES:
                ctk.CTkButton(
                    row,
                    text="Стоп",
                    width=56,
                    fg_color="darkred",
                    hover_color="firebrick",
                    command=lambda job_id=job.id: self._on_stop_job(job_id),
                ).grid(row=0, column=column, padx=(4, 0))
                column += 1

            if self._on_resume_job is not None and job.status == JobStatus.PAUSED_ERROR:
                ctk.CTkButton(
                    row,
                    text="Продолжить",
                    width=90,
                    command=lambda job_id=job.id: self._on_resume_job(job_id),
                ).grid(row=0, column=column, padx=(4, 0))
                column += 1

            self._row_widgets.append(row)
