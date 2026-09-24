"""
Native RFC client for Z_FP_FORM_DEPLOY (SAP_TRANSPORT=rfc).

Uses pyrfc + the SAP NetWeaver RFC SDK. SAProuter is handled natively by
the SDK: SAP_ROUTER_STRING holds the router hops only (e.g.
/H/router.corp/S/3299), SAP_RFC_ASHOST the SAP application server behind
it. With no router string it connects directly (VPN case).

Same safety model as the HTTP clients: only the allow-listed function
module can be called (guards), names/packages are validated (guards), and
every step that talks to SAP -- the logon and each call -- is approved by
the user individually (sap_gate). pyrfc is imported lazily so the rest of
the server works without the SDK installed.

Note: this transport can only call the ALREADY-EXISTING wrapper
Z_FP_FORM_DEPLOY. It cannot create ABAP objects (that needs ADT/HTTP or the
one-time installer), by design: there is no safe generic "run this ABAP"
RFC and this server never uses one.
"""
from __future__ import annotations

import guards
import sap_transport
from sap_transport import RfcConfig, SapConnectionError

FUNCTION_MODULE = "Z_FP_FORM_DEPLOY"


class RfcError(Exception):
    pass


def translate_rfc_error(e: Exception, cfg: RfcConfig):
    hops = cfg.hops
    name = type(e).__name__
    text = sap_transport.scrub(str(e), hops)[:400]
    where = sap_transport.describe_rfc(cfg)
    if name == "LogonError":
        return SapConnectionError(
            f"Logon to {where} failed: check SAP_USER / SAP_PASSWORD / SAP_CLIENT and that the user "
            f"is not locked. [{name}: {text}]")
    if name in ("CommunicationError", "ExternalRuntimeError"):
        return SapConnectionError(
            f"Cannot reach {where}. Is the VPN connected on THIS machine? Are SAP_RFC_ASHOST / "
            f"SAP_RFC_SYSNR right (gateway port 33<sysnr>)? With a SAProuter: is the router reachable and "
            f"does its route permission table allow this connection? [{name}: {text}]")
    if name in ("ABAPApplicationError", "ABAPRuntimeError"):
        return RfcError(f"SAP raised an error in {FUNCTION_MODULE}: [{name}: {text}]")
    return RfcError(f"RFC call failed: [{name}: {text}]")


class RfcClient:
    def __init__(self, gate, cfg: RfcConfig = None, connection_factory=None):
        self.gate = gate
        self.cfg = cfg or RfcConfig.from_env()
        self._factory = connection_factory or self._pyrfc_connection
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _pyrfc_connection(self):
        try:
            from pyrfc import Connection
        except ImportError:
            raise SapConnectionError(
                "pyrfc is not installed (it needs the SAP NetWeaver RFC SDK from the SAP Support Portal). "
                "Install the SDK, set SAPNWRFC_HOME, then `pip install pyrfc`. Or use SAP_TRANSPORT=http.")
        params = dict(ashost=self.cfg.ashost, sysnr=self.cfg.sysnr, client=self.cfg.client,
                      user=self.cfg.user, passwd=self.cfg.password, lang=self.cfg.lang)
        if self.cfg.router_string:
            params["saprouter"] = self.cfg.router_string
        return Connection(**params)

    def _connect(self):
        if self._conn is not None:
            return
        self.gate.check(
            "RFC", "LOGON", kind="READ",
            purpose=f"Open an RFC session to {sap_transport.describe_rfc(self.cfg)} and log on as "
                    f"{self.cfg.user} (nothing is read or changed)",
            detail="the password and any router password are never shown or logged")
        try:
            self._conn = self._factory()
        except SapConnectionError:
            raise
        except Exception as e:
            raise translate_rfc_error(e, self.cfg) from None

    def _call(self, function: str, **params):
        guards.assert_allowed_rfc_function(function)
        try:
            return self._conn.call(function, **params)
        except Exception as e:
            raise translate_rfc_error(e, self.cfg) from None

    def ping(self):
        self._connect()
        self.gate.check("RFC", "PING", kind="READ",
                        purpose="RFC ping of the open session (no data read or changed)")
        try:
            self._conn.ping()
        except Exception as e:
            raise translate_rfc_error(e, self.cfg) from None

    def call_deploy(self, mode, interface_name, form_name, devclass, fields,
                    ordernum=None, xdp_xstring: bytes = None):
        interface_name, form_name, devclass = guards.validate_deploy_args(
            mode, interface_name, form_name, devclass, fields)
        self._connect()
        if mode == "CHECK":
            self.gate.check("RFC", FUNCTION_MODULE, kind="READ",
                            purpose=f"Read-only check whether interface {interface_name} / form {form_name} exist",
                            detail="calls Z_FP_FORM_DEPLOY in CHECK mode: SELECTs from TADIR only, changes nothing")
        else:
            self.gate.check("RFC", FUNCTION_MODULE, kind="WRITE",
                            purpose=f"CREATE and ACTIVATE Adobe interface {interface_name} and form {form_name}",
                            detail=(f"package={devclass}, transport={ordernum or 'none'}, {len(fields)} interface "
                                    f"field(s), layout {len(xdp_xstring or b'')} bytes; one call, performs several "
                                    f"steps inside SAP via the standard SFP APIs"))
        res = self._call(
            FUNCTION_MODULE,
            IV_MODE=mode, IV_INTERFACE_NAME=interface_name, IV_FORM_NAME=form_name,
            IV_DEVCLASS=devclass, IV_ORDERNUM=ordernum or "",
            IV_XDP_XSTRING=xdp_xstring or b"",
            IT_FIELDS=[{"NAME": f["name"], "TYPING": f.get("typing", "TYPE"), "TYPENAME": f["typename"]}
                       for f in fields],
        )
        return {
            "interface_exists": res.get("EV_INTERFACE_EXISTS") in ("X", True),
            "form_exists": res.get("EV_FORM_EXISTS") in ("X", True),
            "subrc": int(res.get("EV_SUBRC") or 0),
            "message": res.get("EV_MESSAGE") or "",
        }

    def check_status(self, interface_name, form_name):
        return self.call_deploy("CHECK", interface_name, form_name, "", [])

    def deploy(self, interface_name, form_name, devclass, fields, ordernum=None, xdp_xstring: bytes = None):
        result = self.call_deploy("DEPLOY", interface_name, form_name, devclass, fields,
                                  ordernum=ordernum, xdp_xstring=xdp_xstring)
        if result["subrc"] != 0:
            raise RfcError(f"Deploy failed (subrc={result['subrc']}): {result['message']}")
        return result
