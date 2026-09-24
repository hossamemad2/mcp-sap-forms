"""
Hard safety rules for everything this server sends to SAP. Enforced at the
lowest level (every HTTP request passes through assert_allowed_adt_path /
the SOAP allow-list), not just in the tool layer, so a bug or a
mis-prompted agent cannot route around them.

Rules:
  1. Custom objects only: every object name must start with Z or Y.
     Only object types the server is designed to create are reachable
     (allow-listed ADT resources + the one SOAP-RFC service). No BADI /
     enhancement / table-content / customizing endpoints exist in the
     allow-list, so they cannot be called at all.
  2. Never a standard package: package must start with Z or Y. The single
     exception is the local package $TMP, and only for the dev-only
     builder tooling created by bootstrap (allow_local_tmp=True there).
  3. (Approval of every call lives in sap_gate.py.)
"""
from __future__ import annotations

import re


class GuardError(Exception):
    pass


LOCAL_PACKAGE = "$TMP"

_CUSTOM_NAME = re.compile(r"^[ZY][A-Z0-9_]{0,29}$")
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")
_TYPE_EXPR = re.compile(r"^[A-Za-z][A-Za-z0-9_.()]{0,40}$")

ADT_PREFIXES = (
    "/sap/bc/adt/oo/classes",
    "/sap/bc/adt/functions/groups",
    "/sap/bc/adt/ddic/structures",
    "/sap/bc/adt/ddic/tabletypes",
)
ADT_FIXED_PATHS = ("/sap/bc/adt/discovery", "/sap/bc/adt/activation")
SOAP_RFC_PATH = "/sap/bc/soap/rfc"


def assert_custom_name(name: str, what: str = "object") -> str:
    n = (name or "").strip().upper()
    if not _CUSTOM_NAME.match(n):
        raise GuardError(
            f"Refusing {what} name '{name}': only custom objects are allowed "
            f"(name must start with Z or Y, A-Z/0-9/_ only, max 30 chars).")
    return n


def assert_custom_package(package: str, allow_local_tmp: bool = False) -> str:
    p = (package or "").strip().upper()
    if allow_local_tmp and p == LOCAL_PACKAGE:
        return p
    if not _CUSTOM_NAME.match(p):
        raise GuardError(
            f"Refusing package '{package}': objects may only be saved in custom "
            f"packages (name must start with Z or Y); never in standard packages.")
    return p


def assert_identifier(name: str, what: str = "identifier") -> str:
    if not _IDENT.match(name or ""):
        raise GuardError(f"Invalid {what} '{name}' (letters, digits, underscore; max 30).")
    return name


def assert_type_expr(type_expr: str) -> str:
    if not _TYPE_EXPR.match(type_expr or ""):
        raise GuardError(f"Invalid type expression '{type_expr}'.")
    return type_expr


def assert_allowed_adt_path(path: str) -> None:
    if ".." in path or "//" in path or "?" in path:
        raise GuardError(f"Refusing malformed ADT path '{path}'.")
    if path in ADT_FIXED_PATHS:
        return
    for prefix in ADT_PREFIXES:
        if path == prefix:
            return
        if path.startswith(prefix + "/"):
            object_segment = path[len(prefix) + 1:].split("/")[0]
            assert_custom_name(object_segment, "object in URL")
            return
    raise GuardError(
        f"Refusing SAP call to '{path}': not on the allow-list of resources this "
        f"server may touch (custom class / function group / structure / table "
        f"type / activation only).")


def assert_allowed_soap_path(path: str) -> None:
    if path != SOAP_RFC_PATH:
        raise GuardError(f"Refusing SOAP call to '{path}'.")


RFC_ALLOWED_FUNCTIONS = ("Z_FP_FORM_DEPLOY",)


def assert_allowed_rfc_function(name: str) -> None:
    if name not in RFC_ALLOWED_FUNCTIONS:
        raise GuardError(f"Refusing RFC call to '{name}': only {', '.join(RFC_ALLOWED_FUNCTIONS)} may be called.")


def validate_deploy_args(mode, interface_name, form_name, devclass, fields):
    """Shared by the SOAP and native-RFC clients. Returns normalized (interface, form, devclass)."""
    if mode not in ("CHECK", "DEPLOY"):
        raise GuardError(f"Invalid mode '{mode}'.")
    interface_name = assert_custom_name(interface_name, "interface")
    form_name = assert_custom_name(form_name, "form")
    if mode == "DEPLOY":
        devclass = assert_custom_package(devclass)
    for f in fields:
        assert_identifier(f["name"], "interface field name")
        assert_type_expr(f["typename"])
        if f.get("typing", "TYPE") not in ("TYPE", "LIKE"):
            raise GuardError(f"Invalid typing '{f.get('typing')}' for field {f['name']}.")
    return interface_name, form_name, devclass
