from pathlib import Path

import fitz

from periprint.infra.renderers.pdf_renderer import PdfRenderer


def _make_pdf(path: Path, page_count: int) -> None:
    document = fitz.open()
    for i in range(page_count):
        page = document.new_page(width=200, height=300)
        page.insert_text((20, 20), f"page {i + 1}")
    document.save(str(path))
    document.close()


def test_render_returns_one_page_per_pdf_page(tmp_path: Path) -> None:
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path, page_count=3)

    pages = PdfRenderer().render(str(pdf_path), width_px=384, fit_mode="fit_width")

    assert len(pages) == 3
    assert all(page.width == 384 for page in pages)


def test_single_page_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "single.pdf"
    _make_pdf(pdf_path, page_count=1)

    pages = PdfRenderer().render(str(pdf_path), width_px=576, fit_mode="fit_width")

    assert len(pages) == 1
    assert pages[0].width == 576


def _make_pdf_with_markers(path: Path, page_count: int) -> None:
    """Each page gets a black rectangle whose width encodes its own
    (0-based) index, so a test can identify which original page a
    rendered image actually came from — not just count how many came
    back."""
    document = fitz.open()
    for i in range(page_count):
        page = document.new_page(width=200, height=300)
        page.draw_rect(fitz.Rect(0, 0, 10 + i * 10, 20), fill=(0, 0, 0))
    document.save(str(path))
    document.close()


def _marker_width_px(image) -> int:
    import PIL.ImageChops

    bbox = PIL.ImageChops.invert(image.convert("L")).getbbox()
    assert bbox is not None
    left, _top, right, _bottom = bbox
    return right - left


def test_page_indices_selects_only_requested_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "marked.pdf"
    _make_pdf_with_markers(pdf_path, page_count=5)
    renderer = PdfRenderer()

    # Ground truth: each page's marker width from a full, unfiltered
    # render — avoids reimplementing PdfRenderer's own scaling math in
    # the test just to predict an expected pixel width.
    all_pages = renderer.render(str(pdf_path), width_px=384, fit_mode="fit_width")
    expected_widths = [_marker_width_px(all_pages[i]) for i in (0, 2, 4)]
    assert len(set(expected_widths)) == 3  # sanity: markers are actually distinguishable

    selected = renderer.render(
        str(pdf_path), width_px=384, fit_mode="fit_width", page_indices=[0, 2, 4]
    )

    assert len(selected) == 3
    assert [_marker_width_px(page) for page in selected] == expected_widths
