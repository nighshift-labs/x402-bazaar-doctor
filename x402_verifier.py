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

Requires: ``pip install "x402[all]"`` (the sync facilitator client needs
``httpx``).
"""
from __future__ import annotations

import json

import x402_endpoint


def build_requirements() -> dict:
    """Single source of truth for the paywall requirements.

    Delegates to the endpoint's builder so the 402 advertisement, the
    structural payload check, and the facilitator-bound verification all
    pin the identical price, asset, network, and payee.
    """
    return x402_endpoint._payment_requirements()


def make_verifier(facilitator_url, settle=False, client_factory=None, extra_headers=None):
    """Return ``(verifier_callable_or_None, gate_info_dict)``.

    ``client_factory`` is an injection seam for tests (default builds the
    official ``HTTPFacilitatorClientSync``). With no facilitator URL the
    result is ``(None, {"mode": "fail_closed"})``.
    """
    if not facilitator_url:
        return None, {"mode": "fail_closed"}

    if client_factory is None:
        try:
            from x402.http import FacilitatorConfig
            from x402.http.facilitator_client import HTTPFacilitatorClientSync
        except ImportError:
            raise RuntimeError(
                "X402_FACILITATOR_URL is set but the official x402 package is not "
                "installed. Run: pip install 'x402[all]'"
            )

        def client_factory(config_dict):
            return HTTPFacilitatorClientSync(FacilitatorConfig(**config_dict))

    config_dict = {"url": facilitator_url}
    if extra_headers:
        config_dict["create_headers"] = lambda: dict(extra_headers)
    client = client_factory(config_dict)
    mode = "verify_and_settle" if settle else "verify_only"

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

    return verifier, {"mode": mode, "facilitator": facilitator_url}


def verifier_from_env(environ=None, client_factory=None):
    """Convenience for server entrypoints: read config from the environment.

    Returns ``(verifier_or_None, gate_info)`` using ``X402_FACILITATOR_URL``,
    ``X402_AUTO_SETTLE``, and ``X402_FACILITATOR_HEADERS`` (JSON object of
    extra headers for facilitators that require auth, e.g. CDP API keys).
    ``client_factory`` is an injection seam for tests; production uses the
    official package client. Never raises for an unset env; raises a clear
    RuntimeError only when the URL is set but the package is missing.
    """
    import os

    environ = os.environ if environ is None else environ
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
        client_factory=client_factory,
    )
