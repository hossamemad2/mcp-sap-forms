"""
MCP server: FSD + PDF -> deployed SAP Adobe Interface/Form (header+items
aware), with these hard restrictions (see guards.py / sap_gate.py):

  1. Custom Z/Y objects only; no standard objects, BADIs, enhancements,
     table entries or customizing -- those endpoints are not reachable.
  2. Never saved in a standard package (Z/Y packages only; $TMP solely for
     the dev-only builder tooling created by bootstrap).
  3. EVERY request to SAP -- reads included -- needs the user's explicit
     approval, asked directly of the human via MCP elicitation for that
     single call. No "approve all". If approval can't be obtained (declined,
     cancelled, or the client lacks elicitation support) the call is not made.

Suggested flow: bootstrap_sap_artifacts (once per system) -> parse_fsd ->
parse_pdf_layout -> [agent pairs fields to regions] -> ensure_form_types
(header+items only) -> generate_xdp -> show preview -> deploy_form.

Connectivity: SAP_TRANSPORT=http (default: SAP_BASE_URL, SAP_USER,
SAP_PASSWORD, optional SAP_CLIENT) or SAP_TRANSPORT=rfc (SAP_RFC_ASHOST,
SAP_RFC_SYSNR, SAP_CLIENT, SAP_USER, SAP_PASSWORD, optional SAP_ROUTER_STRING
for SAProuter). See README "Connecting" and run diagnose_connection first.
Other optional env: SAP_BOOTSTRAP_PACKAGE, SAP_FORMS_OUT_DIR.
"""
from __future__ import annotations

import os
from typing import Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

import adt_client
import connectivity
import fsd_parser
import guards
import pdf_layout_parser
import rfc_client
import sap_gate
import sap_transport
import soap_rfc_client
import xdp_generator

mcp = FastMCP("sap-adobe-forms")

_OUT_DIR = os.environ.get("SAP_FORMS_OUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_generated"))
os.makedirs(_OUT_DIR, exist_ok=True)
_AUDIT_LOG = os.path.join(_OUT_DIR, "sap_audit.log")


class _Approval(BaseModel):
    approve: bool = Field(default=False, description="Tick to allow this single SAP call")


def _system_label() -> str:
    return sap_transport.describe_current()


def _deploy_client(gate):
    """Native RFC when SAP_TRANSPORT=rfc, otherwise SOAP-RFC over HTTP. Both are context managers."""
    if sap_transport.transport_kind() == "rfc":
        return rfc_client.RfcClient(gate)
    return soap_rfc_client.SoapRfcClient(gate)


def _require_http_transport(tool: str):
    if sap_transport.transport_kind() != "http":
        raise sap_transport.SapConnectionError(
            f"{tool} creates ABAP/DDIC objects through ADT, which needs SAP_TRANSPORT=http. Under "
            f"SAP_TRANSPORT=rfc only check_form_status, deploy_form and diagnose_connection are available, "
            f"and Z_FP_FORM_DEPLOY must already exist in SAP.")


def _make_approver(ctx: Context):
    """Sync callable usable from a worker thread; asks the human via elicitation."""
    def approve(text: str) -> bool:
        async def ask() -> bool:
            result = await ctx.elicit(message=text, schema=_Approval)
            return result.action == "accept" and bool(getattr(result.data, "approve", False))
        return anyio.from_thread.run(ask)
    return approve


async def _with_gate(ctx: Context, work):
    """Runs blocking SAP work in a worker thread with a per-call approval gate."""
    gate = sap_gate.ApprovalGate(_make_approver(ctx), system=_system_label(), audit_path=_AUDIT_LOG)
    try:
        return await anyio.to_thread.run_sync(work, gate)
    except sap_gate.SapCallDenied as e:
        return {"denied": True, "error": str(e)}
    except (guards.GuardError, adt_client.ADTError, soap_rfc_client.SoapRfcError,
            rfc_client.RfcError, sap_transport.SapConnectionError) as e:
        return {"error": str(e)}


@mcp.tool()
async def bootstrap_sap_artifacts(ctx: Context) -> dict:
    """
    Creates/updates the dev-only builder tooling (class ZCL_FP_FORM_BUILDER
    + RFC wrapper Z_FP_FORM_DEPLOY) as local objects in $TMP. Makes several
    SAP calls; the user is asked to approve EACH one. If a call is denied,
    stop and ask the user -- never retry or work around it. Needs
    SAP_TRANSPORT=http (ADT).
    """
    def work(gate):
        _require_http_transport("bootstrap_sap_artifacts")
        return adt_client.bootstrap(adt_client.ADTClient(gate))
    return await _with_gate(ctx, work)


@mcp.tool()
async def diagnose_connection(ctx: Context) -> dict:
    """
    Finds where the connection to SAP breaks (config, DNS, TCP, logon/HTTP
    session) and stops at the first failure with a hint. Works for both
    transports and both VPN and SAProuter. Each network step is a separate
    SAP call the user approves; if one is denied, stop and ask the user.
    """
    return await _with_gate(ctx, lambda gate: connectivity.diagnose(gate))


@mcp.tool()
def parse_fsd(fsd_path: str) -> dict:
    """
    Local only (no SAP call). Extracts candidate fields from an FSD
    (.docx/.xlsx/.pdf), tagged section=HEADER/ITEM/None by a heading
    heuristic, with a best-effort typename_guess -- review before use.
    """
    return fsd_parser.parse_fsd(fsd_path)


@mcp.tool()
def parse_pdf_layout(pdf_path: str) -> dict:
    """
    Local only (no SAP call). Per-page text blocks with mm positions
    (top-left origin) and ltr/rtl direction from a layout PDF.
    """
    return pdf_layout_parser.parse_pdf_layout(pdf_path)


@mcp.tool()
async def ensure_form_types(ctx: Context, form_name: str, package: str,
                            header_fields: Optional[list] = None, item_fields: Optional[list] = None,
                            transport: Optional[str] = None) -> dict:
    """
    Header+items forms only. Creates the per-form DDIC types
    <FORM>_HDR_S / <FORM>_ITM_S / <FORM>_ITM_T in `package` (must be a
    custom Z/Y package -- standard packages are refused). Each SAP call is
    individually approved by the user. Returns the GS_HEADER/GT_ITEMS
    interface field entries to pass to deploy_form.
    header_fields/item_fields: [{"name": str, "typename": "abap.string"}].
    """
    def work(gate):
        _require_http_transport("ensure_form_types")
        return adt_client.ADTClient(gate).deploy_header_and_item_types(
            form_name=form_name, header_fields=header_fields or [], item_fields=item_fields or [],
            package=package, transport=transport)

    return await _with_gate(ctx, work)


@mcp.tool()
def generate_xdp(form_name: str, pages: list, locale: str = "en_US") -> dict:
    """
    Local only (no SAP call). Deterministically renders XFA from a decided
    field-to-position mapping; returns file path + preview to show the user.
    """
    try:
        xdp_bytes = xdp_generator.generate_xdp(form_name=form_name, pages=pages, locale=locale)
    except ValueError as e:
        return {"error": str(e)}
    out_path = os.path.join(_OUT_DIR, f"{form_name}.xdp")
    xdp_generator.save_xdp(xdp_bytes, out_path)
    text = xdp_bytes.decode("utf-8")
    return {"xdp_path": out_path, "size_bytes": len(xdp_bytes),
            "preview": text[:4000] + ("...(truncated)" if len(text) > 4000 else "")}


@mcp.tool()
async def check_form_status(ctx: Context, interface_name: str, form_name: str) -> dict:
    """Read-only existence check of a Z/Y interface and form. Requires user approval of the SAP call."""
    def work(gate):
        with _deploy_client(gate) as client:
            return client.check_status(interface_name, form_name)

    return await _with_gate(ctx, work)


@mcp.tool()
async def deploy_form(ctx: Context, interface_name: str, form_name: str, package: str, fields: list,
                      xdp_path: str, confirmed: bool, transport: Optional[str] = None) -> dict:
    """
    Creates and activates a Z/Y Adobe Interface + Form in a custom Z/Y
    package (standard packages refused). Irreversible change to a shared SAP
    system: set confirmed=True only after the user has seen the generate_xdp
    preview (and ensure_form_types result) and said to proceed. Independently
    of that, the user is asked to approve each SAP call (existence check, then
    the deploy call). On any denial, stop and ask the user.
    fields: flat [{"name","typing","typename"}] (GS_HEADER/GT_ITEMS entries
    from ensure_form_types, plus any scalar fields).
    """
    if not confirmed:
        return {"error": "confirmed=False -- show the user the XDP preview and get explicit approval first."}
    if not os.path.isfile(xdp_path):
        return {"error": f"xdp_path not found: {xdp_path}"}
    with open(xdp_path, "rb") as f:
        xdp_bytes = f.read()

    def work(gate):
        with _deploy_client(gate) as client:
            status = client.check_status(interface_name, form_name)
            if status["interface_exists"] or status["form_exists"]:
                return {"error": f"Already exists (interface_exists={status['interface_exists']}, "
                                 f"form_exists={status['form_exists']}) -- nothing changed."}
            return client.deploy(interface_name=interface_name, form_name=form_name, devclass=package,
                                 fields=fields, ordernum=transport, xdp_xstring=xdp_bytes)

    return await _with_gate(ctx, work)


if __name__ == "__main__":
    mcp.run()
