"""
Calls Z_FP_FORM_DEPLOY over SAP's standard, pre-existing SOAP-RFC gateway
(/sap/bc/soap/rfc) -- a SAP-delivered ICF service present on virtually
every ABAP system, so unlike the function module itself, nothing here
needs to be registered/created. Plain HTTP + hand-built SOAP envelope via
`requests`; no NetWeaver RFC SDK / pyrfc dependency.
"""
from __future__ import annotations

import base64
from xml.sax.saxutils import escape

from lxml import etree

import fm_definitions
import guards
import sap_transport


class SoapRfcError(Exception):
    pass


class SoapRfcClient:
    def __init__(self, gate, cfg: "sap_transport.HttpConfig" = None, session=None):
        self.gate = gate
        self.cfg = cfg or sap_transport.HttpConfig.from_env()
        self.session = session or sap_transport.make_session(self.cfg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _endpoint(self):
        url = f"{self.cfg.base_url}/sap/bc/soap/rfc"
        if self.cfg.client:
            url += f"?sap-client={self.cfg.client}"
        return url

    def call_deploy(self, mode, interface_name, form_name, devclass,
                     fields, ordernum=None, xdp_xstring: bytes = None):
        """
        mode: 'CHECK' or 'DEPLOY'
        fields: list of {"name","typing","typename"} -- flat top-level
                interface parameters. A header/items form just includes
                entries like {"name":"GS_HEADER","typing":"TYPE","typename":"ZFOO_HDR_S"}
                alongside (or instead of) plain scalar fields.
        xdp_xstring: raw bytes of the XDP layout (base64-encoded on the wire).
        Returns dict: interface_exists, form_exists, subrc, message.
        """
        interface_name, form_name, devclass = guards.validate_deploy_args(
            mode, interface_name, form_name, devclass, fields)
        guards.assert_allowed_soap_path(guards.SOAP_RFC_PATH)

        rows_xml = "".join(
            "<item>"
            f"<NAME>{escape(f['name'])}</NAME>"
            f"<TYPING>{escape(f.get('typing', 'TYPE'))}</TYPING>"
            f"<TYPENAME>{escape(f['typename'])}</TYPENAME>"
            "</item>"
            for f in fields
        )
        xdp_b64 = base64.b64encode(xdp_xstring).decode("ascii") if xdp_xstring else ""

        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
  <soap-env:Body>
    <urn:{fm_definitions.FUNCTION_MODULE} xmlns:urn="urn:sap-com:document:sap:rfc:functions">
      <IV_MODE>{escape(mode)}</IV_MODE>
      <IV_INTERFACE_NAME>{escape(interface_name)}</IV_INTERFACE_NAME>
      <IV_FORM_NAME>{escape(form_name)}</IV_FORM_NAME>
      <IV_DEVCLASS>{escape(devclass)}</IV_DEVCLASS>
      <IV_ORDERNUM>{escape(ordernum or '')}</IV_ORDERNUM>
      <IV_XDP_XSTRING>{xdp_b64}</IV_XDP_XSTRING>
      <IT_FIELDS>{rows_xml}</IT_FIELDS>
    </urn:{fm_definitions.FUNCTION_MODULE}>
  </soap-env:Body>
</soap-env:Envelope>"""

        if mode == "CHECK":
            self.gate.check("POST", guards.SOAP_RFC_PATH, kind="READ",
                            purpose=f"Read-only check whether interface {interface_name} / form {form_name} exist",
                            detail="calls Z_FP_FORM_DEPLOY in CHECK mode: SELECTs from TADIR only, changes nothing")
        else:
            self.gate.check("POST", guards.SOAP_RFC_PATH, kind="WRITE",
                            purpose=f"CREATE and ACTIVATE Adobe interface {interface_name} and form {form_name}",
                            detail=(f"package={devclass}, transport={ordernum or 'none'}, "
                                    f"{len(fields)} interface field(s), layout {len(xdp_xstring or b'')} bytes; "
                                    f"one call, performs several steps inside SAP via the standard SFP APIs"))

        resp = self.session.post(
            self._endpoint(),
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
        )
        if resp.status_code >= 400:
            raise SoapRfcError(
                f"SOAP-RFC call failed: HTTP {resp.status_code}\n{resp.text[:1500]}")

        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as e:
            raise SoapRfcError(f"Could not parse SOAP response: {e}\n{resp.text[:1500]}")

        fault = root.find(".//{*}Fault")
        if fault is not None:
            fault_text = etree.tostring(fault, pretty_print=True).decode("utf-8", "replace")
            raise SoapRfcError(f"SAP returned a SOAP fault:\n{fault_text[:1500]}")

        def text_of(local_name, default=""):
            el = root.find(f".//*[local-name()='{local_name}']")
            return el.text if el is not None and el.text is not None else default

        return {
            "interface_exists": text_of("EV_INTERFACE_EXISTS") in ("X", "true", "1"),
            "form_exists": text_of("EV_FORM_EXISTS") in ("X", "true", "1"),
            "subrc": int(text_of("EV_SUBRC", "0") or "0"),
            "message": text_of("EV_MESSAGE"),
        }

    def check_status(self, interface_name, form_name):
        return self.call_deploy(mode="CHECK", interface_name=interface_name,
                                 form_name=form_name, devclass="", fields=[])

    def deploy(self, interface_name, form_name, devclass, fields,
               ordernum=None, xdp_xstring: bytes = None):
        result = self.call_deploy(mode="DEPLOY", interface_name=interface_name,
                                   form_name=form_name, devclass=devclass,
                                   fields=fields, ordernum=ordernum, xdp_xstring=xdp_xstring)
        if result["subrc"] != 0:
            raise SoapRfcError(f"Deploy failed (subrc={result['subrc']}): {result['message']}")
        return result
