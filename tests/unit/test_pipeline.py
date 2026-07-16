import uuid
from pathlib import Path

import PIL.Image
import pytest

from periprint.models.document import DocumentItem, PrintSettings
from periprint.models.enums import DocumentKind, PageFormat
from periprint.services.pipeline import (
    DocumentPipeline,
    UnsupportedDocumentKindError,
    detect_document_kind,
)


def _image_document(tmp_path: Path, width: int = 100, height: int = 200) -> DocumentItem:
    source_path = tmp_path / "source.png"
    PIL.Image.new("RGB", (width, height), color=(0, 0, 0)).save(source_path)
    return DocumentItem(id=str(uuid.uuid4()), source_path=str(source_path), kind=DocumentKind.IMAGE)


def test_render_document_applies_margins_and_chunks(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=100)
    document.settings = PrintSettings(margin_top_px=10, margin_bottom_px=20, dithering=False)

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=50)

    assert len(rendered.pages) == 1
    page = rendered.pages[0]
    # source scaled to width 100 stays 100 tall (fit_width, already correct
    # width) + 10 top margin + 20 bottom margin = 130
    assert page.image.height == 130
    assert page.image.mode == "1"
    assert sum(chunk.height for chunk in page.chunks) == 130
    assert len(page.chunks) == 3  # 50, 50, 30


def test_render_document_default_settings_include_bottom_margin(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=100)

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=200)

    # PrintSettings defaults to margin_bottom_px=40 (tear-off allowance),
    # margin_top_px=0 — not literally "no margins".
    assert rendered.pages[0].image.height == 140


def test_render_document_pads_to_canvas_width_without_stretching(tmp_path: Path) -> None:
    """Content must be rendered at the safe content width and padded (not
    stretched) out to the full canvas width — see printer_specs.py: feeding
    printer.printImage() anything narrower than the model's native width
    makes it stretch content into the physically-unreliable zone instead of
    leaving it blank there."""
    document = _image_document(tmp_path, width=100, height=50)
    document.settings = PrintSettings(margin_top_px=0, margin_bottom_px=0)

    rendered = DocumentPipeline().render_document(
        document, width_px=100, chunk_height_px=200, canvas_width_px=150
    )

    page = rendered.pages[0]
    assert page.image.width == 150
    assert page.image.height == 50
    # The padding (white) must be a plain right-side extension, not a
    # rescale of the original 100px-wide content.
    assert page.image.getpixel((149, 0)) != 0


def test_render_document_canvas_width_defaults_to_width_px(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=50)

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=200)

    assert rendered.pages[0].image.width == 100


def test_page_mode_content_length_trims_blank_pdf_tail(tmp_path: Path) -> None:
    """periprint-spec.md §3 P1: "по длине контента" should crop a mostly
    blank PDF page down to its actual content height, unlike the default
    full_page mode which keeps the entire rendered page."""
    import fitz

    pdf_path = tmp_path / "tall.pdf"
    document_handle = fitz.open()
    page = document_handle.new_page(width=200, height=1000)  # very tall, mostly blank
    page.insert_text((20, 20), "short line near the top")
    document_handle.save(str(pdf_path))
    document_handle.close()

    document = DocumentItem(id="x", source_path=str(pdf_path), kind=DocumentKind.PDF)
    document.settings = PrintSettings(page_mode="full_page", margin_top_px=0, margin_bottom_px=0)
    full_page = DocumentPipeline().render_document(document, width_px=200, chunk_height_px=5000)

    document.settings = PrintSettings(
        page_mode="content_length", margin_top_px=0, margin_bottom_px=0
    )
    trimmed = DocumentPipeline().render_document(document, width_px=200, chunk_height_px=5000)

    assert trimmed.pages[0].image.height < full_page.pages[0].image.height / 4


def test_page_range_selects_only_requested_pdf_pages(tmp_path: Path) -> None:
    import fitz

    pdf_path = tmp_path / "multi.pdf"
    document_handle = fitz.open()
    for _ in range(5):
        document_handle.new_page(width=200, height=100)
    document_handle.save(str(pdf_path))
    document_handle.close()

    document = DocumentItem(id="x", source_path=str(pdf_path), kind=DocumentKind.PDF)
    document.settings = PrintSettings(page_range="2-3,5")

    rendered = DocumentPipeline().render_document(document, width_px=200, chunk_height_px=5000)

    assert len(rendered.pages) == 3


def test_page_range_invalid_syntax_propagates(tmp_path: Path) -> None:
    import fitz

    pdf_path = tmp_path / "single.pdf"
    document_handle = fitz.open()
    document_handle.new_page(width=200, height=100)
    document_handle.save(str(pdf_path))
    document_handle.close()

    document = DocumentItem(id="x", source_path=str(pdf_path), kind=DocumentKind.PDF)
    document.settings = PrintSettings(page_range="not-a-range")

    with pytest.raises(ValueError):
        DocumentPipeline().render_document(document, width_px=200, chunk_height_px=5000)


def test_copies_repeats_rendered_pages(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=50)
    document.settings = PrintSettings(copies=3, margin_top_px=0, margin_bottom_px=0)

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=200)

    assert len(rendered.pages) == 3
    # Same processed content repeated, not re-rendered from scratch each
    # time — cheaper, and there's no reason it would differ anyway.
    assert rendered.pages[0].image is rendered.pages[1].image is rendered.pages[2].image


def test_copies_default_is_a_single_page(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=50)

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=200)

    assert len(rendered.pages) == 1


def test_page_format_half_does_not_crop_short_content(tmp_path: Path) -> None:
    """docs/stage5-ux-plan.md M5.5 postmortem #4 — the real bug: reported
    live as "он обрезан, опять" (a landscape photo got sliced in half). An
    earlier version always cropped the page into a *fixed* 2 pieces at
    full scale, discarding whatever fell outside each band — destructive
    whenever the source isn't already ~2 A5-heights tall (e.g. any single
    photo). Fixed to scale the whole page down to A5's width first (no
    content lost, just shrunk) and only paginate if the *scaled* result is
    actually taller than one A5 page. A short/landscape source must land
    as exactly one page with all its content intact."""
    document = _image_document(tmp_path, width=1000, height=200)  # landscape, like a photo
    document.settings = PrintSettings(
        page_format=PageFormat.HALF, margin_top_px=0, margin_bottom_px=0, dithering=False
    )

    rendered = DocumentPipeline().render_document(document, width_px=1664, chunk_height_px=50000)

    assert len(rendered.pages) == 1


def test_page_format_quarter_does_not_crop_short_content(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=1000, height=200)
    document.settings = PrintSettings(
        page_format=PageFormat.QUARTER, margin_top_px=0, margin_bottom_px=0, dithering=False
    )

    rendered = DocumentPipeline().render_document(document, width_px=1664, chunk_height_px=50000)

    assert len(rendered.pages) == 1


def test_page_format_half_still_paginates_genuinely_tall_content(tmp_path: Path) -> None:
    """The other half of the postmortem #4 fix: content that's actually
    taller than one A5 page (once scaled to A5's width) must still split
    across as many pages as it needs — the fix is "don't force a fixed
    count", not "never split"."""
    document = _image_document(tmp_path, width=1000, height=3000)  # tall
    document.settings = PrintSettings(
        page_format=PageFormat.HALF, margin_top_px=0, margin_bottom_px=0, dithering=False
    )

    rendered = DocumentPipeline().render_document(document, width_px=1664, chunk_height_px=50000)

    assert len(rendered.pages) > 1


def test_page_format_half_target_width_clamped_to_printer_width(tmp_path: Path) -> None:
    """A5's real target width (148mm ~= 1183px at 203dpi) can exceed a
    narrow-roll printer model's own safe content width (e.g. ~384px for
    the A6 hardware line) — clamp to whatever the active printer can
    actually do rather than asking _pad_to_canvas_width to shrink
    something it only ever widens."""
    document = _image_document(tmp_path, width=200, height=280)
    document.settings = PrintSettings(
        page_format=PageFormat.HALF, margin_top_px=0, margin_bottom_px=0, dithering=False
    )

    rendered = DocumentPipeline().render_document(document, width_px=384, chunk_height_px=50000)

    assert rendered.pages[0].image.width == 384


def test_rotation_alone_rotates_the_whole_page_without_splitting(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=50)
    document.settings = PrintSettings(
        rotation_degrees=90, margin_top_px=0, margin_bottom_px=0, dithering=False
    )

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=5000)

    assert len(rendered.pages) == 1
    # 100x50 rotated 90 -> 50x100, re-fit to width_px=100 doubles both axes.
    assert rendered.pages[0].image.width == 100
    assert rendered.pages[0].image.height == 200


def test_page_format_custom_splits_by_explicit_mm_tile_size(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=1000)
    document.settings = PrintSettings(
        page_format=PageFormat.CUSTOM,
        custom_tile_width_mm=25.4,  # -> 203px at PRINT_DPI=203
        custom_tile_height_mm=25.4,
        margin_top_px=0,
        margin_bottom_px=0,
        dithering=False,
    )

    # width_px=300 is wide enough that the custom 203px target isn't
    # clamped (see the dedicated clamp test below) — width 100px scaled up
    # to the printer's 300px, then down to the custom 203px width -> height
    # scales proportionally to 2030px -> split into ceil(2030/203) = 10.
    rendered = DocumentPipeline().render_document(document, width_px=300, chunk_height_px=5000)

    assert len(rendered.pages) == 10


def test_page_format_custom_target_width_clamped_to_printer_width(tmp_path: Path) -> None:
    document = _image_document(tmp_path, width=100, height=1000)
    document.settings = PrintSettings(
        page_format=PageFormat.CUSTOM,
        custom_tile_width_mm=25.4,  # -> 203px, wider than width_px below
        custom_tile_height_mm=25.4,
        margin_top_px=0,
        margin_bottom_px=0,
        dithering=False,
    )

    rendered = DocumentPipeline().render_document(document, width_px=100, chunk_height_px=5000)

    assert rendered.pages[0].image.width == 100


def test_unsupported_kind_raises(tmp_path: Path) -> None:
    source_path = tmp_path / "note.md"
    source_path.write_text("# heading", encoding="utf-8")
    document = DocumentItem(id="x", source_path=str(source_path), kind=DocumentKind.MARKDOWN)

    with pytest.raises(UnsupportedDocumentKindError):
        DocumentPipeline().render_document(document, width_px=384, chunk_height_px=220)


@pytest.mark.parametrize(
    ("filename", "expected_kind"),
    [
        ("photo.png", DocumentKind.IMAGE),
        ("photo.JPG", DocumentKind.IMAGE),
        ("scan.jpeg", DocumentKind.IMAGE),
        ("icon.bmp", DocumentKind.IMAGE),
        ("notes.txt", DocumentKind.TEXT),
        ("report.pdf", DocumentKind.PDF),
        ("readme.md", DocumentKind.MARKDOWN),
    ],
)
def test_detect_document_kind(filename: str, expected_kind: DocumentKind) -> None:
    assert detect_document_kind(f"/some/path/{filename}") == expected_kind


def test_detect_document_kind_unknown_extension() -> None:
    assert detect_document_kind("/some/path/file.xyz") is None
