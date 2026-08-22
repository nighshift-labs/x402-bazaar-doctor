"""Tests for the official-package-backed x402 payment verifier.

The verifier delegates EIP-3009 signature/balance/nonce checking to the
official ``x402`` package's facilitator client (network boundary), so these
tests run fully offline against a fake client factory. No chain access, no
signing, no live facilitator calls.
"""
import base64
import json
import os
import sys
import unittest

from starlette.testclient import TestClient

import x402_endpoint
from x402_endpoint import PAYWALL_AMOUNT, PAYWALL_ASSET, PAYWALL_NETWORK, PAYWALL_PAYTO, create_app
from x402_verifier import build_requirements, make_verifier, verifier_from_env


PAYER = "0x1111111111111111111111111111111111111111"


def _paid_headers():
    payload = {
        "x402Version": 2,
        "payload": {
            "authorization": {
                "from": PAYER,
                "to": PAYWALL_PAYTO,
                "value": PAYWALL_AMOUNT,
                "validAfter": "0",
                "validBefore": "99999999999",
                "nonce": "0x" + "00" * 32,
            },
            "signature": {"v": 27, "r": "0x" + "11" * 32, "s": "0x" + "22" * 32},
        },
        "accepted": {
            "scheme": "exact",
            "network": PAYWALL_NETWORK,
            "asset": PAYWALL_ASSET,
            "amount": PAYWALL_AMOUNT,
            "payTo": PAYWALL_PAYTO,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USD Coin", "version": "2"},
        },
    }
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"PAYMENT-SIGNATURE": header}, payload


class _FakeFacilitator:
    """Offline stand-in for HTTPFacilitatorClientSync."""

    def __init__(self, config=None):
        self.config = config
        self.verify_calls = []
        self.settle_calls = []
        self.verify_result = None
        self.settle_result = None
        self.verify_raises = None
        self.settle_raises = None

    def verify_from_bytes(self, payload_bytes, requirements_bytes):
        self.verify_calls.append((payload_bytes, requirements_bytes))
        if self.verify_raises is not None:
            raise self.verify_raises
        return self.verify_result

    def settle_from_bytes(self, payload_bytes, requirements_bytes):
        self.settle_calls.append((payload_bytes, requirements_bytes))
        if self.settle_raises is not None:
            raise self.settle_raises
        return self.settle_result


class _Types:
    """Minimal namespace mimicking package response objects."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class BuildRequirementsTests(unittest.TestCase):
    def test_matches_endpoint_payment_requirements_exactly(self):
        self.assertEqual(build_requirements(), x402_endpoint._payment_requirements())

    def test_rail_pins_are_unchanged(self):
        req = build_requirements()
        self.assertEqual(req["network"], "eip155:8453")
        self.assertEqual(req["amount"], "50000")
        self.assertEqual(req["payTo"], PAYWALL_PAYTO)


class MakeVerifierTests(unittest.TestCase):
    def test_no_facilitator_returns_fail_closed_none(self):
        verifier, gate = make_verifier(None)
        self.assertIsNone(verifier)
        self.assertEqual(gate["mode"], "fail_closed")

    def test_blank_facilitator_returns_fail_closed_none(self):
        verifier, gate = make_verifier("")
        self.assertIsNone(verifier)
        self.assertEqual(gate["mode"], "fail_closed")

    def test_health_reports_command_auth_mode(self):
        """Operators must be able to confirm the mint script is wired at boot."""
        import x402_endpoint

        app = x402_endpoint.create_app(
            payment_verifier=lambda p, r: {"verified": False, "reason": "x"},
            payment_gate={
                "mode": "verify_only",
                "facilitator": "https://facilitator.example",
                "auth": "command",
            },
        )
        client = TestClient(app)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("auth: command", resp.json()["payment_gate"])

    def test_facilitator_url_yields_verify_only_by_default(self):
        verifier, gate = make_verifier("https://facilitator.example", client_factory=_FakeFacilitator)
        self.assertTrue(callable(verifier))
        self.assertEqual(gate["mode"], "verify_only")
        self.assertEqual(gate["facilitator"], "https://facilitator.example")

    def test_settle_flag_yields_verify_and_settle(self):
        _, gate = make_verifier(
            "https://facilitator.example", settle=True, client_factory=_FakeFacilitator
        )
        self.assertEqual(gate["mode"], "verify_and_settle")


class VerifierBehaviourTests(unittest.TestCase):
    def _make(self, settle=False):
        fake = _FakeFacilitator()
        verifier, _ = make_verifier(
            "https://facilitator.example", settle=settle, client_factory=lambda cfg: fake
        )
        return verifier, fake

    def _payload(self):
        _, payload = _paid_headers()
        return payload

    def test_valid_verify_returns_verified_with_payer(self):
        verifier, fake = self._make()
        fake.verify_result = _Types(is_valid=True, invalid_reason=None, payer=PAYER)
        verdict = verifier(self._payload(), build_requirements())
        self.assertTrue(verdict["verified"])
        self.assertEqual(verdict["payer"], PAYER)
        self.assertIsNone(verdict.get("transaction"))
        self.assertEqual(len(fake.settle_calls), 0)

    def test_invalid_verify_fails_with_reason(self):
        verifier, fake = self._make()
        fake.verify_result = _Types(is_valid=False, invalid_reason="invalid_signature", payer=None)
        verdict = verifier(self._payload(), build_requirements())
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["reason"], "invalid_signature")

    def test_facilitator_exception_fails_closed(self):
        verifier, fake = self._make()
        fake.verify_raises = RuntimeError("connection refused")
        verdict = verifier(self._payload(), build_requirements())
        self.assertFalse(verdict["verified"])
        self.assertIn("facilitator_error", verdict["reason"])

    def test_exact_serialized_bytes_sent_to_facilitator(self):
        verifier, fake = self._make()
        fake.verify_result = _Types(is_valid=True, invalid_reason=None, payer=PAYER)
        payload = self._payload()
        verifier(payload, build_requirements())
        sent_payload, sent_requirements = fake.verify_calls[0]
        self.assertEqual(json.loads(sent_payload), payload)
        self.assertEqual(json.loads(sent_requirements), build_requirements())

    def test_settle_success_records_transaction(self):
        verifier, fake = self._make(settle=True)
        fake.verify_result = _Types(is_valid=True, invalid_reason=None, payer=PAYER)
        fake.settle_result = _Types(
            success=True, error_reason=None, payer=PAYER, transaction="0xabc123"
        )
        verdict = verifier(self._payload(), build_requirements())
        self.assertTrue(verdict["verified"])
        self.assertEqual(verdict["transaction"], "0xabc123")
        self.assertEqual(len(fake.settle_calls), 1)

    def test_settle_failure_fails_with_error_reason(self):
        verifier, fake = self._make(settle=True)
        fake.verify_result = _Types(is_valid=True, invalid_reason=None, payer=PAYER)
        fake.settle_result = _Types(success=False, error_reason="transfer_failed", payer=PAYER)
        verdict = verifier(self._payload(), build_requirements())
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["reason"], "transfer_failed")


class ExtraHeadersTests(unittest.TestCase):
    """Auth headers must survive the real package's wire contract.

    The official client's dict-config path wraps ``create_headers`` in
    ``CreateHeadersAuthProvider``, which calls it once per HTTP request and
    expects a *grouped* result: ``{"verify": {...}, "settle": {...},
    "supported": {...}}``. A flat header dict is silently dropped — requests
    go out unauthenticated. These tests pin the grouped, per-request shape.
    """

    def test_extra_headers_reach_client_config_grouped_per_request(self):
        captured = {}
        calls = []

        def factory(config):
            captured.update(config)
            return _FakeFacilitator(config)

        def create_headers():
            calls.append(1)
            return {"verify": {"X-API-KEY": "v1"}, "settle": {"X-API-KEY": "s1"}}

        verifier, _ = make_verifier(
            "https://facilitator.example",
            client_factory=factory,
            extra_headers=create_headers,
        )
        self.assertTrue(callable(verifier))
        built = captured["create_headers"]()
        # Grouped contract: per-operation header sets.
        self.assertEqual(built["verify"], {"X-API-KEY": "v1"})
        self.assertEqual(built["settle"], {"X-API-KEY": "s1"})
        # Per-request freshness: invoked through the provider each call.
        captured["create_headers"]()
        self.assertEqual(len(calls), 2)

    def test_flat_static_headers_are_wrapped_into_groups(self):
        """Static env JSON still works, but is re-shaped into the grouped form."""
        captured = {}

        def factory(config):
            captured.update(config)
            return _FakeFacilitator(config)

        verifier, _ = make_verifier(
            "https://facilitator.example",
            client_factory=factory,
            extra_headers={"X-API-KEY": "secret-value"},
        )
        self.assertTrue(callable(verifier))
        provider_built = captured["create_headers"]()
        self.assertEqual(provider_built["verify"], {"X-API-KEY": "secret-value"})
        self.assertEqual(provider_built["supported"], {"X-API-KEY": "secret-value"})

    def test_no_extra_headers_leaves_config_without_auth(self):
        captured = {}

        def factory(config):
            captured.update(config)
            return _FakeFacilitator(config)

        make_verifier("https://facilitator.example", client_factory=factory)
        self.assertNotIn("create_headers", captured)

    def test_config_dict_is_accepted_by_real_package_client(self):
        """The config we build must construct the REAL package client.

        Regression guard: an earlier revision passed ``create_headers`` as a
        FacilitatorConfig dataclass kwarg, which raises TypeError on
        construction (the field does not exist) and was never exercised.
        """
        try:
            from x402.http.facilitator_client import (  # noqa: F401
                FacilitatorConfig,
                HTTPFacilitatorClientSync,
            )
        except ImportError:
            self.skipTest("official x402 package not installed")
        import x402_verifier

        cfg = x402_verifier._client_config(
            "https://facilitator.example",
            extra_headers={"X-API-KEY": "k"},
        )
        client = HTTPFacilitatorClientSync(cfg)  # must not raise
        self.assertEqual(client.url, "https://facilitator.example")

    def test_headers_command_is_invoked_per_request_and_grouped(self):
        import x402_verifier

        script = (
            'import json,os,sys; sys.stdout.write(json.dumps('
            '{"verify": {"Authorization": "Bearer tok-" + os.environ.get("PROBE_N","0")}, '
            '"settle": {"Authorization": "Bearer tok-s"}}))'
        )
        verifier, gate = make_verifier(
            "https://facilitator.example",
            headers_command=[sys.executable, "-c", script],
            client_factory=lambda cfg: _FakeFacilitator(cfg),
        )
        self.assertTrue(callable(verifier))
        self.assertEqual(gate["auth"], "command")
        # Each invocation must produce fresh output (env changes between calls).
        os.environ["PROBE_N"] = "1"
        try:
            first = gate["create_headers_probe"]()
            os.environ["PROBE_N"] = "2"
            second = gate["create_headers_probe"]()
        finally:
            os.environ.pop("PROBE_N", None)
        self.assertIn("tok-1", json.dumps(first))
        self.assertIn("tok-2", json.dumps(second))

    def test_headers_command_failure_raises_not_silently_authenticates(self):
        # A broken mint script must fail at BOOT (startup probe), not on the
        # first paid call — and never yield unauthenticated requests.
        with self.assertRaises(RuntimeError):
            make_verifier(
                "https://facilitator.example",
                headers_command=[sys.executable, "-c", "raise SystemExit(3)"],
                client_factory=lambda cfg: _FakeFacilitator(cfg),
            )

    def test_headers_command_and_static_headers_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            make_verifier(
                "https://facilitator.example",
                extra_headers={"X-API-KEY": "k"},
                headers_command=["false"],
            )


class VerifierFromEnvTests(unittest.TestCase):
    def _env(self, **overrides):
        env = {"X402_FACILITATOR_URL": "", "X402_AUTO_SETTLE": "", "X402_FACILITATOR_HEADERS": ""}
        env.update(overrides)
        return env

    def test_empty_env_is_fail_closed(self):
        verifier, gate = verifier_from_env(self._env())
        self.assertIsNone(verifier)
        self.assertEqual(gate["mode"], "fail_closed")

    def test_url_only_env_activates_verify_only(self):
        verifier, gate = verifier_from_env(
            self._env(X402_FACILITATOR_URL="https://facilitator.example"),
            client_factory=lambda cfg: _FakeFacilitator(cfg),
        )
        self.assertTrue(callable(verifier))
        self.assertEqual(gate["mode"], "verify_only")

    def test_auto_settle_flag_activates_settlement(self):
        _, gate = verifier_from_env(
            self._env(
                X402_FACILITATOR_URL="https://facilitator.example",
                X402_AUTO_SETTLE="1",
            ),
            client_factory=lambda cfg: _FakeFacilitator(cfg),
        )
        self.assertEqual(gate["mode"], "verify_and_settle")

    def test_invalid_headers_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            verifier_from_env(
                self._env(
                    X402_FACILITATOR_URL="https://facilitator.example",
                    X402_FACILITATOR_HEADERS="{not json",
                )
            )

    def test_non_string_headers_values_rejected(self):
        with self.assertRaises(ValueError):
            verifier_from_env(
                self._env(
                    X402_FACILITATOR_URL="https://facilitator.example",
                    X402_FACILITATOR_HEADERS='{"X-API-KEY": 123}',
                )
            )

    def test_missing_package_raises_install_hint(self):
        try:
            import x402  # noqa: F401

            self.skipTest("official x402 package installed; hint path not reachable")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as ctx:
            verifier_from_env(
                self._env(X402_FACILITATOR_URL="https://facilitator.example")
            )
        self.assertIn("pip install", str(ctx.exception))


class FailClosedIntegrationTests(unittest.TestCase):
    def test_unconfigured_verifier_keeps_endpoint_at_501(self):
        verifier, _ = make_verifier(None)
        client = TestClient(create_app(payment_verifier=verifier))
        headers, _ = _paid_headers()
        resp = client.post("/diagnose", json={"observation": {}}, headers=headers)
        self.assertEqual(resp.status_code, 501)
        self.assertEqual(resp.json()["error"], "payment_not_verified")

    def test_real_verifier_shape_passes_diagnosis_through(self):
        fake = _FakeFacilitator()
        fake.verify_result = _Types(is_valid=True, invalid_reason=None, payer=PAYER)
        verifier, _ = make_verifier(
            "https://facilitator.example", client_factory=lambda cfg: fake
        )
        observation = {
            "payment_scheme_version": 1,
            "extensions_bazaar_key_present": False,
            "settle_response_bazaar_present": False,
            "bazaar_status": None,
            "discovery_row_present": False,
            "resource_url": "https://example.com/paid-report",
        }
        client = TestClient(create_app(payment_verifier=verifier))
        headers, _ = _paid_headers()
        resp = client.post("/diagnose", json={"observation": observation}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["diagnosis"], "v1_envelope_extension_ignored")
        self.assertEqual(body["payer"], PAYER)


if __name__ == "__main__":
    unittest.main()
