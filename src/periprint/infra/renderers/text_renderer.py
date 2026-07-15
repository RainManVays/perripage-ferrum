import textwrap
from pathlib import Path

import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)
_FONT_SIZE_PX = 24
_LINE_SPACING_PX = 6
_MARGIN_PX = 12


def _load_monospace_font() -> PIL.ImageFont.ImageFont | PIL.ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return PIL.ImageFont.truetype(path, _FONT_SIZE_PX)
    return PIL.ImageFont.load_default(_FONT_SIZE_PX)


class TextRenderer:
    """Simple monospace renderer — no markdown/rich-text support (that's
    MarkdownRenderer, Stage 6/P1). Wraps to the printer's character width so
    lines never overflow the printable area."""

    def render(
        self,
        source_path: str,
        width_px: int,
        fit_mode: str = "fit_width",
        page_indices: list[int] | None = None,  # always exactly 1 page, ignored
    ) -> list[PIL.Image.Image]:
        text = Path(source_path).read_text(encoding="utf-8", errors="replace")
        font = _load_monospace_font()

        char_width = font.getlength("0") or 1
        usable_width = max(1, width_px - 2 * _MARGIN_PX)
        max_chars = max(1, int(usable_width / char_width))

        wrapped_lines: list[str] = []
        for raw_line in text.splitlines() or [""]:
            if raw_line == "":
                wrapped_lines.append("")
            else:
                wrapped_lines.extend(textwrap.wrap(raw_line, width=max_chars) or [""])

        line_height = _FONT_SIZE_PX + _LINE_SPACING_PX
        height = _MARGIN_PX * 2 + line_height * len(wrapped_lines)

        image = PIL.Image.new("L", (width_px, height), color=255)
        draw = PIL.ImageDraw.Draw(image)
        y = _MARGIN_PX
        for line in wrapped_lines:
            draw.text((_MARGIN_PX, y), line, fill=0, font=font)
            y += line_height

        return [image]
