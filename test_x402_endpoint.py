"""Tests for the x402-payable HTTP endpoint wrapping the Bazaar Doctor.

Wire shapes mirror the x402 V2 reference implementation
(x402-foundation/x402, python/x402/schemas): PaymentRequired.accepts[] with
camelCase keys, CAIP-2 network ids, Base64-encoded JSON headers.
"""
import base64
import json
import unittest

from starlette.testclient import TestClient

import x402_endpoint
from x402_endpoint import PAYWALL_AMOUNT, PAYWALL_ASSET, PAYWALL_NETWORK, PAYWALL_PAYTO, create_app


def _b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _payment_required(client):
    resp = client.post("/diagnose", json={"observation": {}})
    assert resp.status_code == 402, resp.text
    return json.loads(base64.b64decode(resp.headers["PAYMENT-REQUIRED"]))


def _valid_authorization():
    return {
        "from": "0x1111111111111111111111111111111111111111",
        "to": PAYWALL_PAYTO,
        "value": PAYWALL_AMOUNT,
        "validAfter": "0",
        "validBefore": "99999999999",
        "nonce": "0x" + "00" * 32,
    }


def _valid_signature():
    return {
        "v": 27,
        "r": "0x" + "11" * 32,
        "s": "0x" + "22" * 32,
    }


def _paid_headers(authorization=None, signature=None, accepted=None):
    authorization = authorization or _valid_authorization()
    signature = signature or _valid_signature()
    accepted = accepted or {
        "scheme": "exact",
        "network": PAYWALL_NETWORK,
        "asset": PAYWALL_ASSET,
        "amount": PAYWALL_AMOUNT,
        "payTo": PAYWALL_PAYTO,
        "maxTimeoutSeconds": 60,
        "extra": {"name": "USD Coin", "version": "2"},
    }
    payload = {
        "x402Version": 2,
        "payload": {"authorization": authorization, "signature": signature},
        "accepted": accepted,
    }
    return {"PAYMENT-SIGNATURE": _b64(payload)}


V1_OBSERVATION = {
    "payment_scheme_version": 1,
    "extensions_bazaar_key_present": False,
    "settle_response_bazaar_present": False,
    "bazaar_status": None,
    "discovery_row_present": False,
    "resource_url": "https://example.com/paid-report",
}


class PaymentRequiredShapeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_amount_is_half_a_usdc_in_smallest_unit(self):
        self.assertEqual(PAYWALL_AMOUNT, "50000")

    def test_unpaid_request_returns_402_with_v2_payment_required(self):
        resp = self.client.post("/diagnose", json={"observation": V1_OBSERVATION})
        self.assertEqual(resp.status_code, 402)
        header = resp.headers.get("PAYMENT-REQUIRED")
        self.assertTrue(header)
        required = json.loads(base64.b64decode(header))
        self.assertEqual(required["x402Version"], 2)
        self.assertEqual(required["error"], "payment required")

    def test_accepts_entry_carries_exact_v2_fields(self):
        required = _payment_required(self.client)
        self.assertEqual(len(required["accepts"]), 1)
        acc = required["accepts"][0]
        self.assertEqual(acc["scheme"], "exact")
        self.assertEqual(acc["network"], "eip155:8453")
        self.assertEqual(acc["asset"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(acc["amount"], "50000")
        self.assertEqual(acc["payTo"], "0x940445bEf451033D92929A22c7bf6ee72947267c")
        self.assertEqual(acc["maxTimeoutSeconds"], 60)

    def test_resource_and_bazaar_discovery_extension_present(self):
        required = _payment_required(self.client)
        resource = required["resource"]
        self.assertTrue(resource["url"].endswith("/diagnose"))
        self.assertTrue(resource["description"])
        ext = required["extensions"]["bazaar"]
        self.assertEqual(ext["info"]["input"]["type"], "http")
        self.assertEqual(ext["info"]["input"]["method"], "POST")
        self.assertIn("observation", ext["info"]["input"]["body"])


class PaidRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_paid_with_garbage_signature_fails_closed_501(self):
        resp = self.client.post(
            "/diagnose", json={"observation": V1_OBSERVATION}, headers=_paid_headers()
        )
        self.assertEqual(resp.status_code, 501)
        body = resp.json()
        self.assertEqual(body["error"], "payment_not_verified")
        header = resp.headers.get("PAYMENT-RESPONSE")
        self.assertTrue(header)
        settlement = json.loads(base64.b64decode(header))
        self.assertFalse(settlement["success"])
        self.assertTrue(settlement["errorReason"])

    def test_missing_authorization_fields_rejected_400(self):
        bad_auth = {"from": "0x1111111111111111111111111111111111111111"}
        resp = self.client.post(
            "/diagnose",
            json={"observation": V1_OBSERVATION},
            headers=_paid_headers(authorization=bad_auth),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_payment_payload")

    def test_malformed_payment_signature_header_rejected_400(self):
        resp = self.client.post(
            "/diagnose",
            json={"observation": V1_OBSERVATION},
            headers={"PAYMENT-SIGNATURE": "!!!not-base64!!!"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_payment_payload")

    def test_accepted_requirements_mismatch_rejected_400(self):
        accepted = {
            "scheme": "exact",
            "network": "eip155:1",
            "asset": PAYWALL_ASSET,
            "amount": PAYWALL_AMOUNT,
            "payTo": PAYWALL_PAYTO,
            "maxTimeoutSeconds": 60,
            "extra": {},
        }
        resp = self.client.post(
            "/diagnose",
            json={"observation": V1_OBSERVATION},
            headers=_paid_headers(accepted=accepted),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "payment_requirements_mismatch")

    def test_verified_payment_yields_diagnosis(self):
        calls = []

        def fake_verifier(payload, requirements):
            calls.append(payload)
            return {"verified": True, "payer": payload["payload"]["authorization"]["from"]}

        app = create_app(payment_verifier=fake_verifier)
        client = TestClient(app)
        resp = client.post(
            "/diagnose", json={"observation": V1_OBSERVATION}, headers=_paid_headers()
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["diagnosis"], "v1_envelope_extension_ignored")
        self.assertEqual(body["confidence"], "confirmed")
        self.assertEqual(body["payer"], "0x1111111111111111111111111111111111111111")
        settlement = json.loads(base64.b64decode(resp.headers["PAYMENT-RESPONSE"]))
        self.assertTrue(settlement["success"])
        self.assertEqual(len(calls), 1)

    def test_verifier_rejection_surfaces_402_with_error(self):
        def fake_verifier(payload, requirements):
            return {"verified": False, "reason": "invalid signature"}

        app = create_app(payment_verifier=fake_verifier)
        client = TestClient(app)
        resp = client.post(
            "/diagnose", json={"observation": V1_OBSERVATION}, headers=_paid_headers()
        )
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["error"], "payment_invalid")

    def test_invalid_observation_after_verification_is_400_not_500(self):
        def fake_verifier(payload, requirements):
            return {"verified": True, "payer": payload["payload"]["authorization"]["from"]}

        app = create_app(payment_verifier=fake_verifier)
        client = TestClient(app)
        resp = client.post(
            "/diagnose",
            json={"observation": {"payment_scheme_version": 9}},
            headers=_paid_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_observation")


class FreeSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    def test_health_is_free(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_free_self_serve_sample_is_free(self):
        resp = self.client.get("/sample")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("observation", resp.json())

    def test_get_on_diagnose_is_405(self):
        resp = self.client.get("/diagnose")
        self.assertEqual(resp.status_code, 405)


if __name__ == "__main__":
    unittest.main()
