#!/usr/bin/env python3
"""Mint CDP JWT auth headers for the x402 facilitator, grouped per route.

Prints the grouped auth-header JSON that tools/x402_verifier.py's
``X402_FACILITATOR_HEADERS_COMMAND`` seam consumes:

    {"verify":    {"Authorization": "Bearer <jwt>"},
     "settle":    {"Authorization": "Bearer <jwt>"},
     "supported": {"Authorization": "Bearer <jwt>"}}

One JWT per route because the official x402 client calls ``create_headers()``
without route context and picks its group per HTTP call (see
``x402.http.facilitator_client_base.CreateHeadersAuthProvider``), while CDP
binds each token to one route via the ``uri`` claim ("METHOD host path") —
so a token minted for /verify cannot authenticate /settle.

Header/claim contract per CDP's official authentication guide
(docs.cdp.coinbase.com/api-reference/v2/authentication, read 2026-08-21):

    header: {alg: EdDSA, typ: JWT, kid: <api key id>, nonce: <random>}
    claims: {sub: <api key id>, iss: "cdp", aud: ["cdp_service"],
             nbf: now-1, exp: now + expires_in, uri: "METHOD host path"}

Key material comes from the environment, never from this file:

    CDP_API_KEY_ID       the CDP key's id (the ``kid``)
    CDP_API_KEY_SECRET   base64 of a 64-byte Ed25519 key (32-byte seed
                         || 32-byte public key); only the seed signs
    CDP_JWT_HOST         optional, default api.cdp.coinbase.com
    CDP_JWT_EXPIRES_IN   optional token TTL seconds, default 120

stdout carries ONLY the grouped JSON (the verifier parses it as such);
diagnostics and errors go to stderr; any failure exits nonzero with
nothing useful on stdout — fail closed, never send unauthenticated.

Host install:  pip install pyjwt cryptography      (no cdp-sdk needed)
Self-test:     python3 mint_cdp_jwt.py --check     (validates env, key shape,
               and the minted claim/header structure; never prints tokens)
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_HOST = "api.cdp.coinbase.com"
DEFAULT_TTL = 120

# (group, method, path) — the three x402 facilitator routes the official
# client can call with auth headers (facilitator_client.py: /verify,
# /settle, /supported).
ROUTES = (
    ("verify", "POST", "/platform/v2/x402/verify"),
    ("settle", "POST", "/platform/v2/x402/settle"),
    ("supported", "GET", "/platform/v2/x402/supported"),
)


class MintError(Exception):
    """Any failure that must abort with a nonzero exit and clean stdout."""


def load_signing_key(secret_b64: str) -> Ed25519PrivateKey:
    """Build the Ed25519 signing key from CDP's base64 secret.

    CDP secret format: base64(64 bytes) = 32-byte seed || 32-byte public
    key. Only the seed is the private key (official Ruby/PHP examples
    decode and slice exactly this way).
    """
    try:
        decoded = base64.b64decode(secret_b64, validate=True)
    except Exception as exc:  # binascii.Error / ValueError
        raise MintError(f"CDP_API_KEY_SECRET is not valid base64: {exc}") from exc
    if len(decoded) != 64:
        raise MintError(
            f"CDP_API_KEY_SECRET must decode to 64 bytes (seed||public), "
            f"got {len(decoded)}"
        )
    return Ed25519PrivateKey.from_private_bytes(decoded[:32])


def mint_token(
    key: Ed25519PrivateKey,
    key_id: str,
    method: str,
    path: str,
    host: str = DEFAULT_HOST,
    expires_in: int = DEFAULT_TTL,
    now: int | None = None,
) -> str:
    """Mint one route-bound CDP JWT exactly per the official contract."""
    ts_now = int(time.time()) if now is None else int(now)
    header = {
        "alg": "EdDSA",
        "typ": "JWT",
        "kid": key_id,
        "nonce": secrets.token_hex(16),
    }
    claims = {
        "sub": key_id,
        "iss": "cdp",
        "aud": ["cdp_service"],
        # 1s back-dated nbf absorbs small clock skew; exp stays exact.
        "nbf": ts_now - 1,
        "exp": ts_now + int(expires_in),
        "uri": f"{method.upper()} {host}{path}",
    }
    return pyjwt.encode(claims, key, algorithm="EdDSA", headers=header)


def build_grouped_headers(
    key_id: str,
    secret_b64: str,
    host: str = DEFAULT_HOST,
    expires_in: int = DEFAULT_TTL,
) -> dict:
    """Mint all route tokens and return the grouped header JSON dict."""
    key = load_signing_key(secret_b64)
    grouped = {}
    for group, method, path in ROUTES:
        token = mint_token(key, key_id, method, path, host, expires_in)
        grouped[group] = {"Authorization": f"Bearer {token}"}
    return grouped


def _self_check(key_id: str, secret_b64: str, host: str, expires_in: int) -> str:
    """Mint, then decode-and-verify every token; return a diagnostic line.

    Verifies signatures against the public key derived from the seed, so a
    wrong seed/public split fails here and not on the first paid call.
    Never prints tokens or key material.
    """
    grouped = build_grouped_headers(key_id, secret_b64, host, expires_in)
    key = load_signing_key(secret_b64)
    public_key = key.public_key()
    routes = {group: f"{method} {host}{path}" for group, method, path in ROUTES}
    for group, headers in grouped.items():
        token = headers["Authorization"].split(" ", 1)[1]
        decoded = pyjwt.decode(
            token, public_key, algorithms=["EdDSA"], audience="cdp_service"
        )
        if decoded.get("uri") != routes[group]:
            raise MintError(f"self-check: {group} token uri claim mismatch")
        if decoded.get("iss") != "cdp" or decoded.get("sub") != key_id:
            raise MintError(f"self-check: {group} token iss/sub mismatch")
        if decoded.get("exp", 0) - decoded.get("nbf", 0) != int(expires_in) + 1:
            raise MintError(f"self-check: {group} token ttl mismatch")
    return (
        f"OK: minted and signature-verified {len(grouped)} route-bound JWTs "
        f"(kid={key_id}, host={host}, ttl={expires_in}s)"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_mode = "--check" in argv
    key_id = os.environ.get("CDP_API_KEY_ID", "").strip()
    secret_b64 = os.environ.get("CDP_API_KEY_SECRET", "").strip()
    host = os.environ.get("CDP_JWT_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    ttl_raw = os.environ.get("CDP_JWT_EXPIRES_IN", "").strip()
    try:
        expires_in = int(ttl_raw) if ttl_raw else DEFAULT_TTL
    except ValueError:
        print("mint_cdp_jwt: CDP_JWT_EXPIRES_IN must be an integer", file=sys.stderr)
        return 2

    missing = [
        name
        for name, val in (("CDP_API_KEY_ID", key_id), ("CDP_API_KEY_SECRET", secret_b64))
        if not val
    ]
    if missing:
        print(
            f"mint_cdp_jwt: missing environment variable(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    try:
        if check_mode:
            print(_self_check(key_id, secret_b64, host, expires_in), file=sys.stderr)
            return 0
        grouped = build_grouped_headers(key_id, secret_b64, host, expires_in)
    except MintError as exc:
        print(f"mint_cdp_jwt: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # unexpected — still fail closed, clean stdout
        print(f"mint_cdp_jwt: unexpected failure: {exc}", file=sys.stderr)
        return 2

    json.dump(grouped, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
