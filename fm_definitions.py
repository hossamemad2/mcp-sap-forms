"""
Single source of truth for the RFC-enabled function module Z_FP_FORM_DEPLOY.

Three things are generated from this one definition, so they can never drift
out of sync with each other:
  1. adt_client.py  -- the ADT metadata XML used to create the FM's signature
  2. soap_rfc_client.py -- the SOAP-RFC request/response envelope shape
  3. abap_src/z_fp_form_deploy_body.abap -- assumes exactly these names

If you change a parameter here, regenerate/re-push the FM via
bootstrap_sap_artifacts before calling deploy_form again.
"""

FUNCTION_GROUP = "ZFP_FORM_DEPLOY"
FUNCTION_MODULE = "Z_FP_FORM_DEPLOY"

# kind: 'IMPORTING' | 'EXPORTING'
# typing: 'TYPE'
# is_table: True -> deep/standard table parameter (fields list)
PARAMETERS = [
    {"name": "IV_MODE", "kind": "IMPORTING", "typing": "TYPE", "typename": "STRING",
     "doc": "'CHECK' (existence check only) or 'DEPLOY' (create + activate)"},
    {"name": "IV_INTERFACE_NAME", "kind": "IMPORTING", "typing": "TYPE", "typename": "FPNAME"},
    {"name": "IV_FORM_NAME", "kind": "IMPORTING", "typing": "TYPE", "typename": "FPNAME"},
    {"name": "IV_DEVCLASS", "kind": "IMPORTING", "typing": "TYPE", "typename": "DEVCLASS"},
    {"name": "IV_ORDERNUM", "kind": "IMPORTING", "typing": "TYPE", "typename": "TRKORR", "optional": True},
    # TFPIOPAR is a standard SAP-delivered table type (name/typing/typename),
    # already present on any system with Adobe Forms -- reusing it means
    # bootstrap never has to create a custom DDIC structure/table type.
    # Trade-off: the RFC entry point only supports a FLAT field list (no
    # PARENT/BEFORE nesting). ZCL_FP_FORM_BUILDER itself still supports
    # nested context nodes for callers that invoke it directly in ABAP --
    # this just isn't exposed through Z_FP_FORM_DEPLOY in v1.
    {"name": "IT_FIELDS", "kind": "IMPORTING", "typing": "TYPE", "typename": "TFPIOPAR",
     "is_table": True, "optional": True},
    {"name": "IV_XDP_XSTRING", "kind": "IMPORTING", "typing": "TYPE", "typename": "XSTRING", "optional": True},
    {"name": "EV_INTERFACE_EXISTS", "kind": "EXPORTING", "typing": "TYPE", "typename": "ABAP_BOOL"},
    {"name": "EV_FORM_EXISTS", "kind": "EXPORTING", "typing": "TYPE", "typename": "ABAP_BOOL"},
    {"name": "EV_SUBRC", "kind": "EXPORTING", "typing": "TYPE", "typename": "SY-SUBRC"},
    {"name": "EV_MESSAGE", "kind": "EXPORTING", "typing": "TYPE", "typename": "STRING"},
]

# TFPIOPAR's row type (FPIOPAR) fields, for reference by soap_rfc_client.py
# when building/parsing the SOAP table rows.
FIELD_ROW_STRUCTURE = [
    {"name": "NAME", "typename": "FPFIELD"},
    {"name": "TYPING", "typename": "CHAR4"},
    {"name": "TYPENAME", "typename": "STRING"},
]


def importing_params():
    return [p for p in PARAMETERS if p["kind"] == "IMPORTING" and not p.get("is_table")]


def exporting_params():
    return [p for p in PARAMETERS if p["kind"] == "EXPORTING"]


def table_params():
    return [p for p in PARAMETERS if p.get("is_table")]


def render_function_module_source() -> str:
    """
    Renders the full FUNCTION...ENDFUNCTION source, including the classic
    "special comment" interface block (the *"  lines between FUNCTION and
    the first dashed separator). This is how SE37/ADT's source-based editor
    represents a function module's signature as plain text -- activation
    re-derives the real interface metadata (FUPARAREF etc.) from it, so
    pushing this text via ADT's source/main PUT is sufficient; no separate
    "signature" API call is needed.

    NOTE: "Remote-Enabled Module" is a processing-type attribute, not part
    of this text block. adt_client.py must request it in the object's
    creation payload; if your system's ADT metadata schema doesn't accept
    that attribute the same way, this is the one piece to verify by hand
    (SE37 -> Attributes -> "Remote-Enabled Module" checkbox is a one-time
    toggle, not a code-writing step).
    """
    lines = ["FUNCTION z_fp_form_deploy.",
             '*"----------------------------------------------------------------------',
             '*"*"Local Interface:']

    imp = importing_params()
    if imp:
        lines.append('*"  IMPORTING')
        for p in imp:
            opt = " OPTIONAL" if p.get("optional") else ""
            lines.append(f'*"     VALUE({p["name"]}) TYPE  {p["typename"]}{opt}')

    exp = exporting_params()
    if exp:
        lines.append('*"  EXPORTING')
        for p in exp:
            lines.append(f'*"     VALUE({p["name"]}) TYPE  {p["typename"]}')

    tabs = table_params()
    if tabs:
        lines.append('*"  TABLES')
        for p in tabs:
            opt = " OPTIONAL" if p.get("optional") else ""
            lines.append(f'*"      {p["name"]} STRUCTURE  {p["typename"]}{opt}')

    lines.append('*"----------------------------------------------------------------------')
    lines.append("")
    lines.append(_FUNCTION_BODY.strip("\n"))
    lines.append("")
    lines.append("ENDFUNCTION.")
    return "\n".join(lines)


_FUNCTION_BODY = """
  DATA: lo_builder TYPE REF TO zcl_fp_form_builder,
        lx_error   TYPE REF TO cx_root,
        lt_fields  TYPE zcl_fp_form_builder=>ty_fields.

  CLEAR: ev_interface_exists, ev_form_exists, ev_subrc, ev_message.

  " Safety guard: custom (Z*/Y*) objects and packages only, known modes only.
  IF iv_mode <> 'CHECK' AND iv_mode <> 'DEPLOY'.
    ev_subrc   = 12.
    ev_message = 'Invalid mode'.
    RETURN.
  ENDIF.
  IF NOT ( iv_interface_name(1) CA 'ZY' AND iv_form_name(1) CA 'ZY' ).
    ev_subrc   = 12.
    ev_message = 'Only custom (Z*/Y*) interface and form names are allowed'.
    RETURN.
  ENDIF.
  IF iv_mode = 'DEPLOY' AND ( iv_devclass IS INITIAL OR NOT iv_devclass(1) CA 'ZY' ).
    ev_subrc   = 12.
    ev_message = 'Only custom (Z*/Y*) packages are allowed'.
    RETURN.
  ENDIF.

  ev_interface_exists = zcl_fp_form_builder=>interface_exists( iv_interface_name ).
  ev_form_exists      = zcl_fp_form_builder=>form_exists( iv_form_name ).

  IF iv_mode = 'CHECK'.
    RETURN.
  ENDIF.

  IF ev_interface_exists = abap_true.
    ev_subrc   = 4.
    ev_message = |Interface { iv_interface_name } already exists|.
    RETURN.
  ENDIF.
  IF ev_form_exists = abap_true.
    ev_subrc   = 4.
    ev_message = |Form { iv_form_name } already exists|.
    RETURN.
  ENDIF.

  LOOP AT it_fields INTO DATA(ls_field).
    APPEND VALUE #( name = ls_field-name typing = ls_field-typing
                     typename = ls_field-typename ) TO lt_fields.
  ENDLOOP.

  lo_builder = NEW zcl_fp_form_builder( ).

  TRY.
      lo_builder->create_interface(
        i_name     = iv_interface_name
        i_devclass = iv_devclass
        i_ordernum = iv_ordernum
        it_fields  = lt_fields ).

      lo_builder->create_form(
        i_name           = iv_form_name
        i_interface_name = iv_interface_name
        i_devclass       = iv_devclass
        i_ordernum       = iv_ordernum
        i_xdp_layout     = iv_xdp_xstring ).

      ev_subrc   = 0.
      ev_message = 'OK'.

    CATCH cx_root INTO lx_error.
      ev_subrc   = 8.
      ev_message = lx_error->get_text( ).
  ENDTRY.
"""
