import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import guards
import sap_gate


def test_only_z_or_y_names():
    assert guards.assert_custom_name("zcl_foo") == "ZCL_FOO"
    assert guards.assert_custom_name("YFOO") == "YFOO"
    for bad in ("CL_GUI_ALV_GRID", "SFP", "/SAPAPO/X", "", "Z" + "A" * 30, "Z-BAD"):
        with pytest.raises(guards.GuardError):
            guards.assert_custom_name(bad)


def test_standard_packages_refused_and_tmp_only_when_allowed():
    assert guards.assert_custom_package("ZSD_FORMS") == "ZSD_FORMS"
    for bad in ("SAP", "BASIS", "SFP", "/SAPAPO/A", "", "$TMP"):
        with pytest.raises(guards.GuardError):
            guards.assert_custom_package(bad)
    assert guards.assert_custom_package("$TMP", allow_local_tmp=True) == "$TMP"
    with pytest.raises(guards.GuardError):
        guards.assert_custom_package("SAP", allow_local_tmp=True)


def test_adt_path_allow_list():
    ok = ["/sap/bc/adt/discovery", "/sap/bc/adt/activation",
          "/sap/bc/adt/oo/classes", "/sap/bc/adt/oo/classes/zcl_fp_form_builder",
          "/sap/bc/adt/oo/classes/zcl_fp_form_builder/source/main",
          "/sap/bc/adt/functions/groups/zfp_form_deploy/fmodules/z_fp_form_deploy",
          "/sap/bc/adt/ddic/structures/zfoo_hdr_s"]
    for p in ok:
        guards.assert_allowed_adt_path(p)
    bad = ["/sap/bc/adt/oo/classes/cl_gui_alv_grid",          # standard class
           "/sap/bc/adt/enhancements/enhoxhb/zimpl",           # BADI / enhancement
           "/sap/bc/adt/datapreview/freestyle",                # table content
           "/sap/bc/adt/oo/classes/../../datapreview",         # traversal
           "/sap/bc/adt/programs/programs/zprog",              # object type not allow-listed
           "/sap/bc/adt/oo/classes/zfoo?x=1"]                  # params must go via params=
    for p in bad:
        with pytest.raises(guards.GuardError):
            guards.assert_allowed_adt_path(p)


def test_gate_blocks_when_declined_and_when_approver_fails():
    calls = []
    gate = sap_gate.ApprovalGate(lambda text: calls.append(text) or False, system="DEV")
    with pytest.raises(sap_gate.SapCallDenied):
        gate.check("GET", "/sap/bc/adt/discovery", "x", "READ")
    assert len(calls) == 1 and "GET /sap/bc/adt/discovery" in calls[0]

    def boom(text):
        raise RuntimeError("client does not support elicitation")
    with pytest.raises(sap_gate.SapCallDenied):
        sap_gate.ApprovalGate(boom).check("GET", "/x", "x", "READ")


def test_gate_allows_only_on_explicit_approval_and_asks_every_time():
    n = []
    gate = sap_gate.ApprovalGate(lambda text: n.append(1) or True)
    gate.check("GET", "/a", "p", "READ")
    gate.check("GET", "/a", "p", "READ")
    assert len(n) == 2  # no caching / no "approve all"
