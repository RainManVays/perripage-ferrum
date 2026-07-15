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


class JobStatus(StrEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    PRINTING = "printing"
    PAUSED_ERROR = "paused_error"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
