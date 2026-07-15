from __future__ import annotations

from typing import Protocol

import PIL.Image
import PIL.ImageChops


class Renderer(Protocol):
    def render(
        self,
        source_path: str,
        width_px: int,
        fit_mode: str = "fit_width",
        page_indices: list[int] | None = None,
    ) -> list[PIL.Image.Image]:
        """One image per logical page (PdfRenderer may return several;
        everything else returns exactly one), already fit to width_px.
        page_indices (0-based, see utils/page_range.py) restricts which
        pages get rasterized at all — None means every page. Renderers
        that only ever produce a single page (image/text) can ignore it;
        PdfRenderer uses it to skip pages instead of throwing away
        already-rendered rasters (periprint-spec.md §11 memory NFR)."""
        ...


def fit_to_width(image: PIL.Image.Image, width_px: int, fit_mode: str) -> PIL.Image.Image:
    """fit_mode: fit_width | actual_size | crop. Output is always exactly
    width_px wide, scaled/padded/cropped as needed."""
    if fit_mode == "fit_width":
        if image.width == width_px:
            return image
        new_height = max(1, round(image.height * (width_px / image.width)))
        return image.resize((width_px, new_height), PIL.Image.Resampling.LANCZOS)

    # actual_size / crop: preserve native scale. A source narrower than the
    # printer gets centered on a white canvas; one wider gets center-cropped
    # (printers can't physically print past their own width).
    if image.width <= width_px:
        if image.width == width_px:
            return image
        canvas = PIL.Image.new(image.mode, (width_px, image.height), color=255)
        canvas.paste(image, ((width_px - image.width) // 2, 0))
        return canvas

    left = (image.width - width_px) // 2
    return image.crop((left, 0, left + width_px, image.height))


def trim_to_content_height(image: PIL.Image.Image) -> PIL.Image.Image:
    """"По длине контента" page mode (periprint-spec.md §3 P1): crops
    trailing/leading blank vertical space from a full-page raster, so a
    page mostly empty (e.g. a short PDF page rendered at full A4 height)
    doesn't waste paper on blank tape. Only trims height, never width —
    the spec is explicit this is a vertical crop, not a horizontal one;
    "целиком по формату" (full_page) keeps the untrimmed page instead.
    A fully blank page is left untouched: there's nothing meaningful to
    trim to, and collapsing it to 0 height would silently drop it from a
    multi-page document instead of printing a visibly blank page."""
    grayscale = image.convert("L")
    # Background is white (255); invert so content (anything non-white)
    # becomes the non-zero region getbbox() looks for.
    bbox = PIL.ImageChops.invert(grayscale).getbbox()
    if bbox is None:
        return image
    _left, top, _right, bottom = bbox
    return image.crop((0, top, image.width, bottom))


def normalize_to_1bit(image: PIL.Image.Image, dithering: bool) -> PIL.Image.Image:
    """Thermal printers are binary (dot fires or it doesn't) — this is the
    one normalization every renderer's output must go through before
    chunking. dithering=True uses PIL's default Floyd-Steinberg for mode
    '1'; False uses a flat threshold."""
    grayscale = image.convert("L")
    if dithering:
        return grayscale.convert("1")
    return grayscale.convert("1", dither=PIL.Image.Dither.NONE)


def slice_into_chunks(image: PIL.Image.Image, chunk_height_px: int) -> list[PIL.Image.Image]:
    """Vertical slices of exactly chunk_height_px (last one may be shorter)
    — the unit PrintJobManager sends/retries individually (Stage 4)."""
    if chunk_height_px <= 0:
        raise ValueError("chunk_height_px must be positive")
    height = image.height
    if height == 0:
        return []
    return [
        image.crop((0, top, image.width, min(top + chunk_height_px, height)))
        for top in range(0, height, chunk_height_px)
    ]
