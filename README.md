# mcp-sap-forms

MCP server that turns a Functional Spec Document (FSD) + a layout PDF into
a deployed SAP Adobe Interface + Form (SFPI/SFPF), including header+items
(repeating line-item table) forms. No manual SAP-side setup: the server
bootstraps its own dev-client-only ABAP tooling via SAP's ADT REST API.

## Setup

```bash
pip install -r requirements.txt
```

Environment variables for the default http transport (for `SAP_TRANSPORT=rfc`
see "Connecting" below):

| Var | Required | Notes |
|---|---|---|
| `SAP_BASE_URL` | yes | e.g. `https://sapdev.example.com:44300` |
| `SAP_USER` / `SAP_PASSWORD` | yes | needs standard ABAP developer authorizations (S_DEVELOP etc.) |
| `SAP_CLIENT` | no | logon client, if your system needs it in the URL |
| `SAP_BOOTSTRAP_PACKAGE` | no | defaults to `$TMP` |
| `SAP_FORMS_OUT_DIR` | no | where generated `.xdp` files are written |

Register in your MCP client config pointing at `server.py`.

## Connecting: VPN or SAProuter

Pick the transport with `SAP_TRANSPORT`. Run `diagnose_connection` first: it
walks config -> DNS -> TCP -> logon/HTTP and stops at the first failure with
a hint (each network step is one approval prompt).

| | VPN (or direct) | SAProuter |
|---|---|---|
| **`SAP_TRANSPORT=rfc`** (native RFC) | set the three `SAP_RFC_*` vars, no router string | add `SAP_ROUTER_STRING` |
| **`SAP_TRANSPORT=http`** (default; ADT + SOAP-RFC) | connect the VPN, set `SAP_BASE_URL` | not supported (see below) |

**RFC transport** (needs the SAP NetWeaver RFC SDK from the SAP Support
Portal, then `pip install pyrfc`; set `SAPNWRFC_HOME`):

| Var | Notes |
|---|---|
| `SAP_RFC_ASHOST` | application server host (behind the router, if any) |
| `SAP_RFC_SYSNR` | 2-digit instance number, e.g. `00` (gateway port 33`<nn>`) |
| `SAP_CLIENT` | 3-digit client |
| `SAP_ROUTER_STRING` | optional; router hops only, e.g. `/H/router.corp/S/3299`. May contain `/P/password`: never logged or shown |
| `SAP_USER` / `SAP_PASSWORD` | as before |

RFC can only call the already-existing `Z_FP_FORM_DEPLOY`
(`check_form_status`, `deploy_form`, `diagnose_connection`). It cannot
create ABAP or DDIC objects, so `bootstrap_sap_artifacts` and
`ensure_form_types` (ADT) need the http transport, and the one-time setup
objects must already exist in SAP.

**HTTP transport** extras (all optional): `SAP_CA_BUNDLE` (internal CA
file), `SAP_VERIFY_SSL=false` (shown in every approval prompt),
`SAP_TLS_SERVER_NAME` (certificate hostname when you go through a
tunnel/alias), `SAP_TIMEOUT_CONNECT` (default 10 s) and `SAP_TIMEOUT_READ`
(default 120 s). Only failures before a request is sent are retried, so a
create is never duplicated. A read timeout on a write means SAP may have
processed it: check with `check_form_status` before retrying.
`requests` can't speak the SAProuter protocol. If only the router reaches
SAP, use the rfc transport, or run your own port-forward and point
`SAP_BASE_URL` at `https://localhost:PORT` with `SAP_TLS_SERVER_NAME`.

Tips: run the MCP on the machine that has the VPN (WSL and containers often
don't inherit VPN routes or split-tunnel DNS); the router's route permission
table must allow your target host/port; behind a corporate proxy use
`NO_PROXY` for the SAP host.

## Safety restrictions (enforced in code)

1. **Custom objects only.** Every object name must start with `Z`/`Y`
   (`guards.assert_custom_name`). The only SAP endpoints reachable are an
   allow-list (custom class, function group/module, DDIC structure/table
   type, activation, the SOAP-RFC service) — there is no path to BADIs,
   enhancements, table content, customizing or standard objects. The ABAP
   wrapper `Z_FP_FORM_DEPLOY` re-checks Z/Y names and packages itself.
   Existing objects are only overwritten if they already sit in the exact
   package being targeted.
   *Inherent limit:* creating any object makes SAP write its own
   bookkeeping (TADIR, repository, transport entries) through its own APIs.
   This server never writes tables directly.
2. **Never a standard package.** Packages must start with `Z`/`Y`. The
   single exception is `$TMP`, only for the dev-only builder tooling
   (`bootstrap_sap_artifacts`); deliverable forms/types can never go there.
3. **Every SAP call needs your approval, reads included.** Before each
   HTTP request the server asks *you* (MCP elicitation, not the agent) to
   approve that single call, showing system, method/path, purpose and
   details. No "approve all". Denied, cancelled, or a client without
   elicitation support = the call is **not made**. Decisions are logged to
   `_generated/sap_audit.log` (no credentials or bodies).
   Granularity note: `deploy_form`'s deploy call is one approval, but it
   runs the fixed wrapper code that performs several steps inside SAP via
   the standard SFP APIs; the approval text says so.

## Pipeline

1. **`bootstrap_sap_artifacts()`** — once per SAP system. Pushes
   `ZCL_FP_FORM_BUILDER` and the RFC wrapper `Z_FP_FORM_DEPLOY` into
   package `$TMP` (local, never transported — this is tooling, not a
   deliverable). Idempotent.
2. **`parse_fsd(fsd_path)`** — extracts candidate fields from the FSD,
   tagged `HEADER`/`ITEM`/`None` by a heading-keyword heuristic, with a
   best-effort `typename_guess`. Review before using — it's a starting
   point, not ground truth.
3. **`parse_pdf_layout(pdf_path)`** — per-page text blocks with mm
   positions and `ltr`/`rtl` direction.
4. **Agent reasoning step (not a tool call)** — pair FSD fields to PDF
   regions. This is deliberately not automated blindly: see
   "Why the field↔position mapping isn't fully automatic" below.
5. **`ensure_form_types(...)`** — only for header+items forms. Auto-creates
   `<FORM>_HDR_S` / `<FORM>_ITM_S` / `<FORM>_ITM_T` in your real target
   package/transport (not `$TMP`), returns the `GS_HEADER`/`GT_ITEMS`
   field entries to fold into the interface's field list.
6. **`generate_xdp(...)`** — deterministic XFA rendering from the mapping
   assembled in step 4 (+ step 5's compound fields for header+items).
7. **Show the user the preview** returned by `generate_xdp` (and, for
   header+items, the `ensure_form_types` result) and get explicit
   approval — this is a real, hard-to-reverse change to a shared SAP
   system.
8. **`deploy_form(..., confirmed=True)`** — only after that approval.

## Header + items example

```python
header_types = ensure_form_types(
    form_name="ZSO_CONF",
    package="ZSD_FORMS", transport="DEVK900555",
    header_fields=[{"name": "IV_ORDER_NO", "typename": "abap.string"},
                    {"name": "IV_CUSTOMER", "typename": "abap.string"}],
    item_fields=[{"name": "IV_MATERIAL", "typename": "abap.string"},
                 {"name": "IV_QTY", "typename": "abap.string"},
                 {"name": "IV_PRICE", "typename": "abap.string"}],
)
# header_types["fields"] == [{"name":"GS_HEADER","typing":"TYPE","typename":"ZSO_CONF_HDR_S"},
#                             {"name":"GT_ITEMS","typing":"TYPE","typename":"ZSO_CONF_ITM_T"}]

xdp = generate_xdp(form_name="ZSO_CONF", locale="en_US", pages=[{
    "width_mm": 210, "height_mm": 297,
    "elements": [
        {"kind": "draw", "name": "D_Title", "x": 15, "y": 10, "w": 100, "h": 8, "text": "Order Confirmation", "bold": True},
        {"kind": "field", "name": "F_OrderNo", "x": 15, "y": 20, "w": 60, "h": 7, "data_ref": "$record.GS_HEADER.IV_ORDER_NO"},
        {"kind": "field", "name": "F_Customer", "x": 80, "y": 20, "w": 100, "h": 7, "data_ref": "$record.GS_HEADER.IV_CUSTOMER"},
    ],
    "items_table": {
        "name": "Items", "x": 15, "y": 40, "w": 180,
        "data_ref": "$record.GT_ITEMS.ITEM",
        "columns": [
            {"name": "IV_MATERIAL", "label": "Material", "w": 80},
            {"name": "IV_QTY", "label": "Qty", "w": 40, "align": "center"},
            {"name": "IV_PRICE", "label": "Price", "w": 60, "align": "right"},
        ],
    },
}])

deploy_form(
    interface_name="ZSO_CONF_I01", form_name="ZSO_CONF_F01", package="ZSD_FORMS",
    fields=header_types["fields"], xdp_path=xdp["xdp_path"],
    transport="DEVK900555", confirmed=True,  # only after showing the user xdp["preview"]
)
```

## Why the field↔position mapping isn't fully automatic

Pairing an FSD field ("Customer Name") to a specific blank region on a PDF
page, with no anchors, is a form-understanding problem — coordinate
extraction alone tells you *where things are*, not *what belongs where*.
`parse_fsd` and `parse_pdf_layout` give the calling agent (an LLM, well
suited to exactly this kind of semantic pairing) everything needed to
decide that mapping; `generate_xdp` then renders it deterministically, so
the output is always valid XFA regardless of how the mapping was chosen.
The one step that stays "AI/human-in-the-loop" is the pairing itself, and
the confirmation gate before `deploy_form` — not the rest of the pipeline.

## Things to verify on your own system before relying on this in production

- **Function module / DDIC creation via ADT** (`adt_client.py`'s
  `create_fmodule`, `create_structure`, `create_tabletype`): the core
  class-creation flow (CSRF, lock/source/activate) is a well-established
  ADT pattern; the exact content-type/attribute names for function
  modules and DDL-style structures vary more across NetWeaver releases and
  aren't verified against a live system here. If `bootstrap_sap_artifacts`
  or `ensure_form_types` fails, the error message includes the rejected
  request — the fixes are usually a one-line content-type/attribute
  adjustment, not a redesign. Error messages also name the one-time manual
  fallback (e.g. flipping "Remote-Enabled Module" in SE37) where relevant
  — that's a checkbox toggle, not writing code.
- **Item row element name** (`$record.GT_ITEMS.ITEM`): the common SAP
  convention for how a TABLE interface parameter's rows are named in the
  generated XFA schema. Confirm via SFP's Context tab after
  `bootstrap`/`ensure_form_types` create the interface, and pass a
  different `data_ref` to `generate_xdp` if your system differs.
- **`/sap/bc/adt` and `/sap/bc/soap/rfc` must be active** in SICF on the
  target system. Both are standard SAP-delivered services (not something
  this server creates), but some systems have them deactivated by policy
  — that's a Basis activation, not a code change.

## Tests

```bash
pytest tests/
```

`test_xdp_generator.py` checks structural well-formedness and that
header/items forms produce the expected repeating-row/bind shape.
`test_pdf_layout_parser.py` checks mm conversion and RTL detection against
a small generated test PDF. Neither test suite touches a real SAP system —
see "things to verify" above for what still needs a live-system check.
