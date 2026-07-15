import PIL.Image

from periprint.infra.renderers.base import fit_to_width


class ImageRenderer:
    def render(
        self,
        source_path: str,
        width_px: int,
        fit_mode: str = "fit_width",
        page_indices: list[int] | None = None,  # always exactly 1 page, ignored
    ) -> list[PIL.Image.Image]:
        with PIL.Image.open(source_path) as source:
            source.load()
            return [fit_to_width(source, width_px, fit_mode)]
