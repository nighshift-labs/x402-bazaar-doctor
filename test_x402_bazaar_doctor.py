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

    def test_observation_requires_envelope_version(self):
        with self.assertRaises(ValueError):
            diagnose({"extensions_bazaar_key_present": False})


if __name__ == "__main__":
    unittest.main()
