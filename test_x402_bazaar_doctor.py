import json
import unittest
from pathlib import Path

from x402_bazaar_doctor import diagnose, summarize_census_rows


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


class CensusCalibrationTests(unittest.TestCase):
    """Calibration against the real per-row census novadyne-hq delivered in
    #3045 comment 5370410212 (49 rows / 49 distinct operators, one per
    netloc, declared verbs, committed with the publisher's explicit
    authorization). The classifier must reproduce their published
    distribution exactly and never misclassify the real shapes."""

    @classmethod
    def setUpClass(cls):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "x402_census_rows_2026-08-21.json"
        )
        cls.census = json.loads(fixture.read_text())
        cls.rows = cls.census["rows"]

    def test_fixture_matches_published_census_shape(self):
        self.assertEqual(len(self.rows), 49)
        statuses = [r["unpaid_status"] for r in self.rows]
        self.assertEqual(statuses.count(402), 47)
        self.assertEqual(statuses.count(400), 1)
        self.assertEqual(statuses.count(None), 1)
        self.assertEqual(len({r["resource"] for r in self.rows}), 49)
        self.assertTrue(
            all(r["declared_verb"] in ("GET", "POST") for r in self.rows)
        )

    def test_summarize_reproduces_published_distribution(self):
        summary = summarize_census_rows(self.rows)
        self.assertEqual(summary["rows"], 49)
        self.assertEqual(summary["distinct_hosts"], 49)
        self.assertEqual(summary["conclusive"], 48)
        self.assertEqual(summary["non_conclusive"], 1)
        self.assertEqual(summary["status_counts"], {"402": 47, "400": 1})
        self.assertEqual(summary["declared_verb_counts"], {"GET": 27, "POST": 22})

    def test_summarize_flags_400_rows_as_field_observed_instances(self):
        summary = summarize_census_rows(self.rows)
        self.assertEqual(len(summary["field_observed_400_rows"]), 1)
        row = summary["field_observed_400_rows"][0]
        self.assertEqual(row["resource"], "https://ai.stable-jack.com/x402/tools/defi_bridge_intelligence")
        self.assertEqual(row["declared_verb"], "POST")
        self.assertEqual(row["unpaid_status"], 400)

    def test_summarize_reports_zero_200s_as_a_bound(self):
        summary = summarize_census_rows(self.rows)
        self.assertEqual(summary["status_counts"].get("200", 0), 0)
        self.assertIn("bound", summary["bound_note"].lower())

    def test_summarize_keeps_timeout_rows_visible_not_silent(self):
        summary = summarize_census_rows(self.rows)
        self.assertEqual(len(summary["non_conclusive_rows"]), 1)
        self.assertEqual(summary["non_conclusive_rows"][0]["error"], "TimeoutError")

    def test_summarize_accepts_duplicate_netlocs_route_level(self):
        """Correction recorded from the publisher (#3045 comment
        5371343985): several rows per netloc is route-level data, not an
        independence violation. The old one-row-per-netloc guard encoded
        the wrong key and would have rejected the un-deduped set they
        told us to run. Independence is a payTo-cluster question now."""
        duped = self.rows + [dict(self.rows[0])]
        summary = summarize_census_rows(duped)
        self.assertEqual(summary["rows"], 50)
        self.assertEqual(summary["distinct_hosts"], 49)

    def test_summarize_rejects_templated_paths(self):
        """Probing a placeholder path invents a parameter; the publisher's
        caveat says a 404/400 from a made-up value is indistinguishable
        from a real ordering signal."""
        templated = [
            {
                "resource": "https://example.com/api/items/{id}",
                "declared_verb": "GET",
                "unpaid_status": 400,
                "error": None,
            }
        ]
        with self.assertRaises(ValueError):
            summarize_census_rows(templated)

    def test_second_independent_400_upgrades_field_observed_to_multi_instance(self):
        """The publisher's explicit ask: the difference between a
        field_observed note and a rule is a second independent 400-shaped
        instance from a different operator."""
        second_400 = [
            {
                "resource": "https://other-operator.example/v1/tool",
                "declared_verb": "GET",
                "unpaid_status": 400,
                "error": None,
            }
        ]
        summary = summarize_census_rows(self.rows + second_400)
        self.assertEqual(len(summary["field_observed_400_rows"]), 2)
        self.assertEqual(summary["field_observed_400_rule_status"], "multi_instance")
        self.assertEqual(
            summarize_census_rows(self.rows)["field_observed_400_rule_status"],
            "single_instance",
        )

    def test_classifier_diagnoses_every_conclusive_census_row_consistently(self):
        """Run the full census through diagnose(): the 47 clean 402 rows
        (as success-but-absent observations) must classify as
        success_but_not_indexed, the one 400 row as the unpaid-400
        ordering diagnosis, and the timeout row must not be forced into
        any unpaid-status rule."""
        by_status = {}
        for row in self.rows:
            obs = {
                "payment_scheme_version": 2,
                "extensions_bazaar_key_present": True,
                "settle_response_bazaar_present": True,
                "bazaar_status": "success",
                "discovery_row_present": False,
                "unpaid_request_status": row["unpaid_status"],
                "resource_url": row["resource"],
            }
            if row["error"] is not None:
                obs["unpaid_request_status"] = None
            result = diagnose(obs)
            by_status.setdefault(row["unpaid_status"], set()).add(
                result["diagnosis"]
            )
        self.assertEqual(by_status[402], {"success_but_not_indexed"})
        self.assertEqual(
            by_status[400], {"unpaid_400_body_validation_before_payment_gate"}
        )
        self.assertEqual(by_status[None], {"success_but_not_indexed"})


class PaytoClusteringTests(unittest.TestCase):
    """The publisher's own correction (#3045 comment 5371343985): netloc is
    the wrong independence key — one operator can wear several hostnames,
    so `multi_instance` must be computed over `payTo` clusters, and the
    reducer must accept route-level rows (several rows per host) without a
    netloc-uniqueness guard. The publisher's numbers on our committed 49
    rows are the oracle: 49 netlocs, 42 payTo clusters, and four named
    multi-hostname groups."""

    @classmethod
    def setUpClass(cls):
        fixtures = Path(__file__).parent / "fixtures"
        cls.census = json.loads(
            (fixtures / "x402_census_rows_2026-08-21.json").read_text()
        )
        cls.rows = cls.census["rows"]
        cls.annotation = json.loads(
            (fixtures / "x402_census_payto_clusters_2026-08-21.json").read_text()
        )
        cls.payto = {}
        for cluster in cls.annotation["clusters"]:
            for resource in cluster["resources"]:
                cls.payto[resource] = cluster["cluster"]

    def _annotated(self):
        return [
            dict(row, payto=self.payto.get(row["resource"]))
            for row in self.rows
        ]

    def test_annotation_covers_exactly_the_published_multi_hostname_groups(self):
        self.assertEqual(len(self.payto), 11)
        self.assertEqual(len(self.annotation["clusters"]), 4)

    def test_payto_clusters_reproduce_the_publishers_count(self):
        summary = summarize_census_rows(self._annotated())
        self.assertEqual(summary["rows"], 49)
        self.assertEqual(summary["distinct_hosts"], 49)
        self.assertEqual(summary["distinct_payto_clusters"], 42)

    def test_netloc_over_counts_independence_by_seven(self):
        summary = summarize_census_rows(self._annotated())
        self.assertEqual(
            summary["distinct_hosts"] - summary["distinct_payto_clusters"], 7
        )

    def test_missing_payto_is_its_own_cluster_not_an_invisible_merge(self):
        """The publisher's explicit request: a row with no `payTo` must be
        its own cluster — a silent fallback to hostnames reintroduces the
        same error at a smaller n. Dropping the label from a GROUP member
        must split it out of the group (43 clusters), not merge it back
        by hostname."""
        annotated = self._annotated()
        target = next(
            i
            for i, r in enumerate(annotated)
            if r["resource"] == "https://ebay.use.x402atlas.com/sold"
        )
        annotated[target].pop("payto")
        summary = summarize_census_rows(annotated)
        self.assertEqual(summary["rows"], 49)
        self.assertEqual(summary["distinct_payto_clusters"], 43)
        # 38 unannotated singletons + the popped group member.
        self.assertEqual(summary["rows_without_payto"], 39)

    def test_second_400_on_the_same_payto_does_not_upgrade_the_rule(self):
        """The false-upgrade trap: two hostnames of ONE operator both
        answering 400 is a single independent instance. The upgrade fires
        only on a second distinct payTo cluster. Synthetic rows isolate
        the rule; the real fixture carries exactly one 400."""
        same_operator = [
            {
                "resource": "https://host-a.example/v1/tool",
                "declared_verb": "GET",
                "unpaid_status": 400,
                "error": None,
                "payto": "0xsame…",
            },
            {
                "resource": "https://host-b.example/v1/tool",
                "declared_verb": "POST",
                "unpaid_status": 400,
                "error": None,
                "payto": "0xsame…",
            },
        ]
        summary = summarize_census_rows(same_operator)
        self.assertEqual(len(summary["field_observed_400_rows"]), 2)
        self.assertEqual(summary["field_observed_400_distinct_clusters"], 1)
        self.assertEqual(summary["field_observed_400_rule_status"], "single_instance")

        stranger = {
            "resource": "https://host-c.example/v1/tool",
            "declared_verb": "GET",
            "unpaid_status": 400,
            "error": None,
            "payto": "0xother…",
        }
        summary = summarize_census_rows(same_operator + [stranger])
        self.assertEqual(summary["field_observed_400_distinct_clusters"], 2)
        self.assertEqual(summary["field_observed_400_rule_status"], "multi_instance")

    def test_route_level_rows_are_accepted_not_rejected(self):
        """The un-deduped route-level set is several rows per netloc by
        design; a duplicate-host guard would reject the very data the
        publisher told us to run. Duplicate rows must be allowed and
        netloc must remain a reported display field, never the key."""
        annotated = self._annotated()
        extra = dict(annotated[0])
        extra["resource"] = annotated[0]["resource"] + "-route2"
        summary = summarize_census_rows(annotated + [extra])
        self.assertEqual(summary["rows"], 50)
        self.assertEqual(summary["distinct_hosts"], 49)

    def test_publisher_called_out_splits_are_not_merged(self):
        """railway.app / zeroclick.io / hotels.use.x402atlas.com stay
        separate clusters: shared hosting is not a shared operator."""
        summary = summarize_census_rows(self._annotated())
        self.assertEqual(
            summary["distinct_payto_clusters"],
            summary["distinct_hosts"] - 7,
        )


if __name__ == "__main__":
    unittest.main()
