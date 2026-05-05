from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
import pymupdf4llm

MAX_MARKDOWN_CHARS = int(os.getenv("MAX_MARKDOWN_CHARS", "60000"))
PDF_OCR_MAX_PAGES = int(os.getenv("PDF_OCR_MAX_PAGES", "40"))
PDF_OCR_DPI = int(os.getenv("PDF_OCR_DPI", "180"))
# auto = OCR only when text extraction is weak; never = disable; always = always compare with OCR
PDF_OCR_MODE = os.getenv("PDF_OCR_MODE", "auto").strip().lower()
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "eng")


class PDFExtractionError(Exception):
    """Raised when local PDF → Markdown conversion fails."""


class OCRNotAvailableError(Exception):
    """Tesseract / pytesseract missing or not on PATH."""


def _truncate(md: str) -> str:
    if len(md) > MAX_MARKDOWN_CHARS:
        md = md[:MAX_MARKDOWN_CHARS]
        md += "\n\n[DECK TRUNCATED — exceeded character limit]"
    return md


def _unique_word_count(text: str) -> int:
    words = re.findall(r"[\w']+", text.lower())
    return len(set(words))


def _ocr_pdf_pages(pdf_bytes: bytes) -> str:
    """Render each page to a bitmap and run Tesseract OCR."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        raise OCRNotAvailableError(
            "Install packages: pip install pytesseract Pillow. "
            "macOS: brew install tesseract (optional: brew install tesseract-lang)."
        ) from e

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts: list[str] = []
    try:
        n = min(doc.page_count, PDF_OCR_MAX_PAGES)
        for i in range(n):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=PDF_OCR_DPI)
            img_bytes = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_bytes))
            try:
                text = pytesseract.image_to_string(img, lang=TESSERACT_LANG)
            except pytesseract.TesseractNotFoundError as e:
                raise OCRNotAvailableError(
                    "Tesseract not found on PATH. macOS: brew install tesseract "
                    "(optionally set TESSDATA_PREFIX)."
                ) from e
            if text and text.strip():
                parts.append(f"### Slide / page {i + 1}\n\n{text.strip()}")
    finally:
        doc.close()

    return "\n\n".join(parts)


def pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    """
    First pymupdf4llm (fast; preserves structure where a text layer exists).
    If output is empty / suspiciously repetitive (common for image-only decks),
    or PDF_OCR_MODE=always, run OCR (render pages + Tesseract).
    """
    tmp = Path("/tmp/fund_deck.pdf")
    tmp.write_bytes(pdf_bytes)
    try:
        primary = pymupdf4llm.to_markdown(str(tmp))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise PDFExtractionError(str(e)) from e
    tmp.unlink(missing_ok=True)

    ocr_disabled = os.getenv("PDF_OCR_DISABLE", "").lower() in ("1", "true", "yes")
    quality_issue = assess_deck_markdown_quality(primary) is not None
    want_ocr = not ocr_disabled and PDF_OCR_MODE != "never" and (
        PDF_OCR_MODE == "always" or quality_issue
    )

    final = primary
    if want_ocr:
        try:
            ocr_text = _ocr_pdf_pages(pdf_bytes)
        except OCRNotAvailableError as e:
            ocr_text = ""
            if quality_issue or PDF_OCR_MODE == "always":
                print(f"[Fund PDF] OCR unavailable: {e}", file=sys.stderr)

        if ocr_text.strip():
            if PDF_OCR_MODE == "always" and not quality_issue:
                # Oba przebiegi — wybierz bogatszy w unikalne słowa
                if _unique_word_count(ocr_text) > _unique_word_count(primary) * 0.85:
                    final = (
                        "## Deck content from OCR (slides rendered as images)\n\n"
                        + ocr_text
                    )
                else:
                    final = primary
            else:
                # Słaba ekstrakcja tekstowa — preferuj OCR
                final = (
                    "## Deck content from OCR (text visible on slide images; pymupdf4llm had weak signal)\n\n"
                    + ocr_text
                )
        elif quality_issue:
            # Bez Tesseract zostaje słaby primary; nie rzucamy wyjątku
            final = (
                primary
                + "\n\n[OCR: not run or no text — install Tesseract; see README / PDF_OCR_*]\n"
            )

    return _truncate(final)


def assess_deck_markdown_quality(md: str) -> str | None:
    """
    Return a short user-facing warning if extracted text is empty, tiny, or highly repetitive
    (common for image-only / scanned decks). None if OK-ish.
    """
    if not md or not md.strip():
        return (
            "PDF extraction is empty — Gate 2 sees no deck content (often scanned PDF or image-only). "
            "Numeric scores are not meaningful."
        )
    lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
    if len(lines) < 3 and len(md) < 400:
        return (
            "Very little text extracted from the PDF — scores may be arbitrarily low. "
            "Check logs/*_extracted.md and consider a text-layer PDF or OCR."
        )
    unique = len(set(lines))
    if len(lines) >= 12 and unique <= 3:
        return (
            "PDF text is mostly repetition (e.g. header/footer only) — common for image-heavy decks. "
            "The model sees almost no substance from slides; Gate 2 output may be useless."
        )
    return None


def build_pdf_content_block(pdf_bytes: bytes) -> dict:
    """Return a text content block with extracted Markdown for OpenAI."""
    md = pdf_bytes_to_markdown(pdf_bytes)
    char_count = len(md)
    approx_tokens = char_count // 4
    return {
        "type": "text",
        "text": (
            f"\n\n--- PITCH DECK (extracted as Markdown) ---\n"
            f"[{char_count:,} chars, ~{approx_tokens:,} tokens]\n\n"
            f"{md}\n\n--- END OF DECK ---"
        ),
    }
