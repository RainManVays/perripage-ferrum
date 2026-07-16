from enum import IntEnum, StrEnum


class PrinterModel(StrEnum):
    A6 = "A6"
    A6_PLUS = "A6p"
    A40 = "A40"
    A40_PLUS = "A40p"


class PaperType(IntEnum):
    """Opcode 10ff1003 + value (method B()/"choosePaperType" in the
    official app — see docs/bluetooth-protocol-trace-analysis.md §7.2).
    Likely only relevant to label-class printers, not a plain continuous
    thermal roll like this project's A40, but cheap to support."""

    FOLDED_BLACK_MARK = 1
    CONTINUOUS_ROLL = 2
    ADHESIVE_GAP = 3
    PERFORATED = 4


class DocumentKind(StrEnum):
    IMAGE = "image"
    TEXT = "text"
    MARKDOWN = "markdown"
    PDF = "pdf"


class PageFormat(StrEnum):
    """Target page size the rendered content is scaled down to fit (never
    cropped — docs/stage5-ux-plan.md M5.5 postmortem #4: an earlier
    crop-based design sliced a landscape photo in half instead of shrinking
    it). If the scaled content is taller than one target page, it
    paginates across as many physical pieces as it actually needs, printed
    back to back with the existing between-page printBreak() as the cut
    line — HALF/QUARTER don't always mean "exactly 2/4 pieces"; a source
    shorter than one target page becomes a single piece. Deliberately NOT
    named A5/A6 — those names are already PrinterModel's (a different
    axis: fixed roll width per hardware model, not user-selectable per
    job); UI labels still say "А5"/"А6" since that's the user's own
    vocabulary."""

    NATIVE = "native"  # today's behavior — no imposition, fills the roll width
    HALF = "half"  # target width/height = real A5 (148x210mm)
    QUARTER = "quarter"  # target width/height = real A6 (105x148mm)
    CUSTOM = "custom"  # explicit tile size in mm


class JobStatus(StrEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    PRINTING = "printing"
    PAUSED_ERROR = "paused_error"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
