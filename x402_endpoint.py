#!/usr/bin/env python3
"""x402-payable HTTP endpoint wrapping the Bazaar Doctor classifier.

One paid resource: ``POST /diagnose`` at $0.50 native USDC on Base per call,
per the mission's x402-payable product lane. Wire shapes follow the x402 V2
reference implementation (x402-foundation/x402, python/x402/schemas):

- 402 responses carry a Base64-encoded ``PaymentRequired`` object in the
  ``PAYMENT-REQUIRED`` header (accepts[] uses camelCase keys, CAIP-2 network).
- Paid retries carry a Base64-encoded ``PaymentPayload`` in
  ``PAYMENT-SIGNATURE``; every settlement attempt answers with a Base64
  ``SettlementResponse`` in ``PAYMENT-RESPONSE``.

FAIL-CLOSED BY DESIGN: this module never trusts an unverified payment. When no
``payment_verifier`` is wired, every paid request returns 501
``payment_not_verified`` — the service advertises a real price and rail but
moves nothing until the official ``x402`` verifier (or an equivalent EIP-3009
ecrecover check) is installed at deploy time. See deploy/x402-endpoint-runbook.md.

Free surfaces: ``GET /health`` and ``GET /sample`` (self-serve trial input).
Read-only: no chain queries, no signing, no outbound network calls.
"""
from __future__ import annotations

import base64
import binascii
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from x402_bazaar_doctor import diagnose

# Payment rail: native USDC on Base mainnet (payments/base-usdc.json).
PAYWALL_NETWORK = "eip155:8453"  # CAIP-2 for Base mainnet
PAYWALL_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Circle USDC (Base)
PAYWALL_PAYTO = "0x940445bEf451033D92929A22c7bf6ee72947267c"  # receive-only project wallet
PAYWALL_AMOUNT = "50000"  # $0.50 in USDC smallest units (6 decimals)
PAYWALL_PRICE_HUMAN = "$0.50 USDC"
MAX_TIMEOUT_SECONDS = 60

_AUTH_FIELDS = ("from", "to", "value", "validAfter", "validBefore", "nonce")

_SAMPLE_OBSERVATION = {
    "payment_scheme_version": 1,
    "extensions_bazaar_key_present": False,
    "settle_response_bazaar_present": False,
    "bazaar_status": None,
    "discovery_row_present": False,
    "resource_url": "https://example.com/paid-report",
}


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _payment_requirements() -> dict:
    return {
        "scheme": "exact",
        "network": PAYWALL_NETWORK,
        "asset": PAYWALL_ASSET,
        "amount": PAYWALL_AMOUNT,
        "payTo": PAYWALL_PAYTO,
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def _payment_required_body(request_url: str) -> dict:
    """V2 PaymentRequired object, including our own Bazaar discovery extension."""
    return {
        "x402Version": 2,
        "error": "payment required",
        "resource": {
            "url": request_url,
            "description": (
                "Diagnose why a settled x402 payment has no Bazaar discovery row. "
                f"{PAYWALL_PRICE_HUMAN} per diagnosis; deterministic offline classifier."
            ),
            "mimeType": "application/json",
        },
        "accepts": [_payment_requirements()],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {
                        "type": "http",
                        "method": "POST",
                        "bodyType": "json",
                        "body": {"observation": _SAMPLE_OBSERVATION},
                    },
                    "output": {
                        "type": "object",
                        "format": "json",
                        "example": {
                            "diagnosis": "v1_envelope_extension_ignored",
                            "confidence": "confirmed",
                            "recommended_actions": ["…"],
                        },
                    },
                },
                "schema": "https://x402.org/schemas/bazaar-v1.json",
            }
        },
    }


def _settlement(success: bool, error_reason=None, payer=None) -> dict:
    return {
        "success": success,
        "errorReason": error_reason,
        "transaction": None,
        "network": PAYWALL_NETWORK if success else None,
        "payer": payer,
    }


def _decode_payment_header(header: str):
    """Return (payload_dict, None) or (None, error_string)."""
    try:
        payload = json.loads(base64.b64decode(header))
    except (binascii.Error, ValueError, TypeError):
        return None, "PAYMENT-SIGNATURE is not valid Base64 JSON"
    if not isinstance(payload, dict):
        return None, "PAYMENT-SIGNATURE must decode to a JSON object"
    return payload, None


def _validate_payload_shape(payload: dict):
    """Structural validation only — no cryptographic judgement here.

    Returns an error string or None. The accepted requirements must match ours
    exactly so a verifier can bind the authorization to THIS price and payee.
    """
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return "missing scheme payload object"
    auth = inner.get("authorization")
    if not isinstance(auth, dict) or any(not auth.get(f) for f in _AUTH_FIELDS):
        raise _PayloadShapeError(
            "authorization missing required fields: " + ", ".join(_AUTH_FIELDS),
            kind="invalid_payment_payload",
        )
    if not isinstance(inner.get("signature"), dict):
        raise _PayloadShapeError("missing signature object", kind="invalid_payment_payload")

    accepted = payload.get("accepted")
    if not isinstance(accepted, dict):
        return "missing accepted payment requirements"
    expected = _payment_requirements()
    for key in ("scheme", "network", "asset", "amount", "payTo"):
        if accepted.get(key) != expected[key]:
            raise _PayloadShapeError(
                f"accepted.{key} does not match this resource's requirements",
                kind="payment_requirements_mismatch",
            )
    return None


class _PayloadShapeError(ValueError):
    """Structurally invalid or non-matching payment payload."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind  # "invalid_payment_payload" | "payment_requirements_mismatch"


async def diagnose_endpoint(request: Request):
    body_required = _payment_required_body(str(request.url))

    header = request.headers.get("payment-signature")
    if not header:
        return JSONResponse(
            {"error": "payment required", "price": PAYWALL_PRICE_HUMAN,
             "rail": "native USDC on Base", "resource": str(request.url)},
            status_code=402,
            headers={"PAYMENT-REQUIRED": _b64(body_required)},
        )

    payload, err = _decode_payment_header(header)
    if err:
        return JSONResponse({"error": "invalid_payment_payload", "detail": err}, status_code=400)

    try:
        _validate_payload_shape(payload)
    except _PayloadShapeError as shape_err:
        return JSONResponse(
            {"error": shape_err.kind, "detail": str(shape_err)}, status_code=400
        )

    verifier = request.app.state.payment_verifier
    if verifier is None:
        # Fail closed: advertise the rail, verify nothing, move nothing.
        return JSONResponse(
            {
                "error": "payment_not_verified",
                "detail": (
                    "This deployment has no payment verifier wired; no diagnosis was "
                    "performed and nothing was charged. Use the free self-serve path "
                    "(GET /sample + local classifier) or contact the operator."
                ),
            },
            status_code=501,
            headers={"PAYMENT-RESPONSE": _b64(_settlement(False, "verifier_not_configured"))},
        )

    try:
        verdict = verifier(payload, _payment_requirements())
    except Exception as exc:  # verifier crash must never look like a valid payment
        return JSONResponse(
            {"error": "payment_not_verified", "detail": f"verifier failure: {exc}"},
            status_code=501,
            headers={"PAYMENT-RESPONSE": _b64(_settlement(False, "verifier_error"))},
        )

    if not verdict.get("verified"):
        return JSONResponse(
            {"error": "payment_invalid", "detail": verdict.get("reason")},
            status_code=402,
            headers={"PAYMENT-RESPONSE": _b64(_settlement(False, verdict.get("reason")))},
        )

    payer = verdict.get("payer")
    try:
        raw = await request.body()
        data = json.loads(raw) if raw else {}
        observation = data["observation"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return JSONResponse({"error": "invalid_observation"}, status_code=400)

    try:
        report = diagnose(observation)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": "invalid_observation", "detail": str(exc)}, status_code=400
        )

    report["payer"] = payer
    return JSONResponse(
        report,
        headers={"PAYMENT-RESPONSE": _b64(_settlement(True, payer=payer))},
    )


async def health(request: Request):
    return JSONResponse(
        {
            "status": "ok",
            "service": "x402-bazaar-doctor",
            "price": PAYWALL_PRICE_HUMAN,
            "rail": "native USDC on Base",
            "payment_gate": "fail-closed until verifier wired",
        }
    )


async def sample(request: Request):
    return JSONResponse(
        {
            "observation": _SAMPLE_OBSERVATION,
            "usage": "POST /diagnose with {\"observation\": {...}} after x402 payment",
        }
    )


def create_app(payment_verifier=None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/diagnose", diagnose_endpoint, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/sample", sample, methods=["GET"]),
        ]
    )
    app.state.payment_verifier = payment_verifier
    return app


if __name__ == "__main__":  # manual smoke only; see deploy runbook for production
    import os

    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "8787")))
