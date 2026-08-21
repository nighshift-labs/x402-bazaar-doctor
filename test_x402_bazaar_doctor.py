import json
import unittest
from pathlib import Path

from x402_bazaar_doctor import diagnose


class V1EnvelopeTests(unittest.TestCase):
    def test_v1_envelope_without_bazaar_key_is_confirmed_root_cause(self):
        """The author-confirmed failure from x402#3045: a v1 envelope whose
        Bazaar extension was ignored even though validate passed."""
        observation = {
            "payment_scheme_version": 1,
            "extensions_bazaar_key_present": False,
            "settle_response_bazaar_present": False,
            "bazaar_status": None,
            "discovery_row_present": False,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "v1_envelope_extension_ignored")
        self.assertEqual(result["confidence"], "confirmed")
        self.assertTrue(any("v2" in action.lower() for action in result["recommended_actions"]))

    def test_v2_envelope_with_absent_bazaar_response_is_distinct_state(self):
        """Absent Bazaar response must NOT collapse into an empty/other state."""
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": False,
            "bazaar_status": None,
            "discovery_row_present": None,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "bazaar_response_absent")
        self.assertEqual(result["confidence"], "spec_derived")

    def test_rejected_status_reports_reason(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "rejected",
            "rejected_reason": "schema_validation_failed",
            "discovery_row_present": False,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "catalog_rejected")
        self.assertEqual(result["rejected_reason"], "schema_validation_failed")

    def test_processing_status_is_not_a_failure_verdict(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "processing",
            "discovery_row_present": None,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "catalog_processing")

    def test_success_without_discovery_row_isolates_post_acceptance_indexing(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": False,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "success_but_not_indexed")

    def test_success_with_discovery_row_is_healthy(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": True,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "indexed_ok")

    def test_success_without_discovery_poll_is_inconclusive(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": None,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "verify_discovery")

    def test_unpaid_200_with_success_settle_is_confirmed_root_cause(self):
        """#2993 resolution re-derived in #3045: a resource that answers 200
        to an unpaid request is never catalogued, no matter how many
        settlements succeed. This is seller-side and must not be reported
        as a storage/indexing fault."""
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": False,
            "unpaid_request_status": 200,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "unpaid_200_never_catalogued")
        self.assertEqual(result["confidence"], "confirmed")
        self.assertTrue(
            any("unpaid" in action.lower() for action in result["recommended_actions"])
        )

    def test_unpaid_non_200_does_not_trigger(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": False,
            "unpaid_request_status": 402,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "success_but_not_indexed")

    def test_unpaid_status_absent_does_not_trigger(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": False,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "success_but_not_indexed")

    def test_unpaid_200_requires_processed_settle_response(self):
        """The rule only fires once the extension pipeline actually ran;
        an absent bazaar response is still its own earlier fault."""
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": False,
            "bazaar_status": None,
            "discovery_row_present": False,
            "unpaid_request_status": 200,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(result["diagnosis"], "bazaar_response_absent")

    def test_unpaid_status_is_captured_as_evidence(self):
        observation = {
            "payment_scheme_version": 2,
            "extensions_bazaar_key_present": True,
            "settle_response_bazaar_present": True,
            "bazaar_status": "success",
            "discovery_row_present": False,
            "unpaid_request_status": 200,
            "resource_url": "https://example.com/paid-report",
        }
        result = diagnose(observation)
        self.assertEqual(
            result["evidence_captured"]["unpaid_request_status"], 200
        )

    def test_observation_requires_envelope_version(self):
        with self.assertRaises(ValueError):
            diagnose({"extensions_bazaar_key_present": False})


class Unpaid400BodyValidationTests(unittest.TestCase):
    """Field observations from the #3045 catalogued-side census
    (novadyne-hq, 2026-08-21, 49 operators / one per netloc): the only
    non-402 unpaid response was a 400 from a resource that validates the
    request body BEFORE reaching the payment gate. That is ordering,
    not gating, and must not be bucketed as 'possibly ungated'."""

    BASE = {
        "payment_scheme_version": 2,
        "extensions_bazaar_key_present": True,
        "settle_response_bazaar_present": True,
        "bazaar_status": "success",
        "discovery_row_present": False,
        "resource_url": "https://example.com/paid-report",
    }

    def _obs(self, **over):
        obs = dict(self.BASE)
        obs.update(over)
        return obs

    def test_unpaid_400_with_success_settle_is_ordering_not_gating(self):
        result = diagnose(self._obs(unpaid_request_status=400))
        self.assertEqual(
            result["diagnosis"], "unpaid_400_body_validation_before_payment_gate"
        )
        self.assertEqual(result["confidence"], "field_observed")
        self.assertTrue(
            any(
                "body" in action.lower() and "payment gate" in action.lower()
                for action in result["recommended_actions"]
            )
        )

    def test_unpaid_400_requires_processed_settle_response(self):
        """Same precedence as the unpaid-200 rule: an unprocessed extension
        is still the earlier fault."""
        result = diagnose(
            self._obs(
                settle_response_bazaar_present=False,
                bazaar_status=None,
                unpaid_request_status=400,
            )
        )
        self.assertEqual(result["diagnosis"], "bazaar_response_absent")

    def test_unpaid_400_without_absent_row_stays_inconclusive(self):
        result = diagnose(
            self._obs(discovery_row_present=None, unpaid_request_status=400)
        )
        self.assertEqual(result["diagnosis"], "verify_discovery")

    def test_unpaid_400_captured_as_evidence(self):
        result = diagnose(self._obs(unpaid_request_status=400))
        self.assertEqual(result["evidence_captured"]["unpaid_request_status"], 400)

    def test_unpaid_405_is_flagged_as_likely_wrong_verb_probe(self):
        """Census failure mode: defaulting to GET against a POST-only origin
        produced 21 fake 405s. A captured 405 must not be read as gating."""
        result = diagnose(self._obs(unpaid_request_status=405))
        self.assertEqual(result["diagnosis"], "success_but_not_indexed")
        self.assertIn("declared", " ".join(result["recommended_actions"]).lower())

    def test_plain_402_case_still_instructs_declared_verb_capture(self):
        """Even a clean 402 must be captured with the seller's declared
        method, so the checklist carries the verb requirement everywhere."""
        result = diagnose(self._obs(unpaid_request_status=402))
        self.assertEqual(result["diagnosis"], "success_but_not_indexed")
        self.assertIn("declared", " ".join(result["recommended_actions"]).lower())


if __name__ == "__main__":
    unittest.main()
