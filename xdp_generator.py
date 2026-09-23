"""
Deterministic XDP (XFA) layout generator. Not LLM-driven -- it takes an
already-decided field-to-position mapping (assembled by the calling agent
from parse_fsd + parse_pdf_layout output) and renders well-formed XFA,
following the same structure as the hand-authored ZTRM_LG_F01 reference
form: a subform per page, static <draw> labels, <field> elements bound via
`bind match="dataRef"`, plus (new) a repeating item-table subform for
header+items forms.

Coordinate convention: millimetres, top-left origin -- same as the
reference form and as pdf_layout_parser.py's output, so positions from
parse_pdf_layout can be passed straight through without conversion.

Row-name assumption for item tables: SAP's interface builder names each
repeating row of a TABLE parameter "ITEM" in the generated XFA data schema
(e.g. $record.GT_ITEMS.ITEM). This is the common convention but isn't
guaranteed for every NetWeaver release -- after bootstrap creates the
interface, open it once in SFP's Context tab to confirm the actual schema,
and pass a different `data_ref` here if it differs. Fields inside the
repeating row bind with a relative ref ($.FIELDNAME), which is standard
XFA and unaffected by the row-name assumption.
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["xml", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

DEFAULT_FONT = "Arial"
DEFAULT_FONT_SIZE = 11
DEFAULT_ALIGN_RTL = "right"
DEFAULT_ALIGN_LTR = "left"


@dataclass
class Element:
    kind: str            # 'draw' (static label) or 'field' (data-bound)
    name: str
    x: float
    y: float
    w: float
    h: float
    text: str = ""              # for kind='draw'
    data_ref: str = ""          # for kind='field', e.g. "$record.IV_BANK_NAME" or "$record.GS_HEADER.IV_BANK_NAME"
    align: Optional[str] = None
    font: str = DEFAULT_FONT
    font_size: float = DEFAULT_FONT_SIZE
    bold: bool = False
    underline: bool = False
    locale: Optional[str] = None


@dataclass
class ItemColumn:
    name: str          # field name inside the item row structure
    label: str         # column header text
    w: float
    align: Optional[str] = None


@dataclass
class ItemsTable:
    name: str                  # subform name, e.g. "Items"
    x: float
    y: float
    w: float
    columns: list
    data_ref: str = "$record.GT_ITEMS.ITEM"   # see row-name assumption above
    header_h: float = 8
    row_h: float = 7
    locale: Optional[str] = None


@dataclass
class Page:
    width_mm: float = 210
    height_mm: float = 297
    elements: list = field(default_factory=list)
    items_table: Optional[ItemsTable] = None
    locale: Optional[str] = None


def _resolve_align(explicit, locale, default_ltr=DEFAULT_ALIGN_LTR, default_rtl=DEFAULT_ALIGN_RTL):
    if explicit:
        return explicit
    if locale and locale.split("_")[0].lower() in ("ar", "he", "fa", "ur"):
        return default_rtl
    return default_ltr


def generate_xdp(form_name: str, pages: list, locale: str = "en_US") -> bytes:
    """
    pages: list of Page (or equivalent plain dicts with the same shape).
    Returns the XDP document as UTF-8 encoded bytes, ready to hand to
    deploy_form (which base64-encodes it for the SOAP-RFC call).
    """
    rendered_pages = []
    for p in pages:
        p = p if isinstance(p, Page) else Page(**p)
        page_locale = p.locale or locale
        content_w = p.width_mm - 20  # 10mm margin each side, matches reference form
        content_h = p.height_mm - 20
        is_a4 = abs(p.width_mm - 210) < 1 and abs(p.height_mm - 297) < 1

        rendered_elements = []
        for el in p.elements:
            el = el if isinstance(el, Element) else Element(**el)
            align = _resolve_align(el.align, page_locale)
            rendered_elements.append({
                "kind": el.kind, "name": el.name,
                "x": el.x, "y": el.y, "w": el.w, "h": el.h,
                "text": el.text, "data_ref": el.data_ref,
                "align": align, "font": el.font, "font_size": el.font_size,
                "bold": el.bold, "underline": el.underline,
                "locale": el.locale or page_locale,
            })

        items_table = None
        if p.items_table:
            it = p.items_table if isinstance(p.items_table, ItemsTable) else ItemsTable(**p.items_table)
            cols = []
            for c in it.columns:
                c = c if isinstance(c, ItemColumn) else ItemColumn(**c)
                cols.append({"name": c.name, "label": c.label, "w": c.w,
                             "align": _resolve_align(c.align, page_locale, "left", "right")})
            items_table = {
                "name": it.name, "x": it.x, "y": it.y, "w": it.w,
                "columns": cols, "data_ref": it.data_ref,
                "header_h": it.header_h, "row_h": it.row_h,
                "locale": it.locale or page_locale,
            }

        rendered_pages.append({
            "content_w": content_w, "content_h": content_h,
            "stock": "a4" if is_a4 else "custom",
            "short": min(p.width_mm, p.height_mm), "long": max(p.width_mm, p.height_mm),
            "locale": page_locale,
            "elements": rendered_elements,
            "items_table": items_table,
        })

    if not any(pg["elements"] or pg["items_table"] for pg in rendered_pages):
        raise ValueError("generate_xdp: no elements or items_table on any page -- nothing to render.")

    template = _env.get_template("xdp_template.xml.j2")
    xml_text = template.render(
        timestamp=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        locale=locale,
        pages=rendered_pages,
    )
    return xml_text.encode("utf-8")


def save_xdp(xdp_bytes: bytes, out_path: str) -> str:
    with open(out_path, "wb") as f:
        f.write(xdp_bytes)
    return out_path
