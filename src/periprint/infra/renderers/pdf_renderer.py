import fitz  # PyMuPDF
import PIL.Image

from periprint.infra.renderers.base import fit_to_width

# Matches the 203dpi Peripage hardware named in the spec/hardware notes.
_RENDER_DPI = 203


class PdfRenderer:
    """One image per PDF page — printed with an explicit page break between
    them (DocumentPipeline/PrintJobManager's job, not this renderer's),
    per the spec's "постранично" requirement."""

    def render(
        self,
        source_path: str,
        width_px: int,
        fit_mode: str = "fit_width",
        page_indices: list[int] | None = None,
    ) -> list[PIL.Image.Image]:
        zoom = _RENDER_DPI / 72  # PDF points are defined as 1/72 inch
        matrix = fitz.Matrix(zoom, zoom)

        pages: list[PIL.Image.Image] = []
        with fitz.open(source_path) as document:
            # Indexing document[i] directly, not iterating the whole
            # document and skipping unwanted pages — the actual point of
            # page_indices is to avoid rasterizing pages nobody asked for.
            indices = page_indices if page_indices is not None else range(len(document))
            for i in indices:
                page = document[i]
                pixmap = page.get_pixmap(matrix=matrix)
                image = PIL.Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                pages.append(fit_to_width(image, width_px, fit_mode))
        return pages
