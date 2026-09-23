"""
Minimal client for SAP's ADT REST API (/sap/bc/adt/...), used to create
custom (Z/Y) ABAP objects: the dev-only builder class + RFC wrapper
(package $TMP) and per-form header/item DDIC types (caller's Z/Y package).

Safety model (see guards.py / sap_gate.py):
  * Every request goes through _send(): path allow-list + custom-name check
    first, then per-call user approval, then the HTTP call. There is no
    other way in this class to reach SAP.
  * Existing objects are only ever overwritten if they already live in the
    exact package we intend to write to; otherwise GuardError.
  * Nothing here writes tables, BADIs, enhancements or customizing -- only
    the four allow-listed object types. (SAP itself records TADIR /
    repository / transport bookkeeping when any object is created through
    its own APIs; that is inherent and not done by this code directly.)

Confidence levels: class create/lock/source/activate is a well-established
ADT pattern. Function module / DDIC structure / table type payloads are
best effort and vary by NetWeaver release -- if one is rejected, the error
shows the request; adjust the content-type/attribute constants below.
"""
from __future__ import annotations

import os
import requests
from lxml import etree

import fm_definitions
import guards

ADT_CORE_NS = "http://www.sap.com/adt/core"


class ADTError(Exception):
    pass


def _env(name):
    val = os.environ.get(name)
    if not val:
        raise ADTError(f"Missing required environment variable {name}.")
    return val


class ADTClient:
    def __init__(self, gate, base_url=None, client=None, user=None, password=None, verify_ssl=True):
        self.gate = gate
        self.base_url = (base_url or _env("SAP_BASE_URL")).rstrip("/")
        self.client = client or os.environ.get("SAP_CLIENT")
        self.session = requests.Session()
        self.session.auth = (user or _env("SAP_USER"), password or _env("SAP_PASSWORD"))
        self.session.verify = verify_ssl
        self.csrf_token = None

    # -- the single choke point for all SAP traffic ----------------------

    def _send(self, method, path, purpose, kind, params=None, headers=None,
              data=None, detail="", csrf=True):
        guards.assert_allowed_adt_path(path)
        if csrf:
            self._ensure_csrf()
        self.gate.check(method=method, path=path, purpose=purpose, kind=kind, detail=detail)
        merged_params = {}
        if self.client:
            merged_params["sap-client"] = self.client
        merged_params.update(params or {})
        h = dict(headers or {})
        if csrf:
            h["X-CSRF-Token"] = self.csrf_token
        return self.session.request(method, self.base_url + path, params=merged_params,
                                    headers=h, data=data, timeout=60)

    def _ensure_csrf(self):
        if self.csrf_token:
            return
        resp = self._send(
            "GET", "/sap/bc/adt/discovery",
            purpose="Open an ADT session (fetch CSRF token; no SAP data is read or changed)",
            kind="READ", headers={"X-CSRF-Token": "Fetch"}, csrf=False)
        if resp.status_code >= 400:
            raise ADTError(
                f"Could not reach ADT services (HTTP {resp.status_code}). Confirm the "
                f"/sap/bc/adt ICF node is active and the credentials are correct.")
        token = resp.headers.get("x-csrf-token")
        if not token:
            raise ADTError("ADT did not return a CSRF token -- check that ADT services are active.")
        self.csrf_token = token

    # -- primitives ------------------------------------------------------

    def object_info(self, uri, label):
        """None if the object doesn't exist, else {'package': str|None}."""
        resp = self._send("GET", uri, purpose=f"Check whether {label} exists and which package it is in",
                          kind="READ")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ADTError(f"Unexpected HTTP {resp.status_code} checking {label}: {resp.text[:300]}")
        package = None
        try:
            root = etree.fromstring(resp.content)
            for el in root.iter():
                if etree.QName(el).localname == "packageRef":
                    package = el.get(f"{{{ADT_CORE_NS}}}name") or el.get("name")
                    break
        except etree.XMLSyntaxError:
            pass
        return {"package": package}

    @staticmethod
    def _assert_same_package(label, actual, expected):
        if not actual or actual.upper() != expected.upper():
            raise guards.GuardError(
                f"Refusing to modify existing {label}: it is in package "
                f"'{actual or 'unknown'}', not the expected custom package '{expected}'. "
                f"Nothing was changed.")

    def _create(self, base_path, content_type, body, label, package, transport):
        params = {"corrNr": transport} if transport else None
        resp = self._send("POST", base_path, purpose=f"Create {label} in package {package}",
                          kind="WRITE", params=params,
                          headers={"Content-Type": content_type}, data=body.encode("utf-8"),
                          detail=f"package={package}" + (f", transport={transport}" if transport else ", no transport"))
        if resp.status_code >= 400:
            raise ADTError(f"Failed to create {label}: HTTP {resp.status_code} {resp.text[:800]}")

    def _lock(self, uri, label):
        resp = self._send("POST", uri, purpose=f"Lock {label} for editing", kind="WRITE",
                          params={"_action": "LOCK", "accessMode": "MODIFY"},
                          headers={"Accept": "application/vnd.sap.as+xml"})
        if resp.status_code >= 400:
            raise ADTError(f"Lock failed for {label}: HTTP {resp.status_code} {resp.text[:300]}")
        handle = None
        try:
            root = etree.fromstring(resp.content)
            handle = root.findtext(".//{*}LOCK_HANDLE") or root.findtext(".//LOCK_HANDLE")
        except etree.XMLSyntaxError:
            pass
        handle = handle or (resp.text or "").strip() or None
        if not handle:
            raise ADTError(f"Could not parse lock handle for {label}: {resp.text[:300]}")
        return handle

    def _unlock(self, uri, handle, label):
        resp = self._send("POST", uri, purpose=f"Release lock on {label}", kind="WRITE",
                          params={"_action": "UNLOCK", "lockHandle": handle})
        if resp.status_code >= 400:
            raise ADTError(f"Unlock failed for {label}: HTTP {resp.status_code} {resp.text[:300]}")

    def _put_source(self, uri, source, handle, label, transport):
        params = {"lockHandle": handle}
        if transport:
            params["corrNr"] = transport
        resp = self._send("PUT", uri + "/source/main", purpose=f"Write source of {label}",
                          kind="WRITE", params=params,
                          headers={"Content-Type": "text/plain; charset=utf-8"},
                          data=source.encode("utf-8"), detail=f"{len(source)} characters of source")
        if resp.status_code >= 400:
            raise ADTError(f"Failed to set source for {label}: HTTP {resp.status_code} {resp.text[:500]}")

    def _activate(self, uri, name, label):
        name = guards.assert_custom_name(name, "object to activate")
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                f'<adtcore:objectReferences xmlns:adtcore="{ADT_CORE_NS}">'
                f'<adtcore:objectReference adtcore:uri="{uri}" adtcore:name="{name}"/>'
                '</adtcore:objectReferences>')
        resp = self._send("POST", "/sap/bc/adt/activation", purpose=f"Activate {label}", kind="WRITE",
                          params={"method": "activate", "preauditRequested": "true"},
                          headers={"Content-Type": "application/xml"}, data=body.encode("utf-8"))
        if resp.status_code >= 400:
            raise ADTError(f"Activation failed for {label}: HTTP {resp.status_code} {resp.text[:800]}")
        if b'type="E"' in resp.content:
            raise ADTError(f"Activation reported errors for {label}: {resp.text[:800]}")

    def _push_source_and_activate(self, uri, name, source, label, transport):
        handle = self._lock(uri, label)
        try:
            self._put_source(uri, source, handle, label, transport)
        finally:
            self._unlock(uri, handle, label)
        self._activate(uri, name, label)

    # -- class -----------------------------------------------------------

    CLASS_BASE = "/sap/bc/adt/oo/classes"

    def deploy_class(self, name, source, package, description="", transport=None, allow_local_tmp=False):
        name = guards.assert_custom_name(name, "class")
        package = guards.assert_custom_package(package, allow_local_tmp)
        uri, label = f"{self.CLASS_BASE}/{name.lower()}", f"class {name}"
        info = self.object_info(uri, label)
        created = info is None
        if created:
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<class:abapClass xmlns:class="http://www.sap.com/adt/oo/classes" '
                    f'xmlns:adtcore="{ADT_CORE_NS}" adtcore:name="{name}" adtcore:type="CLAS/OC" '
                    f'adtcore:description="{description or name}" class:final="true" class:visibility="public">'
                    f'<adtcore:packageRef adtcore:name="{package}"/></class:abapClass>')
            self._create(self.CLASS_BASE, "application/vnd.sap.adt.oo.classes.v2+xml", body,
                         label, package, transport)
        else:
            self._assert_same_package(label, info["package"], package)
        self._push_source_and_activate(uri, name, source, label, transport)
        return {"object": name, "created": created, "activated": True}

    # -- function group / module (best effort) ----------------------------

    FUGR_BASE = "/sap/bc/adt/functions/groups"

    def deploy_fmodule(self, fugr, fm, source, package, description="", transport=None, allow_local_tmp=False):
        fugr = guards.assert_custom_name(fugr, "function group")
        fm = guards.assert_custom_name(fm, "function module")
        package = guards.assert_custom_package(package, allow_local_tmp)
        fugr_uri, fugr_label = f"{self.FUGR_BASE}/{fugr.lower()}", f"function group {fugr}"
        fm_uri, fm_label = f"{fugr_uri}/fmodules/{fm.lower()}", f"function module {fm}"

        info = self.object_info(fugr_uri, fugr_label)
        created_group = info is None
        if created_group:
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<group:abapFunctionGroup xmlns:group="http://www.sap.com/adt/functions/groups" '
                    f'xmlns:adtcore="{ADT_CORE_NS}" adtcore:name="{fugr}" adtcore:type="FUGR/F" '
                    f'adtcore:description="{description or fugr}">'
                    f'<adtcore:packageRef adtcore:name="{package}"/></group:abapFunctionGroup>')
            self._create(self.FUGR_BASE, "application/vnd.sap.adt.functions.groups.v3+xml", body,
                         fugr_label, package, transport)
        else:
            self._assert_same_package(fugr_label, info["package"], package)

        resp_exists = self._send("GET", fm_uri, purpose=f"Check whether {fm_label} exists", kind="READ")
        created_fm = resp_exists.status_code == 404
        if created_fm:
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<fmodule:abapFunctionModule xmlns:fmodule="http://www.sap.com/adt/functions/fmodules" '
                    f'xmlns:adtcore="{ADT_CORE_NS}" adtcore:name="{fm}" adtcore:type="FUGR/FF" '
                    f'adtcore:description="{description or fm}" fmodule:processingType="rfc"/>')
            try:
                self._create(f"{fugr_uri}/fmodules", "application/vnd.sap.adt.functions.fmodules.v3+xml",
                             body, fm_label, package, transport)
            except ADTError as e:
                raise ADTError(f"{e} -- if this system's ADT schema rejects 'processingType', create the "
                               f"module without it and tick 'Remote-Enabled Module' once in SE37 "
                               f"(Attributes tab): a checkbox toggle, not a code change.")
        elif resp_exists.status_code != 200:
            raise ADTError(f"Unexpected HTTP {resp_exists.status_code} checking {fm_label}")

        self._push_source_and_activate(fm_uri, fm, source, fm_label, transport)
        return {"object": fm, "function_group_created": created_group,
                "module_created": created_fm, "activated": True}

    # -- DDIC structure / table type (per-form header & item types) --------
    # Deliverable objects: caller's real Z/Y package + transport, never $TMP.

    STRUCTURE_BASE = "/sap/bc/adt/ddic/structures"
    TABLETYPE_BASE = "/sap/bc/adt/ddic/tabletypes"
    STRUCT_CT = "application/vnd.sap.adt.ddic.structures.v2+xml"
    TTYPE_CT = "application/vnd.sap.adt.ddic.tabletypes.v2+xml"

    def _deploy_ddic(self, base, ct, adt_type, name, source, package, description, transport, label):
        uri = f"{base}/{name.lower()}"
        info = self.object_info(uri, label)
        created = info is None
        if created:
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<blue:blueSource xmlns:blue="http://www.sap.com/wbobj/blue" xmlns:adtcore="{ADT_CORE_NS}" '
                    f'adtcore:name="{name}" adtcore:type="{adt_type}" adtcore:description="{description or name}">'
                    f'<adtcore:packageRef adtcore:name="{package}"/></blue:blueSource>')
            self._create(base, ct, body, label, package, transport)
        else:
            self._assert_same_package(label, info["package"], package)
        self._push_source_and_activate(uri, name, source, label, transport)
        return {"object": name, "created": created, "activated": True}

    def deploy_structure(self, name, fields, package, description="", transport=None):
        name = guards.assert_custom_name(name, "structure")
        package = guards.assert_custom_package(package)
        lines = [f"define structure {name.lower()} {{"]
        for f in fields:
            fname = guards.assert_identifier(f["name"], "field name")
            ftype = guards.assert_type_expr(f.get("typename", "abap.string"))
            lines.append(f"  {fname.lower()} : {ftype.lower()};")
        lines.append("}")
        return self._deploy_ddic(self.STRUCTURE_BASE, self.STRUCT_CT, "TABL/DS", name,
                                 "\n".join(lines), package, description, transport, f"structure {name}")

    def deploy_tabletype(self, name, row_type_name, package, description="", transport=None):
        name = guards.assert_custom_name(name, "table type")
        row_type_name = guards.assert_custom_name(row_type_name, "row structure")
        package = guards.assert_custom_package(package)
        source = (f"define table type {name.lower()} {{\n"
                  f"  standard table of {row_type_name.lower()} with non-unique key table_line;\n}}")
        return self._deploy_ddic(self.TABLETYPE_BASE, self.TTYPE_CT, "TTYP/DA", name,
                                 source, package, description, transport, f"table type {name}")

    def deploy_header_and_item_types(self, form_name, header_fields, item_fields, package, transport=None):
        """
        Creates <FORM>_HDR_S / <FORM>_ITM_S / <FORM>_ITM_T. Returns the
        GS_HEADER / GT_ITEMS interface field entries for deploy_form.
        """
        form_name = guards.assert_custom_name(form_name, "form")
        package = guards.assert_custom_package(package)
        base = form_name[:24]
        result = {"types": {}, "fields": []}
        if header_fields:
            hdr = f"{base}_HDR_S"
            result["types"]["header_structure"] = self.deploy_structure(
                hdr, header_fields, package, f"{form_name} header", transport)
            result["fields"].append({"name": "GS_HEADER", "typing": "TYPE", "typename": hdr})
        if item_fields:
            itm_s, itm_t = f"{base}_ITM_S", f"{base}_ITM_T"
            result["types"]["item_structure"] = self.deploy_structure(
                itm_s, item_fields, package, f"{form_name} item", transport)
            result["types"]["item_table_type"] = self.deploy_tabletype(
                itm_t, itm_s, package, f"{form_name} items", transport)
            result["fields"].append({"name": "GT_ITEMS", "typing": "TYPE", "typename": itm_t})
        return result


def bootstrap(client: ADTClient, package: str = None, class_source: str = None) -> dict:
    """
    Deploys ZCL_FP_FORM_BUILDER + Z_FP_FORM_DEPLOY as LOCAL objects (no
    transport). Package is $TMP by default (or SAP_BOOTSTRAP_PACKAGE, which
    must itself be $TMP or a Z/Y package -- never a standard package).
    """
    package = guards.assert_custom_package(
        package or os.environ.get("SAP_BOOTSTRAP_PACKAGE", guards.LOCAL_PACKAGE), allow_local_tmp=True)
    return {
        "package": package,
        "class": client.deploy_class(
            "ZCL_FP_FORM_BUILDER", class_source or _read_class_source(), package,
            "Adobe Interface/Form builder engine (dev-only, local)", None, allow_local_tmp=True),
        "function_module": client.deploy_fmodule(
            fm_definitions.FUNCTION_GROUP, fm_definitions.FUNCTION_MODULE,
            fm_definitions.render_function_module_source(), package,
            "RFC entry point for ZCL_FP_FORM_BUILDER (dev-only, local)", None, allow_local_tmp=True),
    }


def _read_class_source():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "abap_src", "zcl_fp_form_builder.abap"), "r", encoding="utf-8") as f:
        return f.read()
