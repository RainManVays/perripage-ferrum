from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from periprint.models.document import DocumentItem
from periprint.models.enums import JobStatus


@dataclass
class PrintJob:
    id: str
    document: DocumentItem
    printer_profile_id: str
    status: JobStatus = JobStatus.QUEUED
    # total_chunks/completed_chunks are the actual internal unit of work
    # PrintJobManager tracks/resumes by — kept as-is, not user-facing.
    # total_pages/current_page exist purely so the UI can talk about
    # "page" (a concept users actually have), not "chunk" (one, an
    # internal detail of thermal-buffer-sized slices within a page, that
    # a real user has no reason to know exists — see
    # docs/stage5-ux-plan.md's post-launch UX fixes).
    total_chunks: int = 0
    completed_chunks: int = 0
    total_pages: int = 0
    current_page: int = 0
    error_message: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
