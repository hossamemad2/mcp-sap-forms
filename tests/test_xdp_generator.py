import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lxml import etree
import xdp_generator as xg


def test_flat_form_is_well_formed_xml():
    xdp = xg.generate_xdp(
        form_name="ZFLAT",
        locale="ar_EG",
        pages=[{
            "width_mm": 210, "height_mm": 297,
            "elements": [
                {"kind": "draw", "name": "D1", "x": 10, "y": 10, "w": 50, "h": 8, "text": "Label"},
                {"kind": "field", "name": "IV_NAME", "x": 70, "y": 10, "w": 50, "h": 8,
                 "data_ref": "$record.IV_NAME"},
            ],
        }],
    )
    root = etree.fromstring(xdp)  # raises if not well-formed
    assert root.tag.endswith("xdp")
    fields = root.findall(".//{http://www.xfa.org/schema/xfa-template/3.3/}field")
    assert len(fields) == 1
    assert fields[0].get("name") == "IV_NAME"


def test_header_items_form_has_repeating_row_and_bind():
    xdp = xg.generate_xdp(
        form_name="ZSO_CONF",
        locale="en_US",
        pages=[{
            "width_mm": 210, "height_mm": 297,
            "elements": [
                {"kind": "field", "name": "F_OrderNo", "x": 15, "y": 20, "w": 60, "h": 7,
                 "data_ref": "$record.GS_HEADER.IV_ORDER_NO"},
            ],
            "items_table": {
                "name": "Items", "x": 15, "y": 40, "w": 180,
                "data_ref": "$record.GT_ITEMS.ITEM",
                "columns": [
                    {"name": "IV_MATERIAL", "label": "Material", "w": 80},
                    {"name": "IV_QTY", "label": "Qty", "w": 40, "align": "center"},
                ],
            },
        }],
    )
    root = etree.fromstring(xdp)
    ns = {"t": "http://www.xfa.org/schema/xfa-template/3.3/"}

    row_subform = root.find(".//t:subform[@name='Items_Row']", ns)
    assert row_subform is not None

    occur = row_subform.find("t:occur", ns)
    assert occur is not None and occur.get("max") == "-1"

    bind = row_subform.find("t:bind", ns)
    assert bind is not None
    assert bind.get("ref") == "$record.GT_ITEMS.ITEM[*]"

    row_fields = row_subform.findall(".//t:field", ns)
    assert {f.get("name") for f in row_fields} == {"IV_MATERIAL", "IV_QTY"}
    for f in row_fields:
        fbind = f.find("t:bind", ns)
        assert fbind.get("ref").startswith("$.")

    header_subform = root.find(".//t:subform[@name='Items_Hdr']", ns)
    assert header_subform is not None
    draws = header_subform.findall(".//t:draw", ns)
    assert len(draws) == 2


def test_empty_page_raises():
    try:
        xg.generate_xdp(form_name="ZEMPTY", pages=[{"width_mm": 210, "height_mm": 297, "elements": []}])
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    test_flat_form_is_well_formed_xml()
    test_header_items_form_has_repeating_row_and_bind()
    test_empty_page_raises()
    print("OK")
