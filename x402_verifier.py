#!/usr/bin/env python3
"""Official-package-backed x402 payment verifier for the diagnose endpoint.

Delegates EIP-3009 signature recovery, balance, and nonce checks to the
official ``x402`` package (x402-foundation) by calling its HTTP facilitator
client at the network boundary. This module never touches keys, never signs,
and never queries chains itself — verification and settlement are performed
by the facilitator service the operator configures.

Activation is explicit: the server entrypoint calls ``make_verifier`` with a
facilitator URL (normally from the ``X402_FACILITATOR_URL`` env var). With no
URL configured, ``make_verifier`` returns ``(None, {"mode": "fail_closed"})``
and the endpoint keeps answering paid retries with ``501
payment_not_verified`` — the same fail-closed behavior as before this module
existed.

Modes:
- verify_only (default): ``verify`` only; settlement is left to the operator
  (e.g. CDP's server wallet auto-settles verified requests).
- verify_and_settle (``X402_AUTO_SETTLE=1``): also calls ``settle`` after a
  passing verification and records the on-chain transaction hash.

Facilitator auth (verified against x402==2.20.0):
- The official client's dict-config path accepts ``create_headers``, wraps it
  in ``CreateHeadersAuthProvider``, and calls it ONCE PER HTTP REQUEST. The
    callable must return a GROUPED mapping ``{"verify": {...}, "settle":
  {...}, "supported": {...}}``; a flat header dict is silently dropped and
  requests go out unauthenticated.
- ``X402_FACILITATOR_HEADERS`` (static JSON) is wrapped into that grouped
  shape at startup — fine for facilitators with a long-lived static key.
- ``X402_FACILITATOR_HEADERS_COMMAND`` runs an argv command per request and
  parses its stdout as the grouped JSON. For CDP, whose auth is a JWT minted
  per-request from the API key pair, this is the correct seam: point it at a
  small mint script; the verifier never sees or stores key material beyond
  passing argv through subprocess. Command failure raises — fail closed,
  never send unauthenticated requests.

Requires: ``pip install "x402[all]"`` (the sync facilitator client needs
``httpx``).
"""
from __future__ import annotations

import json
import subprocess

import x402_endpoint


def build_requirements() -> dict:
    """Single source of truth for the paywall requirements.

    Delegates to the endpoint's builder so the 402 advertisement, the
    structural payload check, and the facilitator-bound verification all
    pin the identical price, asset, network, and payee.
    """
    return x402_endpoint._payment_requirements()


def _grouped_static_headers(flat_headers: dict) -> dict:
    """Wrap a flat header dict into the package's per-operation groups."""
    return {op: dict(flat_headers) for op in ("verify", "settle", "supported")}


def _command_create_headers(command: list):
    """Return a create_headers() that shells out per request and parses JSON."""

    def create_headers() -> dict:
        proc = subprocess.run(
            [str(arg) for arg in command],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"X402_FACILITATOR_HEADERS_COMMAND failed rc={proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}"
            )
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"X402_FACILITATOR_HEADERS_COMMAND printed invalid JSON: {exc}"
            )
        if not isinstance(parsed, dict):
            raise RuntimeError("X402_FACILITATOR_HEADERS_COMMAND must print a JSON object")
        return parsed

    return create_headers


def _client_config(facilitator_url: str, extra_headers=None, headers_command=None) -> dict:
    """Build the client config dict in the official package's own dialect.

    Uses the DICT form (not FacilitatorConfig kwargs): only the dict path
    understands ``create_headers``, and the real dataclass has no such field.
    """
    config = {"url": facilitator_url}
    if headers_command is not None:
        config["create_headers"] = _command_create_headers(headers_command)
    elif extra_headers:
        if callable(extra_headers):
            # Programmatic seam: caller supplies a grouped-shape callable that
            # is invoked once per request by the package's auth provider.
            config["create_headers"] = extra_headers
        else:
            flat = dict(extra_headers)
            config["create_headers"] = lambda: _grouped_static_headers(flat)
    return config


def make_verifier(
    facilitator_url,
    settle=False,
    client_factory=None,
    extra_headers=None,
    headers_command=None,
):
    """Return ``(verifier_callable_or_None, gate_info_dict)``.

    ``client_factory`` is an injection seam for tests (default builds the
    official ``HTTPFacilitatorClientSync``). With no facilitator URL the
    result is ``(None, {"mode": "fail_closed"})``.

    Auth precedence: ``headers_command`` > ``extra_headers`` > none. Passing
    both header sources raises ValueError (ambiguous deploy config).
    """
    if not facilitator_url:
        return None, {"mode": "fail_closed"}

    if extra_headers and headers_command:
        raise ValueError(
            "Set X402_FACILITATOR_HEADERS or X402_FACILITATOR_HEADERS_COMMAND, not both"
        )

    if client_factory is None:
        try:
            from x402.http.facilitator_client import HTTPFacilitatorClientSync
        except ImportError:
            raise RuntimeError(
                "X402_FACILITATOR_URL is set but the official x402 package is not "
                "installed. Run: pip install 'x402[all]'"
            )

        def client_factory(config_dict):
            return HTTPFacilitatorClientSync(config_dict)

    config_dict = _client_config(facilitator_url, extra_headers, headers_command)
    client = client_factory(config_dict)
    mode = "verify_and_settle" if settle else "verify_only"

    gate = {"mode": mode, "facilitator": facilitator_url}
    if headers_command is not None:
        gate["auth"] = "command"
        # Health-check seam without exposing secrets: run once now so a broken
        # mint script surfaces at boot, not on the first paid call.
        probe = _command_create_headers(headers_command)
        probe()
        gate["create_headers_probe"] = probe

    def verifier(payload: dict, requirements: dict) -> dict:
        payload_bytes = json.dumps(payload).encode()
        requirements_bytes = json.dumps(requirements).encode()
        try:
            verdict = client.verify_from_bytes(payload_bytes, requirements_bytes)
        except Exception as exc:  # facilitator outage must never look like a valid payment
            return {"verified": False, "reason": f"facilitator_error: {exc}"}

        if not getattr(verdict, "is_valid", False):
            return {
                "verified": False,
                "reason": getattr(verdict, "invalid_reason", None) or "verification_failed",
            }

        payer = getattr(verdict, "payer", None)
        transaction = None
        if settle:
            try:
                settlement = client.settle_from_bytes(payload_bytes, requirements_bytes)
            except Exception as exc:
                return {"verified": False, "reason": f"settlement_error: {exc}"}
            if not getattr(settlement, "success", False):
                return {
                    "verified": False,
                    "reason": getattr(settlement, "error_reason", None) or "settlement_failed",
                }
            transaction = getattr(settlement, "transaction", None)

        return {"verified": True, "payer": payer, "transaction": transaction}

    return verifier, gate


def verifier_from_env(environ=None, client_factory=None):
    """Convenience for server entrypoints: read config from the environment.

    Returns ``(verifier_or_None, gate_info)`` using ``X402_FACILITATOR_URL``,
    ``X402_AUTO_SETTLE``, ``X402_FACILITATOR_HEADERS`` (JSON object of static
    extra headers), and ``X402_FACILITATOR_HEADERS_COMMAND`` (argv JSON array;
    run per request, stdout parsed as grouped auth-header JSON).
    ``client_factory`` is an injection seam for tests; production uses the
    official package client. Never raises for an unset env; raises a clear
    RuntimeError only when the URL is set but the package is missing.
    """
    import os

    environ = os.environ if environ is None else environ
    raw_command = (environ.get("X402_FACILITATOR_HEADERS_COMMAND") or "").strip()
    headers_command = None
    if raw_command:
        try:
            parsed_cmd = json.loads(raw_command)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"X402_FACILITATOR_HEADERS_COMMAND is not valid JSON: {exc}"
            )
        if (
            not isinstance(parsed_cmd, list)
            or not parsed_cmd
            or not all(isinstance(a, str) for a in parsed_cmd)
        ):
            raise ValueError(
                "X402_FACILITATOR_HEADERS_COMMAND must be a JSON array of strings "
                '(argv), e.g. ["python3", "/srv/mint_cdp_jwt.py"]'
            )
        headers_command = parsed_cmd

    extra_headers = None
    raw_headers = (environ.get("X402_FACILITATOR_HEADERS") or "").strip()
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
        except json.JSONDecodeError as exc:
            raise ValueError(f"X402_FACILITATOR_HEADERS is not valid JSON: {exc}")
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError("X402_FACILITATOR_HEADERS must be a JSON object of strings")
        extra_headers = parsed

    return make_verifier(
        (environ.get("X402_FACILITATOR_URL") or "").strip(),
        settle=environ.get("X402_AUTO_SETTLE", "").strip() in ("1", "true", "yes"),
        extra_headers=extra_headers,
        headers_command=headers_command,
        client_factory=client_factory,
    )
