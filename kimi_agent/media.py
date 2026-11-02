"""Helpers for images and documents shared by several tools."""

from __future__ import annotations

import base64
import io

from kimi_agent.tools.base import Image, ToolError

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_SIDE = 2048


def prepare_image(data: bytes, mime: str) -> tuple[Image, str]:
    """Return an Image suitable for the model plus a human description (dimensions).

    Large images are downscaled when Pillow is installed.
    """
    try:
        from PIL import Image as PILImage  # type: ignore[import-not-found]
    except ImportError:
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError(
                f"Image is too large ({len(data):,} bytes). Install Pillow to enable automatic downscaling."
            ) from None
        return Image(base64.b64encode(data).decode("ascii"), mime), f"{len(data):,} bytes"

    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ToolError(f"Could not decode image: {exc}") from exc
    width, height = img.size
    needs_resize = max(width, height) > MAX_IMAGE_SIDE
    needs_convert = mime not in ("image/png", "image/jpeg", "image/webp", "image/gif")
    if not needs_resize and not needs_convert and len(data) <= MAX_IMAGE_BYTES:
        return Image(base64.b64encode(data).decode("ascii"), mime), f"{width}x{height}"
    if needs_resize:
        scale = MAX_IMAGE_SIDE / max(width, height)
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    buf = io.BytesIO()
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    img.save(buf, format="PNG", optimize=True)
    out = buf.getvalue()
    if len(out) > MAX_IMAGE_BYTES:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return Image(base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"), (
            f"{width}x{height}, resized to {img.size[0]}x{img.size[1]}"
        )
    desc = f"{width}x{height}" + (f", resized to {img.size[0]}x{img.size[1]}" if needs_resize else "")
    return Image(base64.b64encode(out).decode("ascii"), "image/png"), desc


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        raise ToolError(
            "Reading PDFs requires pypdf: pip install 'kimi-agent[docs]'. "
            "Alternatively use run_command with `pdftotext file.pdf -`."
        ) from None
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [f"--- page {i} ---\n{(page.extract_text() or '').strip()}" for i, page in enumerate(reader.pages, 1)]
    except Exception as exc:
        raise ToolError(f"Could not parse PDF: {exc}") from exc
    return "\n\n".join(pages) if pages else "(PDF has no extractable text)"
