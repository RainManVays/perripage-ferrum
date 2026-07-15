from __future__ import annotations

from dataclasses import dataclass, field

from periprint.models.enums import DocumentKind, PaperType


@dataclass
class PrintSettings:
    concentration: int = 1
    break_px: int = 60
    fit_mode: str = "fit_width"  # fit_width | actual_size | crop
    dithering: bool = True
    margin_top_px: int = 0
    margin_bottom_px: int = 40
    # Sent via PeripageClient.choose_paper_type() once per job (see
    # docs/stage5-ux-plan.md §0.1 — the official app calls this before
    # every print action, not once per connection). Defaults to
    # continuous roll since that's this project's actual paper stock;
    # only matters in practice for label-class printers.
    paper_type: PaperType = PaperType.CONTINUOUS_ROLL
    # periprint-spec.md §3 P1: full_page prints the whole rendered page
    # (e.g. a full A4 height, most of it likely blank) scaled to printer
    # width; content_length trims trailing/leading blank vertical space
    # first (DocumentPipeline / infra/renderers/base.py::
    # trim_to_content_height) to save tape. Not the same axis as
    # fit_mode, which only controls horizontal scaling.
    page_mode: str = "full_page"  # full_page | content_length
    # periprint-spec.md §3 P1: "2-4,7" style page selection, 1-based, see
    # utils/page_range.py::parse_page_range(). Empty means all pages —
    # only meaningful for multi-page documents (PDF); single-page
    # documents (image/text) always have exactly page "1" regardless.
    page_range: str = ""
    # N copies of whatever page_range selects (the whole document if
    # page_range is empty) — see docs/stage5-ux-plan.md M5.2: implemented
    # as literal repeated entries in DocumentPipeline's rendered page
    # list, so the existing between-page printBreak() in
    # PrintJobManager._process_job() already inserts a break between
    # copies for free, with no separate protocol/architecture needed.
    copies: int = 1


@dataclass
class DocumentItem:
    id: str
    source_path: str
    kind: DocumentKind
    settings: PrintSettings = field(default_factory=PrintSettings)
