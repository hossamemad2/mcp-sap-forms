import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import requests

import connectivity
import guards
import rfc_client
import sap_gate
import sap_transport as t

HTTP_ENV = {"SAP_BASE_URL": "https://sap.example:44300", "SAP_USER": "U", "SAP_PASSWORD": "pw"}
RFC_ENV = {"SAP_RFC_ASHOST": "sapdev", "SAP_RFC_SYSNR": "00", "SAP_CLIENT": "100",
           "SAP_USER": "U", "SAP_PASSWORD": "pw"}


# ---------------------------------------------------------------- router string

def test_route_parse_mask_and_scrub():
    hops = t.parse_router_string("/H/router.corp/S/3299/P/secret/H/other/S/3300")
    assert [(h.host, h.port) for h in hops] == [("router.corp", 3299), ("other", 3300)]
    text = t.describe_route(hops)
    assert "secret" not in text and "password hidden" in text
    assert t.scrub("bad secret here", hops) == "bad *** here"
    assert t.parse_router_string("/H/r")[0].port == 3299


@pytest.mark.parametrize("bad", ["", "router", "/H/", "/H/r/S/99999"])
def test_route_parse_rejects_malformed(bad):
    with pytest.raises(t.SapConnectionError):
        t.parse_router_string(bad)


# ------------------------------------------------------------------- config

def test_transport_kind():
    assert t.transport_kind({}) == "http"
    assert t.transport_kind({"SAP_TRANSPORT": "RFC"}) == "rfc"
    with pytest.raises(t.SapConnectionError):
        t.transport_kind({"SAP_TRANSPORT": "carrier-pigeon"})


def test_http_config_defaults_and_secret_not_in_repr():
    c = t.HttpConfig.from_env(dict(HTTP_ENV))
    assert c.verify_ssl and c.connect_timeout == 10.0 and c.read_timeout == 120.0
    assert "password" not in repr(c)
    assert "TLS verification DISABLED" in t.describe_http(
        t.HttpConfig.from_env({**HTTP_ENV, "SAP_VERIFY_SSL": "false"}))


def test_http_config_rejects_router_string_and_bad_url():
    with pytest.raises(t.SapConnectionError, match="SAProuter"):
        t.HttpConfig.from_env({**HTTP_ENV, "SAP_ROUTER_STRING": "/H/r/S/3299"})
    with pytest.raises(t.SapConnectionError):
        t.HttpConfig.from_env({**HTTP_ENV, "SAP_BASE_URL": "sap.example"})
    with pytest.raises(t.SapConnectionError, match="SAP_PASSWORD"):
        t.HttpConfig.from_env({k: v for k, v in HTTP_ENV.items() if k != "SAP_PASSWORD"})


def test_rfc_config_validation_and_description():
    c = t.RfcConfig.from_env({**RFC_ENV, "SAP_ROUTER_STRING": "/H/router.corp/S/3299/P/topsecret"})
    assert len(c.hops) == 1
    desc = t.describe_rfc(c)
    assert "topsecret" not in desc and "router.corp" in desc
    assert "direct/VPN" in t.describe_rfc(t.RfcConfig.from_env(dict(RFC_ENV)))
    with pytest.raises(t.SapConnectionError):
        t.RfcConfig.from_env({**RFC_ENV, "SAP_RFC_SYSNR": "0"})
    with pytest.raises(t.SapConnectionError):
        t.RfcConfig.from_env({**RFC_ENV, "SAP_CLIENT": "10"})
    with pytest.raises(t.SapConnectionError):
        t.RfcConfig.from_env({**RFC_ENV, "SAP_ROUTER_STRING": "not-a-route"})


# ------------------------------------------------------------ error hints

def test_http_error_translation_hints():
    cfg = t.HttpConfig.from_env(dict(HTTP_ENV))
    assert "VPN" in str(t.translate_http_error(requests.exceptions.ConnectTimeout("x"), cfg))
    assert "check_form_status" in str(t.translate_http_error(requests.exceptions.ReadTimeout("x"), cfg))
    assert "SAP_TLS_SERVER_NAME" in str(t.translate_http_error(requests.exceptions.SSLError("x"), cfg))
    assert "hostname" in str(t.translate_http_error(requests.exceptions.ConnectionError("x"), cfg)).lower()


def test_rfc_error_translation_never_leaks_router_password():
    cfg = t.RfcConfig.from_env({**RFC_ENV, "SAP_ROUTER_STRING": "/H/r/S/3299/P/topsecret"})
    comm = type("CommunicationError", (Exception,), {})("cannot reach /H/r/S/3299/P/topsecret")
    err = rfc_client.translate_rfc_error(comm, cfg)
    assert isinstance(err, t.SapConnectionError) and "topsecret" not in str(err) and "VPN" in str(err)
    logon = type("LogonError", (Exception,), {})("Name or password is incorrect")
    assert "SAP_USER" in str(rfc_client.translate_rfc_error(logon, cfg))
    abap = type("ABAPApplicationError", (Exception,), {})("boom")
    assert isinstance(rfc_client.translate_rfc_error(abap, cfg), rfc_client.RfcError)


# ------------------------------------------------------------- RFC client

class FakeConn:
    def __init__(self, result=None):
        self.calls, self.closed = [], False
        self.result = result or {"EV_INTERFACE_EXISTS": "X", "EV_FORM_EXISTS": "", "EV_SUBRC": 0, "EV_MESSAGE": ""}

    def call(self, fm, **kw):
        self.calls.append((fm, kw))
        return self.result

    def ping(self):
        pass

    def close(self):
        self.closed = True


def make_gate(answer=True):
    texts = []

    def approver(text):
        texts.append(text)
        return answer
    return sap_gate.ApprovalGate(approver, system="DEV"), texts


def test_rfc_asks_for_logon_then_for_the_call_and_closes():
    gate, texts = make_gate()
    conn = FakeConn()
    cfg = t.RfcConfig.from_env(dict(RFC_ENV))
    with rfc_client.RfcClient(gate, cfg, connection_factory=lambda: conn) as c:
        res = c.check_status("zi_test", "zf_test")
    assert len(texts) == 2 and "log on" in texts[0] and "Z_FP_FORM_DEPLOY" in texts[1]
    assert conn.calls[0][0] == "Z_FP_FORM_DEPLOY" and conn.calls[0][1]["IV_MODE"] == "CHECK"
    assert conn.calls[0][1]["IV_INTERFACE_NAME"] == "ZI_TEST"
    assert res["interface_exists"] is True and res["form_exists"] is False
    assert conn.closed


def test_rfc_logon_denied_means_no_connection_attempt():
    gate, texts = make_gate(answer=False)
    attempts = []
    cfg = t.RfcConfig.from_env(dict(RFC_ENV))
    with rfc_client.RfcClient(gate, cfg, connection_factory=lambda: attempts.append(1)) as c:
        with pytest.raises(sap_gate.SapCallDenied):
            c.check_status("ZI", "ZF")
    assert attempts == []


def test_rfc_guards_run_before_any_prompt_or_connection():
    gate, texts = make_gate()
    cfg = t.RfcConfig.from_env(dict(RFC_ENV))
    with rfc_client.RfcClient(gate, cfg, connection_factory=lambda: FakeConn()) as c:
        with pytest.raises(guards.GuardError):
            c.check_status("CL_STANDARD", "ZF")
        with pytest.raises(guards.GuardError):
            c.deploy("ZI", "ZF", "SAP", [], xdp_xstring=b"<x/>")
        c._conn = FakeConn()
        with pytest.raises(guards.GuardError):
            c._call("BAPI_USER_GET_DETAIL")
    assert texts == []


def test_rfc_deploy_maps_fields_and_reports_failure():
    gate, _ = make_gate()
    conn = FakeConn({"EV_INTERFACE_EXISTS": "", "EV_FORM_EXISTS": "", "EV_SUBRC": 0, "EV_MESSAGE": "OK"})
    cfg = t.RfcConfig.from_env(dict(RFC_ENV))
    fields = [{"name": "GS_HEADER", "typing": "TYPE", "typename": "ZFOO_HDR_S"}]
    with rfc_client.RfcClient(gate, cfg, connection_factory=lambda: conn) as c:
        c.deploy("ZI", "ZF", "ZPKG", fields, ordernum="DEVK900001", xdp_xstring=b"<x/>")
        kw = conn.calls[-1][1]
        assert kw["IT_FIELDS"] == [{"NAME": "GS_HEADER", "TYPING": "TYPE", "TYPENAME": "ZFOO_HDR_S"}]
        assert kw["IV_DEVCLASS"] == "ZPKG" and kw["IV_XDP_XSTRING"] == b"<x/>" and kw["IV_ORDERNUM"] == "DEVK900001"
        conn.result = {"EV_SUBRC": 12, "EV_MESSAGE": "Only custom (Z*/Y*)"}
        with pytest.raises(rfc_client.RfcError, match="subrc=12"):
            c.deploy("ZI", "ZF", "ZPKG", fields)


# --------------------------------------------------------------- diagnose

def test_diagnose_stops_at_config_without_touching_sap():
    gate, texts = make_gate()
    out = connectivity.diagnose(gate, env={})
    assert out["ok"] is False and out["first_failure"] == "config" and texts == []


def test_diagnose_stops_at_dns_without_asking_for_approval():
    gate, texts = make_gate()
    out = connectivity.diagnose(gate, env={**RFC_ENV, "SAP_RFC_ASHOST": "no-such-host.invalid"})
    assert out["first_failure"] == "dns" and texts == []
