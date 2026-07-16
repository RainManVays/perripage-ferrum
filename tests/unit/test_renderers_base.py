import PIL.Image
import PIL.ImageDraw

from periprint.infra.renderers.base import (
    fit_to_width,
    normalize_to_1bit,
    rotate_page,
    split_into_tiles,
    trim_to_content_height,
)


def _image(width: int, height: int = 50, color: int = 128) -> PIL.Image.Image:
    return PIL.Image.new("L", (width, height), color=color)


def test_fit_width_scales_proportionally() -> None:
    result = fit_to_width(_image(width=200, height=100), width_px=100, fit_mode="fit_width")

    assert result.width == 100
    assert result.height == 50


def test_fit_width_no_op_when_already_target_width() -> None:
    source = _image(width=100, height=40)

    result = fit_to_width(source, width_px=100, fit_mode="fit_width")

    assert result.width == 100
    assert result.height == 40


def test_actual_size_pads_narrower_image() -> None:
    result = fit_to_width(_image(width=50, height=30), width_px=100, fit_mode="actual_size")

    assert result.width == 100
    assert result.height == 30


def test_actual_size_crops_wider_image() -> None:
    result = fit_to_width(_image(width=200, height=30), width_px=100, fit_mode="actual_size")

    assert result.width == 100
    assert result.height == 30


def test_crop_mode_center_crops_wider_image() -> None:
    result = fit_to_width(_image(width=300, height=30), width_px=100, fit_mode="crop")

    assert result.width == 100


def test_normalize_to_1bit_with_dithering() -> None:
    image = PIL.Image.new("L", (10, 10), color=128)

    result = normalize_to_1bit(image, dithering=True)

    assert result.mode == "1"
    assert result.size == (10, 10)


def test_normalize_to_1bit_without_dithering_uses_threshold() -> None:
    image = PIL.Image.new("L", (10, 10), color=200)

    result = normalize_to_1bit(image, dithering=False)

    assert result.mode == "1"
    # Above mid-gray threshold with no dithering -> every pixel white (255/on).
    assert result.getpixel((0, 0)) != 0


def test_trim_to_content_height_crops_blank_tail() -> None:
    # A "page" with content only in a band near the top, like a short PDF
    # page rendered at full A4 height (periprint-spec.md §3 P1 "по длине
    # контента" mode).
    image = PIL.Image.new("L", (100, 1000), color=255)
    draw = PIL.ImageDraw.Draw(image)
    draw.rectangle([10, 20, 90, 60], fill=0)

    result = trim_to_content_height(image)

    assert result.width == 100
    # rectangle([10, 20, 90, 60]) draws rows 20-60 inclusive (41 rows);
    # getbbox()'s lower bound is exclusive, so the crop is [20, 61).
    assert result.height == 41
    assert result.getpixel((50, 10)) == 0  # was row 30 in the original


def test_trim_to_content_height_leaves_full_page_untouched() -> None:
    image = PIL.Image.new("L", (100, 1000), color=255)
    draw = PIL.ImageDraw.Draw(image)
    draw.rectangle([10, 20, 90, 60], fill=0)

    # full_page mode never calls trim_to_content_height at all — this just
    # confirms fit_to_width alone (the other axis) doesn't accidentally
    # crop height on its own.
    result = fit_to_width(image, width_px=100, fit_mode="fit_width")

    assert result.height == 1000


def test_trim_to_content_height_blank_page_is_untouched() -> None:
    blank = PIL.Image.new("L", (100, 500), color=255)

    result = trim_to_content_height(blank)

    assert result.height == 500
    assert result.width == 100


def test_rotate_page_zero_degrees_is_a_no_op() -> None:
    source = _image(width=100, height=50)

    result = rotate_page(source, 0)

    assert result is source


def test_rotate_page_90_and_270_swap_dimensions() -> None:
    source = _image(width=100, height=50)

    assert rotate_page(source, 90).size == (50, 100)
    assert rotate_page(source, 270).size == (50, 100)


def test_rotate_page_180_keeps_dimensions_but_flips_content() -> None:
    image = PIL.Image.new("L", (10, 20), color=255)
    image.putpixel((0, 0), 0)  # mark the top-left corner

    result = rotate_page(image, 180)

    assert result.size == (10, 20)
    assert result.getpixel((0, 0)) != 0  # was white
    assert result.getpixel((9, 19)) == 0  # marker moved to the opposite corner


def test_split_into_tiles_single_tile_is_a_no_op() -> None:
    source = _image(width=100, height=50)

    assert split_into_tiles(source, 1) == [source]


def test_split_into_tiles_splits_into_equal_height_bands() -> None:
    source = _image(width=100, height=200)

    tiles = split_into_tiles(source, 4)

    assert len(tiles) == 4
    assert all(tile.width == 100 for tile in tiles)
    assert sum(tile.height for tile in tiles) == 200


def test_split_into_tiles_handles_uneven_division() -> None:
    source = _image(width=100, height=100)

    tiles = split_into_tiles(source, 3)

    assert len(tiles) == 3
    assert sum(tile.height for tile in tiles) == 100
    # ceil(100/3) = 34 -> bands of 34, 34, 32
    assert [tile.height for tile in tiles] == [34, 34, 32]
