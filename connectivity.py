"""
diagnose(): finds where the connection to SAP breaks, one rung at a time,
and stops at the first failure with a hint on what to fix.

Rungs: config -> DNS (local resolver, not a SAP call) -> TCP reach ->
logon/HTTP session. Every rung that touches the network is a SEPARATE call
that the user must approve (READ, "no data sent/changed"), same rule as all
other SAP traffic. A denial raises SapCallDenied and ends the run.
"""
from __future__ import annotations

import socket
from urllib.parse import urlparse

import adt_client
import rfc_client
import sap_transport
from sap_transport import SapConnectionError


def diagnose(gate, env=None) -> dict:
    steps = []

    def record(name, ok, detail="", hint=""):
        steps.append({"step": name, "ok": ok, "detail": detail, **({"hint": hint} if hint else {})})
        return ok

    def result():
        failed = next((s for s in steps if not s["ok"]), None)
        return {"ok": failed is None, "steps": steps, "first_failure": failed["step"] if failed else None}

    try:
        kind = sap_transport.transport_kind(env)
        cfg = sap_transport.RfcConfig.from_env(env) if kind == "rfc" else sap_transport.HttpConfig.from_env(env)
    except SapConnectionError as e:
        record("config", False, str(e), "Fix the environment variables listed in the README, then run again.")
        return result()
    record("config", True, f"transport={kind}: " + (
        sap_transport.describe_rfc(cfg) if kind == "rfc" else sap_transport.describe_http(cfg)))

    if kind == "rfc":
        hops = cfg.hops
        host, port = (hops[0].host, hops[0].port) if hops else (cfg.ashost, 3300 + int(cfg.sysnr))
        connect_timeout = 10.0
        first_hop = "SAProuter" if hops else "SAP gateway (port 33<sysnr>)"
    else:
        u = urlparse(cfg.base_url)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        connect_timeout = cfg.connect_timeout
        first_hop = "SAP HTTP(S) port"

    try:
        socket.getaddrinfo(host, port)
    except OSError as e:
        record("dns", False, f"'{host}' does not resolve ({e.__class__.__name__}).",
               "Connect the VPN on this machine (or fix split-tunnel DNS), or correct the hostname.")
        return result()
    record("dns", True, f"'{host}' resolves (local lookup, no SAP call)")

    gate.check("TCP", f"{host}:{port}", kind="READ",
               purpose=f"TCP reachability probe of the {first_hop}; the connection is closed at once, no data is sent")
    try:
        socket.create_connection((host, port), timeout=connect_timeout).close()
    except OSError as e:
        record("tcp", False, f"Cannot open a TCP connection to {host}:{port} ({e.__class__.__name__}).",
               "VPN down, wrong host/port, or a firewall / SAProuter route-permission table blocking it.")
        return result()
    record("tcp", True, f"TCP connection to {host}:{port} succeeded")

    try:
        if kind == "rfc":
            with rfc_client.RfcClient(gate, cfg) as client:
                client.ping()
            record("rfc_logon", True, "RFC logon and ping succeeded")
        else:
            adt_client.ADTClient(gate, cfg)._ensure_csrf()
            record("http_session", True, "TLS + ADT discovery request succeeded")
    except (SapConnectionError, rfc_client.RfcError, adt_client.ADTError) as e:
        record("rfc_logon" if kind == "rfc" else "http_session", False, str(e))
    return result()
