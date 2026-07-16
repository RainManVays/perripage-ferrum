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


def rotate_page(image: PIL.Image.Image, degrees: int) -> PIL.Image.Image:
    """0/90/180/270 only — always comes from a fixed UI dropdown (see
    PrintSettings.rotation_degrees), never free-form input, so no other
    angle can reach here. Uses .transpose() (exact pixel remap), not
    .rotate() (anti-aliases edges — irrelevant for content that's about to
    be dithered/thresholded in normalize_to_1bit anyway, but transpose is
    also just cheaper for exact 90-degree steps)."""
    if degrees == 0:
        return image
    transpose_by_degrees = {
        90: PIL.Image.Transpose.ROTATE_90,
        180: PIL.Image.Transpose.ROTATE_180,
        270: PIL.Image.Transpose.ROTATE_270,
    }
    return image.transpose(transpose_by_degrees[degrees])


def split_into_tiles(
    image: PIL.Image.Image, tile_count: int, *, rotate_each: bool
) -> list[PIL.Image.Image]:
    """Equal-height horizontal bands, top to bottom — docs/stage5-ux-plan.md
    M5.5 imposition (e.g. one "A4"-equivalent page split into 2/4 pieces).
    Band order matches the physical cut order a continuous roll naturally
    supports (PrintJobManager already inserts a printBreak() between
    consecutive RenderedPage entries — a visible gap to cut at).

    rotate_each additionally rotates each band 90° — so that once a piece
    is physically cut off and picked up rotated upright by the user, its
    content reads the right way. NOT VERIFIED AGAINST REAL HARDWARE: which
    way (CW vs CCW) is a judgment call made without a physical print+cut+
    read-in-hand test. If it comes out backwards, flip ROTATE_90 to
    ROTATE_270 right below — nothing else needs to change."""
    if tile_count <= 1:
        return [image]
    tile_height = max(1, -(-image.height // tile_count))  # ceil division
    tiles = [
        image.crop((0, top, image.width, min(top + tile_height, image.height)))
        for top in range(0, image.height, tile_height)
    ]
    if rotate_each:
        tiles = [tile.transpose(PIL.Image.Transpose.ROTATE_90) for tile in tiles]
    return tiles


def split_into_grid(
    image: PIL.Image.Image, cols: int, rows: int, *, rotate_each: bool
) -> list[PIL.Image.Image]:
    """2-D grid imposition (docs/stage5-ux-plan.md M5.5 postmortem #3) —
    unlike split_into_tiles' 1-D horizontal bands, this cuts both axes at
    once. Confirmed against a hand-drawn packing diagram, not guessed: a
    plain 2x2 grid of an *unrotated* "A4"-equivalent page (210x297mm)
    already lands almost exactly on real A6 proportions per cell
    (105x148.5mm vs real A6's 105x148mm) with NO rotation needed at all —
    unlike a 1x2 *row* split (HALF/"A5"), whose bands are landscape-shaped
    and do need a 90° rotation each to reach A5's portrait shape. Cells
    are ordered row-major (top-left, top-right, then next row) — the
    natural reading order for a grid, each pair separated by the same
    printBreak() between RenderedPage entries as any other imposition."""
    cell_width = max(1, -(-image.width // cols))  # ceil division
    cell_height = max(1, -(-image.height // rows))
    tiles = []
    for row in range(rows):
        for col in range(cols):
            left = col * cell_width
            top = row * cell_height
            right = min(left + cell_width, image.width)
            bottom = min(top + cell_height, image.height)
            tile = image.crop((left, top, right, bottom))
            if rotate_each:
                tile = tile.transpose(PIL.Image.Transpose.ROTATE_90)
            tiles.append(tile)
    return tiles


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
