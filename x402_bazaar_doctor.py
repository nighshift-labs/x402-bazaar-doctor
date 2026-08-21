#!/usr/bin/env python3
"""x402 Bazaar Doctor — classify why a settled x402 payment has no Bazaar row.

Deterministic diagnostic over a captured observation (no network access).
Encodes the discriminators confirmed in x402-foundation/x402#3045:

- A v1 payment envelope whose Bazaar extension is absent gets ignored by the
  pipeline even when ``/v2/x402/validate`` passes -> settlement succeeds,
  discovery stays empty.
- An absent settle-response ``bazaar`` key must be reported as its own state;
  it is NOT an empty outcome (``e30=`` decodes to ``{}`` and parsers that
  default missing keys to ``{}`` erase the distinction).
- ``rejected`` / ``processing`` / ``success`` statuses isolate validator-ingest,
  queue/indexing-delay, and post-acceptance storage respectively.
- A resource that answers ``200`` to an *unpaid* request is never catalogued,
  no matter how many settlements succeed (the #2993 resolution, re-derived by
  the #3045 method census). This seller-side cause outranks storage/indexing
  theories when ``unpaid_request_status`` is captured as 200.
- A resource that answers ``400`` to an *unpaid* request is validating the
  request body BEFORE the payment gate — ordering, not gating (#3045
  catalogued-side census, novadyne-hq 2026-08-21: 49 operators, one 400,
  zero 200s). Unpaid captures must use the seller's declared method; wrong-
  verb probes produce misleading 405s (21 observed in the same census).

Read-only: no chain queries, no signing, no network calls.
"""
from __future__ import annotations

import json
import sys

CONFIRMED = "confirmed"
SPEC_DERIVED = "spec_derived"
FIELD_OBSERVED = "field_observed"

_ACTIONS = {
    "v1_envelope_extension_ignored": [
        "Move to an x402 v2 envelope: place `extensions` at the PaymentPayload top level and use the ResourceInfo `resource` shape.",
        "Re-send one controlled paid request and capture EXTENSION-RESPONSES.bazaar.status from the settle response.",
        "/v2/x402/validate passing is necessary but NOT sufficient; do not rely on validate as a discovery oracle.",
    ],
    "bazaar_response_absent": [
        "Capture the raw settle response header length and full key set before changing route metadata.",
        "Verify the envelope version and that `extensions` sits at the PaymentPayload top level.",
        "Treat this as 'extension not processed', distinct from an empty Bazaar outcome object.",
    ],
    "catalog_rejected": [
        "Inspect rejectedReason against the Bazaar extension schema; fix `info` so facilitator validation passes.",
        "Do not resolve untrusted external `$ref`s in extension info; inline the values instead.",
    ],
    "catalog_processing": [
        "Wait and poll discovery by exact resource URL and settlement time; indexing may be asynchronous.",
        "If still absent after the operator's stated window, escalate as a queue/job failure with timestamps.",
    ],
    "success_but_not_indexed": [
        "Focus on post-acceptance storage/indexing or discovery filtering; settlement and ingest both succeeded.",
        "Query discovery with the exact resource URL used in the paid request before concluding.",
        "Before deeper debugging: confirm the resource answers 402 (not 200) to an unpaid request — a 200-on-unpaid resource is never catalogued (#2993). Capture that unpaid status with the seller's declared method (read it from a catalogued sibling row at `extensions.bazaar.info.input.method`); probing with the wrong verb produces misleading 405s.",
    ],
    "unpaid_200_never_catalogued": [
        "Make the resource answer 402 PAYMENT-REQUIRED to unpaid requests; a resource that serves 200 without payment is never catalogued, regardless of successful settlements.",
        "After fixing the unpaid response, re-send one controlled paid request and re-check discovery by exact resource URL.",
        "Do not debug storage/indexing first: this seller-side cause explains settled-but-absent resources on its own.",
    ],
    "unpaid_400_body_validation_before_payment_gate": [
        "The 400 to an unpaid request is request-body validation running BEFORE the payment gate — that is ordering, not gating, and not a #2993 signal.",
        "Capture the unpaid status again with a spec-shaped empty body and the seller's declared method; if it still returns 400, fix body validation ordering so unpaid requests reach the 402 gate.",
        "Do not rank this seller as 'possibly ungated': a catalogued-side census found exactly this shape at ai.stable-jack.com while 47/48 conclusive operators answered 402.",
    ],
    "indexed_ok": [],
    "verify_discovery": [
        "Poll the discovery endpoint for the exact resource URL for at least ten minutes after settlement, then re-run.",
    ],
    "unknown_status": [
        "Capture the raw bazaar status value verbatim; it is outside the spec's success/processing/rejected set.",
    ],
}


def diagnose(observation):
    """Classify one captured observation dict into a diagnosis report."""
    if not isinstance(observation, dict):
        raise TypeError("observation must be a dict")
    version = observation.get("payment_scheme_version")
    if version not in (1, 2):
        raise ValueError("payment_scheme_version must be 1 or 2")

    status = observation.get("bazaar_status")
    response_present = bool(observation.get("settle_response_bazaar_present"))
    v1_no_extension = (
        version == 1 and not observation.get("extensions_bazaar_key_present")
    )

    if v1_no_extension:
        diagnosis, confidence = "v1_envelope_extension_ignored", CONFIRMED
    elif not response_present:
        diagnosis, confidence = "bazaar_response_absent", SPEC_DERIVED
    elif (
        status == "success"
        and observation.get("discovery_row_present") is False
        and observation.get("unpaid_request_status") == 200
    ):
        diagnosis, confidence = "unpaid_200_never_catalogued", CONFIRMED
    elif (
        status == "success"
        and observation.get("discovery_row_present") is False
        and observation.get("unpaid_request_status") == 400
    ):
        diagnosis, confidence = "unpaid_400_body_validation_before_payment_gate", FIELD_OBSERVED
    elif status == "rejected":
        diagnosis, confidence = "catalog_rejected", SPEC_DERIVED
    elif status == "processing":
        diagnosis, confidence = "catalog_processing", SPEC_DERIVED
    elif status == "success":
        row = observation.get("discovery_row_present")
        if row is True:
            diagnosis, confidence = "indexed_ok", SPEC_DERIVED
        elif row is False:
            diagnosis, confidence = "success_but_not_indexed", SPEC_DERIVED
        else:
            diagnosis, confidence = "verify_discovery", SPEC_DERIVED
    else:
        diagnosis, confidence = "unknown_status", SPEC_DERIVED

    return {
        "diagnosis": diagnosis,
        "confidence": confidence,
        "recommended_actions": list(_ACTIONS[diagnosis]),
        "rejected_reason": observation.get("rejected_reason"),
        "resource_url": observation.get("resource_url"),
        "evidence_captured": {
            "payment_scheme_version": version,
            "extensions_bazaar_key_present": observation.get(
                "extensions_bazaar_key_present"
            ),
            "settle_response_bazaar_present": response_present,
            "bazaar_status": status,
            "discovery_row_present": observation.get("discovery_row_present"),
            "unpaid_request_status": observation.get("unpaid_request_status"),
        },
        "scope_note": (
            "Static classification of one captured observation. No chain query, "
            "no settlement execution, no guarantee of discovery behavior."
        ),
    }


def main(argv):
    if len(argv) != 1:
        print("usage: x402_bazaar_doctor.py <observation.json>", file=sys.stderr)
        return 2
    observation = json.loads(Path(argv[0]).read_text())
    print(json.dumps(diagnose(observation), indent=2))
    return 0


if __name__ == "__main__":
    from pathlib import Path

    sys.exit(main(sys.argv[1:]))
