"""DOCX/PPTX → per-page PNG via the Collabora editor sidecar + PyMuPDF.

The pipeline mirrors ai/agent's editor_session.do_view() (kept around so
when the editor swap happens the document path is identical):

  1. POST the DOCX/PPTX bytes to `http://editor:9980/cool/convert-to/pdf`
     — Collabora returns a single multi-page PDF.
  2. PyMuPDF (fitz) opens the PDF in-memory and rasterizes each page to
     a PNG at DPI controlled by `PREVIEW_DPI` (default 120).
  3. PNGs are written next to the artifact as `page-01.png`, `page-02.png`,
     ... so the analyst surface (and the developer doing visual iteration)
     can diff against `preview.png` (the TinyMCE iframe render) to catch
     HTML/DOCX drift in a single glance.

Collabora's `/cool/convert-to/pdf` endpoint runs entirely inside the
sidecar (no LibreOffice install in docker-api). PyMuPDF is a pure-Python
wheel — no system deps in docker-api either.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_EDITOR_HOST = os.environ.get("EDITOR_HOST", "editor:9980")
_EDITOR_BASE = f"http://{_EDITOR_HOST}"
_PREVIEW_DPI = int(os.environ.get("PREVIEW_DPI", "120"))
# Max wall-clock for the convert-to/pdf hop. Collabora can take 5-10s on
# a cold first call; 30s leaves headroom without parking the artifact
# emit indefinitely if the sidecar is down.
_CONVERT_TIMEOUT_S = float(os.environ.get("EDITOR_CONVERT_TIMEOUT_S", "30"))


def render_doc_to_pages(src_path: str | Path, out_dir: str | Path,
                         filename_stem: str = "page",
                         dpi: Optional[int] = None) -> list[Path]:
    """Convert one DOCX/PPTX file at `src_path` into N PNG files in `out_dir`.

    Returns the list of PNG paths written (one per page, ordered). Empty
    list on failure — callers should treat this as best-effort and not
    block the artifact emit if it returns nothing.
    """
    src = Path(src_path)
    if not src.exists():
        logger.warning("render_doc_to_pages: source missing: %s", src)
        return []

    dpi = dpi or _PREVIEW_DPI
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. POST the file to Collabora's convert-to/pdf endpoint. Multipart
    # form, field name `data`, filename echoed back in the form so the
    # converter picks the right input filter.
    try:
        with src.open("rb") as f:
            files = {"data": (src.name, f, "application/octet-stream")}
            with httpx.Client(timeout=_CONVERT_TIMEOUT_S) as client:
                resp = client.post(f"{_EDITOR_BASE}/cool/convert-to/pdf",
                                   files=files)
        if resp.status_code != 200:
            logger.warning(
                "render_doc_to_pages: Collabora convert-to/pdf failed "
                "for %s: HTTP %s — %s",
                src.name, resp.status_code, resp.text[:200],
            )
            return []
        pdf_bytes = resp.content
        if not pdf_bytes:
            logger.warning("render_doc_to_pages: Collabora returned empty PDF for %s", src.name)
            return []
    except Exception as e:
        logger.warning("render_doc_to_pages: Collabora call failed for %s: %s", src.name, e)
        return []

    # 2. PyMuPDF rasterize. Lazy import so the import path still works
    # in environments where pymupdf isn't installed yet (e.g. tests).
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("render_doc_to_pages: pymupdf not installed; skipping rasterize")
        return []

    out_paths: list[Path] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            for idx, page in enumerate(doc, start=1):
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                out_path = out_dir / f"{filename_stem}-{idx:02d}.png"
                pix.save(str(out_path))
                out_paths.append(out_path)
        finally:
            doc.close()
    except Exception as e:
        logger.warning("render_doc_to_pages: fitz rasterize failed for %s: %s", src.name, e)
        return []

    return out_paths


def render_doc_to_pages_safe(src_path: str | Path, out_dir: str | Path,
                              filename_stem: str = "page") -> list[Path]:
    """Wrapper that never raises. Returns [] on any error.

    Use this from artifact-render hot paths so a Collabora outage never
    blocks the analyst's artifact emit.
    """
    try:
        return render_doc_to_pages(src_path, out_dir, filename_stem=filename_stem)
    except Exception as e:
        logger.warning("render_doc_to_pages_safe: swallowed %s", e)
        return []
