"""
parse_pdf_layout: extracts text blocks with precise positions from a
layout-reference PDF, converted into the same coordinate convention the
XDP generator and the reference form use: millimetres, top-left origin.
PyMuPDF's native coordinates are in points (1/72"), top-left origin
already for `get_text("dict")` -- only the pt->mm unit conversion is
needed, not an axis flip.

This is the raw material for the field<->position mapping step the
calling agent performs; it does not itself decide which block is a label
vs. a blank-to-be-filled -- see README's "how field<->position mapping
works" section.
"""
from __future__ import annotations

PT_TO_MM = 25.4 / 72.0

ARABIC_RANGES = (
    (0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF), (0xFE70, 0xFEFF),
)
HEBREW_RANGE = (0x0590, 0x05FF)


def _direction_of(text: str) -> str:
    rtl_count = 0
    ltr_count = 0
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in ARABIC_RANGES) or (HEBREW_RANGE[0] <= cp <= HEBREW_RANGE[1]):
            rtl_count += 1
        elif ch.isalpha():
            ltr_count += 1
    if rtl_count == 0 and ltr_count == 0:
        return "ltr"
    return "rtl" if rtl_count > ltr_count else "ltr"


def parse_pdf_layout(path: str) -> dict:
    """
    Returns {"pages": [{"index", "width_mm", "height_mm", "blocks": [...]}]}.
    Each block: {"text","x","y","w","h","direction","font_size"} in mm,
    top-left origin, one entry per line (words on the same visual line are
    merged) so labels read as a single unit rather than one block per word.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    pages = []
    for page_index, page in enumerate(doc):
        rect = page.rect
        width_mm = rect.width * PT_TO_MM
        height_mm = rect.height * PT_TO_MM

        raw = page.get_text("dict")
        blocks = []
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue
                bbox = line.get("bbox")
                x0, y0, x1, y1 = bbox
                font_size = spans[0].get("size", 10)
                blocks.append({
                    "text": text,
                    "x": round(x0 * PT_TO_MM, 2),
                    "y": round(y0 * PT_TO_MM, 2),
                    "w": round((x1 - x0) * PT_TO_MM, 2),
                    "h": round((y1 - y0) * PT_TO_MM, 2),
                    "direction": _direction_of(text),
                    "font_size": round(font_size * PT_TO_MM * (72 / 25.4), 1),  # keep in pt, more familiar for font sizing
                })

        pages.append({
            "index": page_index,
            "width_mm": round(width_mm, 2),
            "height_mm": round(height_mm, 2),
            "blocks": blocks,
        })

    doc.close()
    return {"pages": pages}
