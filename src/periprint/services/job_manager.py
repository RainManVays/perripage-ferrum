from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import PIL.Image

from periprint.infra.peripage_client import PeripageClient
from periprint.infra.printer_status_listener import PAUSE_WORTHY_STATUSES
from periprint.models.enums import JobStatus
from periprint.models.job import PrintJob
from periprint.services.events import EventType
from periprint.services.pipeline import DocumentPipeline

logger = logging.getLogger(__name__)

_BASE_PAUSE_SECONDS = 1.0
_DARK_THRESHOLD = 0.3
_DARK_STREAK_EXTRA_SECONDS = 0.4
_MAX_PAUSE_SECONDS = 5.0
# Per-row delay inside a single printImage() call. Stage 0's own short
# (40-150px) test images suggested delay=0.05 gave the best density, but a
# real HCI Bluetooth trace of the official Peripage app (Stage 4, see
# docs/hardware-notes.md) showed it sends rows with NO manual delay at all
# — ~200-byte frames every 2-4ms, paced only by natural Bluetooth transport
# speed. Confirmed live: 0.05s/row caused silent data loss on longer prints
# (the printer's own receive buffer desyncs waiting on slow input), while a
# near-zero delay fixed it. Kept nonzero only to avoid flooding the local
# socket send buffer pointlessly, not for the printer's benefit.
_ROW_DELAY_SECONDS = 0.001
_PAGE_BREAK_SIZE = 60
# printBreak()'s size byte maps to physical feed at roughly 0.126mm/unit
# (measured on a real A40, 2026-07-15 — size=60 feeds ~9mm, size=100 feeds
# ~14mm, linear fit). That's enough for a *visible gap* between two pieces
# of content that keep printing afterward (confirmed: page breaks mid-job
# work fine at 60). It is NOT enough at the very end of a job: the printer
# holds some fixed length of already-printed paper between the head and
# the tear slot, and nothing prints afterward to push it the rest of the
# way out — confirmed live: a job ending with only _PAGE_BREAK_SIZE left
# its last content stuck inside the printer, invisible/untearable, until
# an extra ~90mm of feed was sent manually. 255 (printBreak's max, ~33mm)
# is used only for this final tear-off, never between pages/chunks.
#
# WORKAROUND, not a general solution: calibrated only against plain
# continuous roll paper printing our own test content, with no inherent
# concept of a "page". Revisit once page-format printing exists (Stage 5
# M5.1 — full-page A4/A5/Letter mode, docs/stage5-ux-plan.md): a rendered
# document may already carry its own bottom margin (from the source
# Word/PDF, or PrintSettings.margin_bottom_px), in which case adding a
# fixed 33mm on top could be redundant or even wrong for that paper/
# format combination — this constant likely needs to become
# format/paper-aware rather than a single global default.
_FINAL_TEAR_OFF_SIZE = 255


def _black_ratio(image: PIL.Image.Image) -> float:
    total = image.width * image.height
    if total == 0:
        return 0.0
    histogram = image.histogram()
    return histogram[0] / total if histogram else 0.0


def _cooldown_seconds(black_ratio: float, dark_streak: int) -> float:
    """Adaptive inter-chunk pause (spec §4.3): flat base pause normally,
    growing with consecutive dark chunks so a long dark run cools down more
    — this is deliberately time.sleep()-based application logic, not
    printBreak() (which feeds paper, see docs/hardware-notes.md /
    Risk Flags: the two are different operations the spec's own wording
    conflates)."""
    if black_ratio < _DARK_THRESHOLD:
        return _BASE_PAUSE_SECONDS
    return min(_MAX_PAUSE_SECONDS, _BASE_PAUSE_SECONDS + _DARK_STREAK_EXTRA_SECONDS * dark_streak)


class PrintJobManager:
    """Queue + single worker thread. Renders lazily — only when a job is
    actually taken up, never all queued documents' rasters at once (per the
    spec's memory NFR). A chunk-send failure pauses the job (PAUSED_ERROR)
    without losing progress: resuming re-renders the document but skips
    chunks already sent, tracked via PrintJob.completed_chunks — so a
    mid-print Bluetooth drop resumes from the last successful chunk, not
    from the start."""

    def __init__(
        self,
        pipeline: DocumentPipeline,
        event_queue: queue.Queue[tuple[EventType, object]],
        client_provider: Callable[[], PeripageClient | None],
    ) -> None:
        self._pipeline = pipeline
        self._event_queue = event_queue
        self._client_provider = client_provider
        self._jobs: dict[str, PrintJob] = {}
        self._order: list[str] = []
        self._render_targets: dict[str, tuple[int, int, int]] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        # job_id -> stop flag, only present while that job is actually
        # being processed (_process_job() adds/removes its own entry) —
        # see request_stop().
        self._stop_events: dict[str, threading.Event] = {}

    def enqueue(
        self,
        job: PrintJob,
        width_px: int,
        chunk_height_px: int,
        canvas_width_px: int | None = None,
    ) -> None:
        """Adds the job as QUEUED but does NOT start printing — files
        accumulate in the queue as the user picks them (spec journey 2.3);
        printing only begins once start() is called ("Печать всё").
        width_px is the safe content width content gets rendered/wrapped
        into; canvas_width_px (defaults to width_px) is the full native
        width the final image is padded out to before printing — see
        DocumentPipeline.render_document / printer_specs.py."""
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._render_targets[job.id] = (width_px, chunk_height_px, canvas_width_px or width_px)
        self._emit(job)

    def start(self) -> None:
        self._ensure_worker()

    def list_jobs(self) -> list[PrintJob]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order]

    def retry_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.status == JobStatus.PAUSED_ERROR:
                job.status = JobStatus.QUEUED
                job.error_message = None
        if job is not None:
            self._emit(job)
        self._ensure_worker()

    def cancel_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.status in (JobStatus.QUEUED, JobStatus.PAUSED_ERROR):
                job.status = JobStatus.CANCELLED
        if job is not None:
            self._emit(job)

    def clear_queue(self) -> None:
        """Cancels anything not actively communicating with the printer
        right now and drops finished/cancelled entries from the visible
        list. QUEUED and PAUSED_ERROR are both safe to cancel outright —
        neither is holding an open exchange with the printer at this
        instant (PAUSED_ERROR is just sitting there, already stopped,
        waiting on a user decision; see also retry_job()/move_job()'s own
        "is this job actually QUEUED" guards). Only RENDERING/PRINTING is
        left alone, since that one genuinely is mid-flight. Originally
        left PAUSED_ERROR untouched too — that turned out to be a real
        bug report ("Очистить ничего не делает" whenever the queue held
        an errored job)."""
        with self._lock:
            remaining = []
            for job_id in self._order:
                job = self._jobs[job_id]
                if job.status in (JobStatus.QUEUED, JobStatus.PAUSED_ERROR):
                    job.status = JobStatus.CANCELLED
                    continue
                if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
                    continue
                remaining.append(job_id)
            self._order = remaining

    def request_stop(self, job_id: str) -> None:
        """User-initiated stop for a job that's actually RENDERING/
        PRINTING right now — distinct from cancel_job()/clear_queue()
        (which only ever touch QUEUED/PAUSED_ERROR, jobs not actively
        exchanging data with the printer). Checked at the same points
        _process_job() already checks for a printer-pushed abort signal;
        ends the job as CANCELLED, not PAUSED_ERROR, since this was a
        deliberate user choice, not something to "reconnect and retry".
        A no-op if the job isn't currently being processed — there's
        nothing running to stop."""
        with self._lock:
            event = self._stop_events.get(job_id)
        if event is not None:
            event.set()

    def move_job(self, job_id: str, delta: int) -> None:
        """Stage 5 M5.4 queue reorder. delta<0 moves the job earlier
        (prints sooner), delta>0 later. Only ever swaps with the nearest
        *other still-QUEUED* neighbor, skipping past anything else
        (RENDERING/PRINTING/finished) in between: the worker's
        _next_queued() only ever cares about relative order among QUEUED
        jobs, so swapping past a job that's already started (or already
        done) wouldn't change execution order, only produce a
        confusing-looking list. A no-op (not an error) if the job itself
        isn't QUEUED or there's no QUEUED neighbor in that direction."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return
            index = self._order.index(job_id)
            step = 1 if delta > 0 else -1
            neighbor_index = index + step
            while 0 <= neighbor_index < len(self._order):
                neighbor_id = self._order[neighbor_index]
                if self._jobs[neighbor_id].status == JobStatus.QUEUED:
                    self._order[index], self._order[neighbor_index] = (
                        self._order[neighbor_index],
                        self._order[index],
                    )
                    break
                neighbor_index += step
        self._emit(job)

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()

    def _next_queued(self) -> PrintJob | None:
        with self._lock:
            for job_id in self._order:
                job = self._jobs[job_id]
                if job.status == JobStatus.QUEUED:
                    return job
        return None

    def _worker_loop(self) -> None:
        while True:
            job = self._next_queued()
            if job is None:
                return
            self._process_job(job)

    def _emit(self, job: PrintJob) -> None:
        self._event_queue.put((EventType.PRINT_PROGRESS, job))

    def _process_job(self, job: PrintJob) -> None:
        client = self._client_provider()
        if client is None or not client.is_connected():
            job.status = JobStatus.PAUSED_ERROR
            job.error_message = "Принтер не подключён"
            self._emit(job)
            return

        # The printer can push its own spontaneous status packets mid-job —
        # most importantly an explicit "abort_print" signal (e.g. the user
        # opened the cover), confirmed via a real HCI trace of the official
        # app + decompiling it, see
        # docs/bluetooth-protocol-trace-analysis.md §4/§7.3. Without this,
        # we only find out something's wrong when a socket write eventually
        # fails, which can be well after the printer already gave up.
        abort_requested = threading.Event()
        last_status_reason: list[str] = []

        def handle_status(meaning: str, sub: int) -> None:
            self._event_queue.put((EventType.PRINTER_STATUS, (job.id, meaning, sub)))
            if meaning in PAUSE_WORTHY_STATUSES:
                last_status_reason.append(meaning)
                abort_requested.set()

        # User-initiated stop (request_stop(), Stage 5 M5.4 "Стоп" button)
        # — separate flag/outcome from the printer-pushed abort above: a
        # deliberate stop ends the job as CANCELLED, not PAUSED_ERROR.
        stop_requested = threading.Event()
        with self._lock:
            self._stop_events[job.id] = stop_requested

        def bail_if_stop_requested() -> bool:
            if stop_requested.is_set():
                job.status = JobStatus.CANCELLED
                self._emit(job)
                return True
            return False

        try:
            client.start_status_listening(handle_status)
        except Exception as exc:
            logger.warning("Could not start printer status listening: %s", exc)

        try:
            job.status = JobStatus.RENDERING
            self._emit(job)

            width_px, chunk_height_px, canvas_width_px = self._render_targets[job.id]
            try:
                rendered = self._pipeline.render_document(
                    job.document, width_px, chunk_height_px, canvas_width_px
                )
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error_message = str(exc)
                self._emit(job)
                return

            job.total_chunks = sum(len(page.chunks) for page in rendered.pages)
            job.total_pages = len(rendered.pages)
            job.status = JobStatus.PRINTING
            self._emit(job)

            # Called once per job, not once per connection: a live HCI trace
            # of the official app showed this sent before *every* print
            # action, byte-identical each time (docs/stage5-ux-plan.md
            # §0.1) — not something to cache from a previous job/connect().
            try:
                client.choose_paper_type(job.document.settings.paper_type)
            except Exception as exc:
                job.status = JobStatus.PAUSED_ERROR
                job.error_message = str(exc)
                self._emit(job)
                return

            chunk_index = 0
            dark_streak = 0
            for page_number, page in enumerate(rendered.pages):
                is_last_page = page_number == len(rendered.pages) - 1
                job.current_page = page_number + 1

                for chunk_number, chunk in enumerate(page.chunks):
                    if bail_if_stop_requested():
                        return
                    if abort_requested.is_set():
                        job.status = JobStatus.PAUSED_ERROR
                        job.error_message = f"Принтер сообщил: {last_status_reason[-1]}"
                        self._emit(job)
                        return

                    is_last_chunk_of_page = chunk_number == len(page.chunks) - 1
                    already_sent = chunk_index < job.completed_chunks
                    chunk_index += 1
                    if already_sent:
                        continue

                    try:
                        # Not client.print_image(): that goes through
                        # peripage.Printer.printImage(), which re-slices
                        # anything >255 rows into multiple reset()+header
                        # groups internally (see printer_specs.py TODO /
                        # docs/printer-protocol-implementation-plan.md
                        # Phase 4). print_image_no_height_limit() sends one
                        # image = one reset(), verified on real hardware —
                        # matters whenever chunk_height_px is configured
                        # above 255, not just for very tall documents.
                        client.print_image_no_height_limit(chunk, delay=_ROW_DELAY_SECONDS)
                    except Exception as exc:
                        job.status = JobStatus.PAUSED_ERROR
                        job.error_message = str(exc)
                        self._emit(job)
                        return

                    job.completed_chunks += 1
                    self._emit(job)

                    if not (is_last_page and is_last_chunk_of_page):
                        ratio = _black_ratio(chunk)
                        dark_streak = dark_streak + 1 if ratio >= _DARK_THRESHOLD else 0
                        time.sleep(_cooldown_seconds(ratio, dark_streak))

                if not is_last_page:
                    if bail_if_stop_requested():
                        return
                    try:
                        client.print_break(_PAGE_BREAK_SIZE)
                    except Exception as exc:
                        job.status = JobStatus.PAUSED_ERROR
                        job.error_message = str(exc)
                        self._emit(job)
                        return

            try:
                # Not _PAGE_BREAK_SIZE: this is the final tear-off, not a
                # gap between two pieces of content — needs to be much
                # larger to actually push the last printed content past
                # the printer's internal head-to-tear-slot distance, see
                # _FINAL_TEAR_OFF_SIZE above.
                client.print_break(_FINAL_TEAR_OFF_SIZE)
            except Exception as exc:
                logger.warning("Trailing tear-off feed failed (best-effort): %s", exc)

            job.status = JobStatus.DONE
            self._emit(job)
        finally:
            client.stop_status_listening()
            with self._lock:
                self._stop_events.pop(job.id, None)
