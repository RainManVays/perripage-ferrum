from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import PIL.Image

from periprint.infra.renderers.base import (
    Renderer,
    normalize_to_1bit,
    slice_into_chunks,
    trim_to_content_height,
)
from periprint.infra.renderers.image_renderer import ImageRenderer
from periprint.infra.renderers.pdf_renderer import PdfRenderer
from periprint.infra.renderers.text_renderer import TextRenderer
from periprint.models.document import DocumentItem, PrintSettings
from periprint.models.enums import DocumentKind
from periprint.utils.page_range import parse_page_range

_RENDERERS: dict[DocumentKind, Renderer] = {
    DocumentKind.IMAGE: ImageRenderer(),
    DocumentKind.PDF: PdfRenderer(),
    DocumentKind.TEXT: TextRenderer(),
}

_EXTENSION_TO_KIND: dict[str, DocumentKind] = {
    ".png": DocumentKind.IMAGE,
    ".jpg": DocumentKind.IMAGE,
    ".jpeg": DocumentKind.IMAGE,
    ".bmp": DocumentKind.IMAGE,
    ".txt": DocumentKind.TEXT,
    ".pdf": DocumentKind.PDF,
    ".md": DocumentKind.MARKDOWN,
}


class UnsupportedDocumentKindError(Exception):
    pass


def detect_document_kind(source_path: str) -> DocumentKind | None:
    return _EXTENSION_TO_KIND.get(Path(source_path).suffix.lower())


@dataclass
class RenderedPage:
    image: PIL.Image.Image  # normalized 1-bit, full page — for preview
    chunks: list[PIL.Image.Image]  # same image sliced by chunk_height_px


@dataclass
class RenderedDocument:
    # One RenderedPage per PDF page (or a single page for image/text
    # documents) — kept as an explicit boundary, not flattened into one
    # continuous raster, so a multi-page PDF prints "постранично" (spec
    # §3): PrintJobManager (Stage 4) can insert a break between pages
    # distinct from the inter-chunk cooldown pause within a page.
    pages: list[RenderedPage]


def _apply_margins(image: PIL.Image.Image, settings: PrintSettings) -> PIL.Image.Image:
    if settings.margin_top_px == 0 and settings.margin_bottom_px == 0:
        return image
    new_height = image.height + settings.margin_top_px + settings.margin_bottom_px
    canvas = PIL.Image.new(image.mode, (image.width, new_height), color=255)
    canvas.paste(image, (0, settings.margin_top_px))
    return canvas


def _count_pages(document: DocumentItem) -> int:
    """Cheap page count for parsing page_range against — just opens the
    PDF's structure, doesn't rasterize anything. Non-PDF documents are
    always exactly 1 "page"."""
    if document.kind != DocumentKind.PDF:
        return 1
    with fitz.open(document.source_path) as pdf:
        return len(pdf)


def _pad_to_canvas_width(image: PIL.Image.Image, canvas_width_px: int) -> PIL.Image.Image:
    """Widens (never stretches) content to canvas_width_px by padding white
    on the right. Needed because printer.printImage() unconditionally
    resizes its input to the model's full native width — feeding it
    anything narrower would silently *stretch* content into the unsafe
    zone instead of leaving it blank there (see printer_specs.py)."""
    if image.width >= canvas_width_px:
        return image
    canvas = PIL.Image.new(image.mode, (canvas_width_px, image.height), color=255)
    canvas.paste(image, (0, 0))
    return canvas


class DocumentPipeline:
    def render_document(
        self,
        document: DocumentItem,
        width_px: int,
        chunk_height_px: int,
        canvas_width_px: int | None = None,
    ) -> RenderedDocument:
        renderer = _RENDERERS.get(document.kind)
        if renderer is None:
            raise UnsupportedDocumentKindError(f"No renderer registered for {document.kind}")

        settings = document.settings
        total_pages = _count_pages(document)
        page_indices = parse_page_range(settings.page_range, total_pages)
        raw_pages = renderer.render(
            document.source_path, width_px, settings.fit_mode, page_indices=page_indices
        )
        target_canvas_width = canvas_width_px or width_px

        pages = []
        for raw_page in raw_pages:
            # Convert to a single-channel mode *before* any white-fill
            # padding: PIL.Image.new(mode, size, color=255) only broadcasts
            # a bare int to every channel for single-channel modes. For
            # "RGB" (the common case — real photos/PDF pages), color=255
            # fills only the red channel, i.e. produces red, not white —
            # which then converts to a *dark* gray, not blank space. Caught
            # by a test that actually checked the padded pixel's value
            # rather than just image dimensions.
            grayscale = raw_page.convert("L")
            if settings.page_mode == "content_length":
                grayscale = trim_to_content_height(grayscale)
            widened = _pad_to_canvas_width(grayscale, target_canvas_width)
            padded = _apply_margins(widened, settings)
            normalized = normalize_to_1bit(padded, settings.dithering)
            chunks = slice_into_chunks(normalized, chunk_height_px)
            pages.append(RenderedPage(image=normalized, chunks=chunks))

        # N copies (docs/stage5-ux-plan.md M5.2): literally repeating
        # already-processed RenderedPage entries — PrintJobManager already
        # inserts a printBreak() between consecutive `pages` entries and
        # counts every entry's chunks toward progress/resume, so repeating
        # here needs no separate protocol/resume handling at all.
        if settings.copies > 1:
            pages = pages * settings.copies
        return RenderedDocument(pages=pages)
