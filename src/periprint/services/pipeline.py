from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import PIL.Image

from periprint.infra.renderers.base import (
    Renderer,
    fit_to_width,
    normalize_to_1bit,
    rotate_page,
    slice_into_chunks,
    split_into_tiles,
    trim_to_content_height,
)
from periprint.infra.renderers.image_renderer import ImageRenderer
from periprint.infra.renderers.pdf_renderer import PdfRenderer
from periprint.infra.renderers.text_renderer import TextRenderer
from periprint.models.document import DocumentItem, PrintSettings
from periprint.models.enums import DocumentKind, PageFormat
from periprint.models.printer_specs import mm_to_px
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

# Only extensions whose kind has an actual registered renderer — .md maps
# to a real DocumentKind but MarkdownRenderer doesn't exist yet (Stage 6/
# P1), so detect_document_kind() accepts it while rendering always fails
# later. Used to tell the user what genuinely works right now (dropzone
# caption, docs/stage5-ux-plan.md's post-launch UX fixes) rather than
# silently including a format that's guaranteed to error out.
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(
    sorted(ext for ext, kind in _EXTENSION_TO_KIND.items() if kind in _RENDERERS)
)


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


# Real ISO paper dimensions in mm (portrait) used as HALF/QUARTER's target
# tile size — see _apply_page_format's docstring for why these aren't
# derived by cropping a fixed number of pieces out of the page anymore.
_FORMAT_TARGET_MM: dict[PageFormat, tuple[float, float]] = {
    PageFormat.HALF: (148.0, 210.0),  # real A5
    PageFormat.QUARTER: (105.0, 148.0),  # real A6
}


def _apply_page_format(
    raw_page: PIL.Image.Image, settings: PrintSettings, width_px: int
) -> list[PIL.Image.Image]:
    """docs/stage5-ux-plan.md M5.5 postmortem #4 (real bug, reported live:
    "он обрезан, опять" — a landscape source photo was being cropped in
    half, not scaled down): HALF/QUARTER used to always crop the page into
    a *fixed* count of pieces (2 or 4) at full original scale, discarding
    whatever fell outside each piece — correct only when the source
    happens to be exactly as tall as N target pages' worth of content
    (e.g. a real A4 PDF page), destructive for anything shorter (a single
    photo gets sliced through the middle). Fixed to match CUSTOM's
    existing, already-correct behavior: scale the whole page down to the
    format's target width (148mm for A5, 105mm for A6 — nothing outside
    the frame is lost, just shrunk) and only paginate into multiple
    physical pieces if the scaled result is *actually* taller than one
    target page — a short/landscape source cleanly becomes a single tile,
    a genuinely tall document still splits across as many pages as it
    needs (same page count as before for actually-A4-shaped content,
    since scaling a real A4 page to A5's width naturally yields ~2 A5-tall
    pages worth of height, ~4 for A6). This also drops the per-piece 90°
    auto-rotation from earlier postmortems entirely: with scaling instead
    of cropping there is no "landscape band that must be rotated to reach
    A5's portrait shape" anymore — content stays in its own orientation,
    rotation_degrees is the only rotation control left, same as CUSTOM."""
    if settings.page_format in _FORMAT_TARGET_MM:
        target_width_mm, target_height_mm = _FORMAT_TARGET_MM[settings.page_format]
    elif settings.page_format == PageFormat.CUSTOM:
        target_width_mm = settings.custom_tile_width_mm
        target_height_mm = settings.custom_tile_height_mm
    else:
        # NATIVE — no imposition, rotation fills the full canvas width
        # (the standalone "rotate my sideways photo" case).
        page = rotate_page(raw_page, settings.rotation_degrees)
        return [fit_to_width(page, width_px, "fit_width")]

    page = rotate_page(raw_page, settings.rotation_degrees)
    # Clamped to width_px: target_width_mm is a fixed real-world size
    # (148mm for A5, 105mm for A6) independent of which printer model is
    # active — on a narrow-roll model (e.g. the A6 line, ~48mm real width)
    # the requested size may simply not be physically achievable. Scaling
    # down further to whatever the active printer *can* do is the safe
    # fallback; _pad_to_canvas_width later on only ever widens, never
    # shrinks, so this has to be enforced here.
    tile_width_px = min(mm_to_px(target_width_mm), width_px)
    page = fit_to_width(page, tile_width_px, "fit_width")
    tile_height_px = mm_to_px(target_height_mm)
    tile_count = max(1, -(-page.height // tile_height_px))  # ceil division
    return split_into_tiles(page, tile_count)


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
            for tile in _apply_page_format(raw_page, settings, width_px):
                # Convert to a single-channel mode *before* any white-fill
                # padding: PIL.Image.new(mode, size, color=255) only
                # broadcasts a bare int to every channel for single-channel
                # modes. For "RGB" (the common case — real photos/PDF
                # pages), color=255 fills only the red channel, i.e.
                # produces red, not white — which then converts to a *dark*
                # gray, not blank space. Caught by a test that actually
                # checked the padded pixel's value rather than just image
                # dimensions.
                grayscale = tile.convert("L")
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
