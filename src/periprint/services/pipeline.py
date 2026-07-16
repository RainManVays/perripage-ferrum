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
    split_into_grid,
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


def _shrink_tile_if_wider_than(tile: PIL.Image.Image, width_px: int) -> PIL.Image.Image:
    """Only re-fits a tile down to width_px if it's actually wider —
    unconditionally re-fitting every tile (an earlier, real bug: reported
    live as "печатает увеличенную полноразмерную страницу... но не
    вертикально, а горизонтально") stretched each rotated tile *back up*
    to the full roll width, which defeats the entire point of imposition:
    a real A5 tile is supposed to end up narrower than a "A4"-equivalent
    canvas (~70% of the width, matching A5's real 148mm vs A4's 210mm) —
    _pad_to_canvas_width below already pads that narrower content out with
    blank margin, exactly like any other narrower-than-canvas content in
    this pipeline. Only genuinely oversized tiles (source content taller
    than it is wide by more than the tile_count, an unusual shape) need
    shrinking here at all."""
    if tile.width <= width_px:
        return tile
    return fit_to_width(tile, width_px, "fit_width")


def _apply_page_format(
    raw_page: PIL.Image.Image, settings: PrintSettings, width_px: int
) -> list[PIL.Image.Image]:
    """docs/stage5-ux-plan.md M5.5 postmortem #2 (real bug, found via a
    hand-traced diagram after live testing, not guessed): imposition MUST
    split the page first, while it's still in its original, unrotated
    shape — HALF/QUARTER's split always cuts by height, so splitting an
    *already globally-rotated* page (the previous order) cut across the
    rotated content instead of along the original top/bottom halves,
    mixing both halves into every tile whenever rotation_degrees was
    anything but 0. rotation_degrees is instead applied to each tile
    *after* it's been correctly split out — independent of imposition's
    own fixed per-tile 90° reorientation (rotate_each), so "Поворот"
    behaves the same regardless of which "Формат" is selected, and also
    doubles as a manual override for split_into_tiles' own untested
    rotation direction (flip to 180° if it comes out backwards on real
    hardware).

    Postmortem #3: QUARTER used the same 1-D 4-band split as HALF (just
    with 4 bands instead of 2), which produces oddly elongated strips, not
    real A6 proportions. Confirmed against a hand-drawn packing diagram
    from the user (2x2 grid of A6 cells tiling one A4-equivalent sheet,
    each cell labeled "0°" — i.e. correctly shaped with no rotation at
    all): a plain 2x2 grid of the *unrotated* page already lands on real
    A6 dimensions on its own, unlike HALF's bands (landscape-shaped,
    genuinely need a 90° rotation to reach A5's portrait shape)."""
    if settings.page_format == PageFormat.HALF:
        return [
            _shrink_tile_if_wider_than(rotate_page(tile, settings.rotation_degrees), width_px)
            for tile in split_into_tiles(raw_page, 2, rotate_each=True)
        ]
    if settings.page_format == PageFormat.QUARTER:
        # 2x2 grid, NOT rotated by default — unlike HALF, a plain quadrant
        # of an unrotated "A4"-equivalent page already lands on real A6
        # proportions with no rotation needed (see split_into_grid()).
        return [
            _shrink_tile_if_wider_than(rotate_page(tile, settings.rotation_degrees), width_px)
            for tile in split_into_grid(raw_page, 2, 2, rotate_each=False)
        ]
    if settings.page_format == PageFormat.CUSTOM:
        tile_width_px = mm_to_px(settings.custom_tile_width_mm)
        page = fit_to_width(raw_page, tile_width_px, "fit_width")
        tile_height_px = mm_to_px(settings.custom_tile_height_mm)
        tile_count = max(1, -(-page.height // tile_height_px))  # ceil division
        return [
            _shrink_tile_if_wider_than(rotate_page(tile, settings.rotation_degrees), width_px)
            for tile in split_into_tiles(page, tile_count, rotate_each=False)
        ]
    # NATIVE — no imposition, rotation applies to the whole page and fills
    # the full canvas width (the standalone "rotate my sideways photo"
    # case) rather than only shrinking if it overflows.
    page = rotate_page(raw_page, settings.rotation_degrees)
    return [fit_to_width(page, width_px, "fit_width")]


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
