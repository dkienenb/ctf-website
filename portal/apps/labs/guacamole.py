"""Signed AND encrypted Guacamole grants (handoff §16): short-lived, scoped to
exactly one connection — the player's own active lab attacker desktop.

Implements the guacamole-auth-json format exactly:
1. JSON payload (username, expires, connections)
2. Sign with HMAC/SHA-256 using the shared 128-bit key; prepend the
   signature to the plaintext JSON
3. Encrypt with AES-128-CBC, IV = all zero bytes, PKCS7 padding
4. Base64-encode; submit to Guacamole as the `data` query parameter

The portal never issues anything beyond the active lab's VNC endpoint;
players cannot supply hosts, ports, or protocols.
"""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from django.conf import settings

from .services import vnc_port_for

import re
from django.conf import settings

GRANT_TTL_MS = 5 * 60 * 1000
GUAC_ORIGIN = "/guacamole/"  # default single-instance path
ZERO_IV = b"\x00" * 16


def _host_slug(host_name):
    """URL-safe path segment for a host name (the routing key)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(host_name).lower()).strip("-")
    return slug or "host"


def instance_base_path(host_name):
    """The web path of the Guacamole instance serving THIS host's desktops
    (Path A: one instance per workhorse, each pinned to that host's guacd).

    Single-instance deployments (GUACAMOLE_INSTANCE_PATHS unset) serve every
    host at /guacamole/. Multi-instance deployments map host → path; an
    unmapped host is an operator configuration gap, refused with instructions
    rather than routed to the wrong guacd."""
    mapping = getattr(settings, "GUACAMOLE_INSTANCE_PATHS", {}) or {}
    if not mapping:
        return GUAC_ORIGIN
    path = mapping.get(_host_slug(host_name))
    if not path:
        raise GrantError(
            f"No Guacamole instance is configured for workhorse '{host_name}'. "
            "Add a compose service (GUACD_HOSTNAME = that host's management IP, "
            "WEBAPP_CONTEXT = its route path), a Caddy route, and a "
            "GUACAMOLE_INSTANCE_PATHS entry — see agent/ADD-A-WORKHORSE.md."
        )
    return path if path.startswith("/") else f"/{path}"


class GrantError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _json_secret():
    secret = settings.GUACAMOLE_JSON_SECRET
    if not secret:
        raise GrantError("Guacamole JSON secret is not configured.")
    try:
        key = bytes.fromhex(secret)
    except ValueError:
        raise GrantError("GUACAMOLE_JSON_SECRET must be 32 hex characters (128 bits).")
    if len(key) != 16:
        raise GrantError("GUACAMOLE_JSON_SECRET must be exactly 128 bits (32 hex characters).")
    return key


def _sign_and_encrypt(payload_bytes, key):
    signature = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    padder = PKCS7(128).padder()
    padded = padder.update(signature + payload_bytes) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(ZERO_IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _client_url_identifier(connection_id):
    """Build the SPA's 1.6.0 client-URL identifier for a connection."""
    raw = f"{connection_id}\x00c\x00json".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def issue_grant_flow(user, lab, *, vnc_port=None):
    """Grant parts for the console bridge page (the browser completes the
    JSON handshake itself — see `issue_grant` for why)."""
    if not lab.host or not lab.host.management_ip:
        raise GrantError("The lab's workhorse has not reported its management address yet.")
    vnc_port = str(vnc_port if vnc_port is not None else vnc_port_for(lab))
    return _grant_parts(user, f"lab-{lab.id}", "127.0.0.1", vnc_port, lab.host.name)


def issue_build_grant_flow(user, build):
    """Grant parts for a template-build guest's console bridge page."""
    if not build.host or not build.host.management_ip:
        raise GrantError("The build's workhorse has not reported its management address yet.")
    if not build.vnc_port:
        raise GrantError("The build guest is not booted yet (no console port assigned).")
    return _grant_parts(user, f"build-{build.id}", "127.0.0.1", str(build.vnc_port), build.host.name)


def issue_grant(user, lab, *, vnc_port=None):
    """A grant valid five minutes, permitting only this lab's attacker desktop.

    The webapp's guacd dial target is set deployment-wide via GUACD_HOSTNAME
    (compose: the guacd co-located on the lab's workhorse) — Guacamole 1.6.0
    has no per-connection guacd routing, so no guacd parameters are sent
    here. That guacd reaches the guest's VNC over the workhorse's loopback.
    All destinations are server-derived from the lab record.

    Returns the deep-link URL (client identifier + `data` parameter). Views
    should prefer `issue_grant_flow` + the console bridge page: navigating
    straight here makes the SPA re-POST its PREVIOUS auth token alongside
    the grant, and Guacamole's cached-token provider then wins, silently
    discarding the grant's temporary connections ("connection does not
    exist"). The bridge POSTs the grant alone, so the JSON provider always
    sees it."""
    flow = issue_grant_flow(user, lab, vnc_port=vnc_port)
    return f'{flow["client_url"]}?data={quote(flow["data"])}'


def issue_build_grant(user, build):
    """Deep-link grant URL for an interactive template-build guest's VNC
    (attacker-role builds; targets are provisioned over SSH and have no
    console). See `issue_grant` for the cached-token caveat — views should
    prefer `issue_build_grant_flow`."""
    flow = issue_build_grant_flow(user, build)
    return f'{flow["client_url"]}?data={quote(flow["data"])}'


def _grant_parts(user, connection_id, vnc_host, vnc_port, host_name):
    """Build one grant's payload plus the URLs the console bridge page needs:

    data       — the signed+encrypted grant (base64; the `data` parameter)
    auth_url   — the Guacamole instance's token endpoint (POST the grant here)
    client_url — the SPA deep link WITHOUT the data parameter (navigate here
                 once the returned authToken is stored)"""
    payload = {
        "username": user.discord_id,
        "expires": int(time.time() * 1000) + GRANT_TTL_MS,
        "connections": {
            connection_id: {
                "id": connection_id,
                "protocol": "vnc",
                "parameters": {
                    "hostname": vnc_host,
                    "port": str(vnc_port),
                },
            }
        },
    }
    key = _json_secret()
    data = base64.b64encode(_sign_and_encrypt(json.dumps(payload).encode(), key)).decode()
    # Deep-link into the client for this connection. The SPA consumes the
    # `data` parameter from WITHIN the URL fragment (Angular hashbang
    # routing). As of Guacamole 1.6.0, client URLs address connections via a
    # base64url-encoded identifier of the form "<id>\0<type>\0<dataSource>"
    # (type "c" = connection) — see ClientIdentifier.toString in the SPA.
    # The older flat "<dataSource>.<connectionId>" form decodes into garbage
    # and the webapp kills the tunnel with "Illegal identifier - unknown
    # type" before it ever dials guacd.
    fragment_id = _client_url_identifier(connection_id)
    base = instance_base_path(host_name)
    return {
        "data": data,
        "auth_url": f"{base}api/tokens",
        "client_url": f"{base}#/client/{fragment_id}",
    }
