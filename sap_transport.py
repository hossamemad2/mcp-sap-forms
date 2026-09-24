"""
Connectivity layer shared by every SAP client in this server.

Two transports, chosen with SAP_TRANSPORT (default "http"):

  http : ADT REST + SOAP-RFC over HTTP(S). Works directly or over a VPN that
         is already up on the machine running this server. Hardened with a
         separate connect timeout, TLS options and clear error messages.
         SAProuter is NOT supported here (requests can't speak the router
         protocol); if the router only lets RFC through, use "rfc".
  rfc  : native RFC via pyrfc + the SAP NetWeaver RFC SDK. SAProuter is
         handled natively by the SDK (SAP_ROUTER_STRING). VPN = just set the
         application server host and leave the router string empty.

Every network action still goes through guards.py and the per-call approval
gate (sap_gate.py) in the clients; nothing here connects on its own.
Router strings may contain a password (/P/...): it is never logged, echoed
or included in error text (see scrub / describe_route).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SapConnectionError(Exception):
    pass


# ------------------------------------------------------------ router string

@dataclass(frozen=True)
class Hop:
    host: str
    port: int = 3299
    password: str = field(default="", repr=False)


_HOP_RE = re.compile(r"/H/(?P<host>[A-Za-z0-9._\-]+)(?:/S/(?P<port>\d{1,5}))?(?:/P/(?P<pw>[^/\s]+))?")


def parse_router_string(route: str) -> list:
    """SAProuter hops: /H/host[/S/port][/P/password] repeated. Default port 3299."""
    s = (route or "").strip()
    if not s:
        raise SapConnectionError("SAP_ROUTER_STRING is empty.")
    hops, pos = [], 0
    while pos < len(s):
        m = _HOP_RE.match(s, pos)
        if not m:
            raise SapConnectionError(
                f"SAP_ROUTER_STRING is malformed near character {pos + 1} "
                f"(expected /H/host[/S/port][/P/password] hops).")
        port = int(m.group("port") or 3299)
        if not 1 <= port <= 65535:
            raise SapConnectionError("SAP_ROUTER_STRING contains an invalid port.")
        hops.append(Hop(m.group("host"), port, m.group("pw") or ""))
        pos = m.end()
    return hops


def describe_route(hops) -> str:
    text = "".join(f"/H/{h.host}/S/{h.port}" for h in hops)
    return text + (" (password hidden)" if any(h.password for h in hops) else "")


def scrub(text: str, hops) -> str:
    for h in hops:
        if h.password:
            text = text.replace(h.password, "***")
    return text


# ------------------------------------------------------------------- config

def _get(env, name, required=False) -> str:
    val = (env.get(name) or "").strip()
    if required and not val:
        raise SapConnectionError(f"Missing required environment variable {name}.")
    return val


def _get_bool(env, name, default) -> bool:
    val = _get(env, name)
    return default if not val else val.lower() not in ("0", "false", "no", "off")


def _get_float(env, name, default) -> float:
    val = _get(env, name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        raise SapConnectionError(f"{name} must be a number of seconds.")


def transport_kind(env=None) -> str:
    env = os.environ if env is None else env
    kind = (_get(env, "SAP_TRANSPORT") or "http").lower()
    if kind not in ("http", "rfc"):
        raise SapConnectionError("SAP_TRANSPORT must be 'http' or 'rfc'.")
    return kind


@dataclass(frozen=True)
class HttpConfig:
    base_url: str
    user: str
    password: str = field(repr=False)
    client: str = ""
    ca_bundle: str = ""
    verify_ssl: bool = True
    tls_server_name: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 120.0

    @classmethod
    def from_env(cls, env=None) -> "HttpConfig":
        env = os.environ if env is None else env
        if _get(env, "SAP_ROUTER_STRING"):
            raise SapConnectionError(
                "SAP_ROUTER_STRING is set but SAP_TRANSPORT=http can't use a SAProuter. "
                "Set SAP_TRANSPORT=rfc (native RFC handles SAProuter), or run your own "
                "port-forward and point SAP_BASE_URL at it with SAP_TLS_SERVER_NAME set.")
        base_url = _get(env, "SAP_BASE_URL", required=True).rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SapConnectionError("SAP_BASE_URL must look like https://host:port")
        ca = _get(env, "SAP_CA_BUNDLE")
        if ca and not os.path.isfile(ca):
            raise SapConnectionError("SAP_CA_BUNDLE does not point to an existing file.")
        return cls(
            base_url=base_url,
            user=_get(env, "SAP_USER", required=True),
            password=_get(env, "SAP_PASSWORD", required=True),
            client=_get(env, "SAP_CLIENT"),
            ca_bundle=ca,
            verify_ssl=_get_bool(env, "SAP_VERIFY_SSL", True),
            tls_server_name=_get(env, "SAP_TLS_SERVER_NAME"),
            connect_timeout=_get_float(env, "SAP_TIMEOUT_CONNECT", 10.0),
            read_timeout=_get_float(env, "SAP_TIMEOUT_READ", 120.0),
        )


@dataclass(frozen=True)
class RfcConfig:
    ashost: str
    sysnr: str
    client: str
    user: str
    password: str = field(repr=False)
    router_string: str = field(default="", repr=False)
    lang: str = "EN"

    @classmethod
    def from_env(cls, env=None) -> "RfcConfig":
        env = os.environ if env is None else env
        sysnr = _get(env, "SAP_RFC_SYSNR", required=True)
        if not re.fullmatch(r"\d{2}", sysnr):
            raise SapConnectionError("SAP_RFC_SYSNR must be the 2-digit instance number, e.g. 00.")
        client = _get(env, "SAP_CLIENT", required=True)
        if not re.fullmatch(r"\d{3}", client):
            raise SapConnectionError("SAP_CLIENT must be the 3-digit client, e.g. 100.")
        router = _get(env, "SAP_ROUTER_STRING")
        if router:
            parse_router_string(router)  # validate early; raises on malformed
        return cls(
            ashost=_get(env, "SAP_RFC_ASHOST", required=True),
            sysnr=sysnr, client=client,
            user=_get(env, "SAP_USER", required=True),
            password=_get(env, "SAP_PASSWORD", required=True),
            router_string=router,
            lang=_get(env, "SAP_RFC_LANG") or "EN",
        )

    @property
    def hops(self) -> list:
        return parse_router_string(self.router_string) if self.router_string else []


def describe_http(cfg: HttpConfig) -> str:
    u = urlparse(cfg.base_url)
    text = f"{u.scheme}://{u.netloc}" + (f" client {cfg.client}" if cfg.client else "")
    return text + ("" if cfg.verify_ssl else " [TLS verification DISABLED]")


def describe_rfc(cfg: RfcConfig) -> str:
    text = f"RFC {cfg.ashost} sysnr {cfg.sysnr} client {cfg.client}"
    return text + (f" via SAProuter {describe_route(cfg.hops)}" if cfg.router_string else " (direct/VPN)")


def describe_current(env=None) -> str:
    try:
        if transport_kind(env) == "rfc":
            return describe_rfc(RfcConfig.from_env(env))
        return describe_http(HttpConfig.from_env(env))
    except SapConnectionError as e:
        return f"(configuration problem: {e})"


# --------------------------------------------------------------- HTTP session

def translate_http_error(e: Exception, cfg: HttpConfig) -> SapConnectionError:
    where = describe_http(cfg)
    detail = f"{type(e).__name__}: {str(e)[:300]}"
    if isinstance(e, requests.exceptions.SSLError):
        hint = ("TLS certificate problem. If the name doesn't match (you connect through a tunnel or alias) "
                "set SAP_TLS_SERVER_NAME to the certificate's hostname; if it is an internal CA set "
                "SAP_CA_BUNDLE to the CA file.")
    elif isinstance(e, requests.exceptions.ConnectTimeout):
        hint = (f"No connection within {cfg.connect_timeout:g}s. Is the VPN connected on THIS machine "
                f"(WSL/containers often don't inherit VPN routes)? Is a firewall dropping the port?")
    elif isinstance(e, requests.exceptions.ReadTimeout):
        hint = (f"Connected, but SAP didn't answer within {cfg.read_timeout:g}s. The request may still have "
                f"been processed: check with check_form_status before retrying; do not blindly retry.")
    elif isinstance(e, requests.exceptions.ConnectionError):
        hint = ("Connection refused or host not found. Does the hostname resolve while on the VPN "
                "(split-tunnel DNS)? Is SAP_BASE_URL host/port right? Is the ICM HTTP(S) port open?")
    else:
        hint = ""
    return SapConnectionError(f"Cannot talk to {where}: {hint} [{detail}]".replace("  ", " "))


class _SapAdapter(HTTPAdapter):
    def __init__(self, cfg: HttpConfig, **kwargs):
        self._cfg = cfg
        self._tls_name = cfg.tls_server_name or None
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        if self._tls_name and self._cfg.base_url.lower().startswith("https"):
            pool_kwargs["server_hostname"] = self._tls_name
            pool_kwargs["assert_hostname"] = self._tls_name
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def send(self, request, **kwargs):
        kwargs["timeout"] = (self._cfg.connect_timeout, self._cfg.read_timeout)
        try:
            return super().send(request, **kwargs)
        except requests.exceptions.RequestException as e:
            raise translate_http_error(e, self._cfg) from None


def make_session(cfg: HttpConfig) -> requests.Session:
    session = requests.Session()
    session.auth = (cfg.user, cfg.password)
    session.verify = cfg.ca_bundle if cfg.ca_bundle else cfg.verify_ssl
    # Retry only failures that happen before any bytes are sent, so a POST is never duplicated.
    retry = Retry(total=None, connect=2, read=0, status=0, other=0, backoff_factor=0.5, raise_on_status=False)
    adapter = _SapAdapter(cfg, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
