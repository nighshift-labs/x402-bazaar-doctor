"""Tests for scripts/x402_bazaar_demand_probe.py — the on-chain demand instrument.

Encodes the 2026-08-21 filter-shape lesson: seller wallets are EOAs and emit no
events, so a Transfer search MUST target the USDC contract address and select
recipients via topics[2]. A query with address=<seller wallets> returns empty
for every market and looks exactly like "zero demand".
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve()
REPO = _HERE.parents[1] if (_HERE.parents[1] / "scripts" / "x402_bazaar_demand_probe.py").exists() \
    else _HERE.parents[0]
_spec = importlib.util.spec_from_file_location(
    "x402_bazaar_demand_probe", REPO / "scripts" / "x402_bazaar_demand_probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["x402_bazaar_demand_probe"] = probe
_spec.loader.exec_module(probe)

TRANSFER = probe.TRANSFER_TOPIC


def _pad_addr(addr):
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def _log(to_addr, from_addr, amount_units):
    return {
        "address": probe.USDC,
        "topics": [TRANSFER, _pad_addr(from_addr), _pad_addr(to_addr)],
        "data": hex(amount_units),
        "blockNumber": "0x100",
    }


class FakeRpc:
    """Stand-in for httpx.post returning canned JSON-RPC results."""

    def __init__(self, logs):
        self.logs = logs
        self.getlogs_params = []

    def __call__(self, url, payload, timeout=90):
        method = payload["method"]

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self_inner):
                if method == "eth_blockNumber":
                    return {"result": "0x20000"}
                if method == "eth_getLogs":
                    self_inner_capture = self
                    return {"result": self.logs}
                raise AssertionError(f"unexpected method {method}")

        r = R()
        if method == "eth_getLogs":
            self.getlogs_params.append(payload["params"][0])
        return r


PAYTO_A = "0x" + "a" * 40
PAYTO_B = "0x" + "b" * 40
PAYER_1 = "0x" + "1" * 40
PAYER_2 = "0x" + "2" * 40


class ParamRpc:
    """httpx.post stand-in whose getLogs answer depends on the requested window."""

    def __init__(self, getlogs_fn):
        self.getlogs_fn = getlogs_fn
        self.getlogs_params = []

    def __call__(self, url, payload, timeout=90):
        method = payload["method"]

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self_inner):
                if method == "eth_blockNumber":
                    return {"result": "0x20000"}
                if method == "eth_getLogs":
                    return {"result": self.getlogs_fn(payload["params"][0])}
                raise AssertionError(f"unexpected method {method}")

        r = R()
        if method == "eth_getLogs":
            self.getlogs_params.append(payload["params"][0])
        return r


class ScanFilterShapeTests(unittest.TestCase):
    def test_scan_queries_usdc_contract_with_recipient_topic(self):
        """THE regression: recipient filtering must live in topics[2], and the
        event source must be the USDC contract — never the seller wallets."""
        fake = FakeRpc([_log(PAYTO_A, PAYER_1, 1000)])
        with patch.object(probe, "rpc_post", fake):
            result = probe.scan([PAYTO_A], hours=1)
        self.assertTrue(fake.getlogs_params, "scan issued no getLogs queries")
        for p in fake.getlogs_params:
            self.assertEqual(p["address"], probe.USDC,
                             "getLogs must target the USDC contract, not seller wallets")
            self.assertEqual(len(p["topics"]), 3)
            self.assertEqual(p["topics"][0], TRANSFER)
            self.assertIsNone(p["topics"][1])
            self.assertIn(_pad_addr(PAYTO_A), p["topics"][2])
        self.assertEqual(result["total_payments"], 1)
        self.assertEqual(result["addresses_with_incoming_usdc"], 1)

    def test_scan_aggregates_amounts_and_distinct_payers(self):
        fake = FakeRpc([
            _log(PAYTO_A, PAYER_1, 1_000),
            _log(PAYTO_A, PAYER_2, 2_000),
            _log(PAYTO_B, PAYER_1, 5_000_000),
        ])
        with patch.object(probe, "rpc_post", fake):
            result = probe.scan([PAYTO_A, PAYTO_B], hours=1)
        pa = result["per_address"]
        self.assertEqual(pa[PAYTO_A]["payments"], 2)
        self.assertAlmostEqual(pa[PAYTO_A]["usdc_in"], 0.003)
        self.assertEqual(pa[PAYTO_A]["distinct_payers"], 2)
        self.assertEqual(pa[PAYTO_B]["payments"], 1)
        self.assertEqual(result["total_payments"], 3)

    def test_validate_control_targets_usdc_contract(self):
        fake = FakeRpc([_log(PAYTO_A, PAYER_1, 1)])
        with patch.object(probe, "rpc_post", fake):
            self.assertEqual(probe.validate_instrument(), 0)
        p = fake.getlogs_params[0]
        self.assertEqual(p["address"], probe.USDC)


class WhoisTests(unittest.TestCase):
    """whois: is a wallet a seller payTo in the discovery index?"""

    def _bazaar(self):
        return [
            {"resource": "https://a.example.com/x", "accepts": [{"payTo": PAYTO_A, "scheme": "exact"}]},
            {"resource": "https://b.example.com/y", "accepts": [{"payTo": "0x" + "C" * 40, "scheme": "exact"}]},
        ]

    def test_whois_reports_indexed_wallet_with_rows(self):
        rows = probe.whois(self._bazaar(), [PAYTO_A])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wallet"], PAYTO_A)
        self.assertEqual(rows[0]["in_index"], True)
        self.assertEqual(rows[0]["rows"], 1)

    def test_whois_flags_unindexed_wallet(self):
        stranger = "0x" + "d" * 40
        rows = probe.whois(self._bazaar(), [stranger])
        self.assertEqual(rows[0]["wallet"], stranger)
        self.assertEqual(rows[0]["in_index"], False)
        self.assertEqual(rows[0]["rows"], 0)


class PayerRegistryTests(unittest.TestCase):
    """Payer provenance registry (#3226 round 4): recognizing a known platform
    payout wallet turns 'wallet totals cannot attribute inflows' into a
    mechanical label instead of a silent false positive."""

    def _registry_path(self):
        here = _HERE.parent
        for cand in (here / "fixtures" / "platform_payout_wallets.json",
                     here.parent / "fixtures" / "platform_payout_wallets.json"):
            if cand.exists():
                return cand
        self.fail("registry fixture not found next to tests or under fixtures/")

    def test_load_registry_normalizes_addresses(self):
        reg = probe.load_payer_registry(self._registry_path())
        self.assertIn("0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7", reg)
        self.assertIn("0x26572ff23c6c52bfb1a69cb0c9114a8be443b422", reg)
        self.assertEqual(reg["0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7"]["platform"],
                         "Taskmarket")

    def test_load_registry_rejects_entry_without_provenance(self):
        import tempfile
        bad = {"wallets": [{"address": "0x" + "9" * 40, "platform": "X"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(bad, f)
            path = f.name
        with self.assertRaises(ValueError):
            probe.load_payer_registry(path)

    def test_label_payer_recognizes_known_wallet(self):
        reg = probe.load_payer_registry(self._registry_path())
        labeled = probe.label_payers(reg, ["0xDDC6CC3E4D11C1F3527B867C7DAD4ED9869C33F7"])
        self.assertEqual(labeled["0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7"],
                         {"platform": "Taskmarket", "provenance": "tx_hash_confirmed"})

    def test_label_payer_returns_none_for_unknown_wallet(self):
        reg = probe.load_payer_registry(self._registry_path())
        labeled = probe.label_payers(reg, [PAYER_1])
        self.assertIsNone(labeled[PAYER_1])

    def test_scan_labels_known_payer_in_per_address(self):
        # PAYTO_A is paid by the REAL Taskmarket payout wallet (registry hit)
        # and by an unknown wallet (no label) in the same window.
        TM = "0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7"
        fake = FakeRpc([
            _log(PAYTO_A, TM, 92500),
            _log(PAYTO_A, PAYER_2, 1000),
        ])
        with patch.object(probe, "rpc_post", fake):
            result = probe.scan([PAYTO_A], hours=1)
        pa = result["per_address"][PAYTO_A]
        self.assertEqual(pa["payers"], [
            {"payer": PAYER_2, "platform": None, "provenance": None},
            {"payer": TM, "platform": "Taskmarket", "provenance": "tx_hash_confirmed"},
        ])

    def test_scan_without_registry_keeps_payers_unlabeled(self):
        fake = FakeRpc([_log(PAYTO_A, PAYER_1, 1000)])
        with patch.object(probe, "rpc_post", fake):
            result = probe.scan([PAYTO_A], hours=1, registry_path=None)
        self.assertEqual(result["per_address"][PAYTO_A]["payers"],
                         [{"payer": PAYER_1, "platform": None, "provenance": None}])

    def test_never_labels_known_payer_and_unknown_stays_null(self):
        TM = "0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7"
        fake = FakeRpc([_log(PAYTO_A, TM, 92500)])
        with patch.object(probe, "rpc_post", fake):
            result = probe.never_received([PAYTO_A], hours=24)
        payers = result["per_address"][PAYTO_A]["payers"]
        self.assertEqual(payers,
                         [{"payer": TM, "platform": "Taskmarket",
                           "provenance": "tx_hash_confirmed"}])
        # A wallet with no transfers never reaches a payers list at all.
        fake2 = ParamRpc(lambda p: [])
        with patch.object(probe, "rpc_post", fake2):
            result2 = probe.never_received([PAYER_1], hours=24)
        self.assertEqual(result2["per_address"][PAYER_1]["payers"], [])

    def test_never_self_labels_scanned_platform_payout_wallet(self):
        # The Taskmarket payout wallet ITSELF is scanned: its own inflows are
        # platform funding, and the entry must carry that label regardless of
        # who paid it.
        TM = "0xddc6cc3e4d11c1f3527b867c7dad4ed9869c33f7"
        fake = FakeRpc([_log(TM, PAYER_1, 500_000_000)])
        with patch.object(probe, "rpc_post", fake):
            result = probe.never_received([TM], hours=24)
        entry = result["per_address"][TM]
        self.assertEqual(entry["wallet_label"]["platform"], "Taskmarket")
        self.assertEqual(entry["wallet_label"]["provenance"], "tx_hash_confirmed")
        self.assertEqual(entry["wallet_label"]["source"], "registry_self")
        # ...and scan() must do the same.
        fake2 = FakeRpc([_log(TM, PAYER_1, 500_000_000)])
        with patch.object(probe, "rpc_post", fake2):
            result2 = probe.scan([TM], hours=1)
        self.assertEqual(result2["per_address"][TM]["wallet_label"]["platform"],
                         "Taskmarket")

    def test_unlabeled_wallet_has_no_wallet_label_key_content(self):
        fake = FakeRpc([_log(PAYTO_A, PAYER_1, 1000)])
        with patch.object(probe, "rpc_post", fake):
            result = probe.scan([PAYTO_A], hours=1)
        self.assertEqual(result["per_address"][PAYTO_A].get("wallet_label", {}), {})


class NeverReceivedTests(unittest.TestCase):
    """never: zero-in-window vs never-received are different claims (2026-08-21
    #3226 lesson: explorer /counters endpoints can manufacture zeros)."""

    def test_never_received_wallet_scans_full_history_and_reports_class(self):
        # Settlement exists ONLY at an old block (inside full history, outside
        # the recent window) -> wallet is active historically but zero-in-window.
        OLD_BLOCK = 80_000

        def getlogs_fn(p):
            if int(p["fromBlock"], 16) <= OLD_BLOCK <= int(p["toBlock"], 16):
                return [_log(PAYTO_A, PAYER_1, 8_000_000)]
            return []

        fake = ParamRpc(getlogs_fn)
        with patch.object(probe, "rpc_post", fake):
            result = probe.never_received([PAYTO_A], hours=24)
        entry = result["per_address"][PAYTO_A]
        self.assertEqual(entry["classification"], "zero_in_window")
        self.assertEqual(entry["history_payments"], 1)
        self.assertEqual(entry["window_payments"], 0)
        self.assertEqual(result["zero_in_window"], 1)
        self.assertEqual(result["never_received"], 0)

    def test_recently_settled_wallet_classifies_received_in_window(self):
        NEW_BLOCK = 130_500

        def getlogs_fn(p):
            if int(p["fromBlock"], 16) <= NEW_BLOCK <= int(p["toBlock"], 16):
                return [_log(PAYTO_A, PAYER_1, 20_000)]
            return []

        fake = ParamRpc(getlogs_fn)
        with patch.object(probe, "rpc_post", fake):
            result = probe.never_received([PAYTO_A], hours=24)
        entry = result["per_address"][PAYTO_A]
        self.assertEqual(entry["classification"], "received_in_window")
        self.assertEqual(entry["history_payments"], 1)
        self.assertEqual(result["zero_in_window"], 0)
        self.assertEqual(result["never_received"], 0)

    def test_truly_empty_wallet_classifies_never_received(self):
        fake = ParamRpc(lambda p: [])
        with patch.object(probe, "rpc_post", fake):
            result = probe.never_received([PAYTO_B], hours=24)
        entry = result["per_address"][PAYTO_B]
        self.assertEqual(entry["classification"], "never_received")
        self.assertEqual(result["never_received"], 1)
        self.assertEqual(result["zero_in_window"], 0)

    def test_history_scan_keeps_usdc_contract_recipient_topic_shape(self):
        seen = []
        fake = ParamRpc(lambda p: seen.append(p) or [])
        with patch.object(probe, "rpc_post", fake):
            probe.never_received([PAYTO_A], hours=24)
        for p in seen:
            self.assertEqual(p["address"], probe.USDC,
                             "history scan must target the USDC contract")
            self.assertEqual(len(p["topics"]), 3)
            self.assertIn(_pad_addr(PAYTO_A), p["topics"][2])


class _UrlRpc:
    """rpc_post stand-in whose canned answer depends on the endpoint URL."""

    def __init__(self, per_url):
        self.per_url = per_url          # url -> (status_code, body_dict)
        self.calls = []                 # [(url, method)]

    def __call__(self, url, payload, timeout=90):
        self.calls.append((url, payload["method"]))
        status, body = self.per_url[url]

        class R:
            def json(self_inner):
                return body

        r = R()
        r.status_code = status
        return r


class GetlogsFailoverTests(unittest.TestCase):
    """Refusal-aware failover for getlogs (2026-08-22, BACKLOG item 8).

    Observed live: a pooled Base RPC 403s eth_getLogs under burst and serves
    the identical query minutes later; blind round-robin also burned half its
    retry budget on refusals with cooldown sleeps. Contract:
    - an endpoint refusing eth_getLogs (HTTP 403/404/405 or JSON-RPC -32601)
      is dropped for the CURRENT call only — never memoized across calls;
    - refusals rotate immediately without the cooldown sleep;
    - RuntimeError only when every endpoint in the pool is exhausted.
    """

    GOOD = {"jsonrpc": "2.0", "id": 1, "result": []}

    def _params(self):
        return {"address": probe.USDC,
                "topics": [TRANSFER, None, [_pad_addr(PAYTO_A)]],
                "fromBlock": "0x0", "toBlock": "0x10"}

    def test_http_403_marks_endpoint_incapable_and_skips_it(self):
        a, b = "https://rpc-a.example", "https://rpc-b.example"
        fake = _UrlRpc({a: (403, {"error": "forbidden"}), b: (200, self.GOOD)})
        with patch.object(probe, "RPCS", [a, b]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: None):
            logs = probe.getlogs(self._params())
        self.assertEqual(logs, [])
        self.assertEqual(fake.calls.count((a, "eth_getLogs")), 1,
                         "a refusing endpoint must be asked at most once per call")
        self.assertGreaterEqual(fake.calls.count((b, "eth_getLogs")), 1)

    def test_jsonrpc_method_not_found_also_marks_incapable(self):
        a, b = "https://rpc-a.example", "https://rpc-b.example"
        nomethod = {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32601, "message": "method not found"}}
        fake = _UrlRpc({a: (200, nomethod), b: (200, self.GOOD)})
        with patch.object(probe, "RPCS", [a, b]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: None):
            self.assertEqual(probe.getlogs(self._params()), [])
        self.assertEqual(fake.calls.count((a, "eth_getLogs")), 1)

    def test_failover_rotates_to_next_endpoint_without_cooldown_sleep(self):
        # a is getLogs-incapable (403); b rate-limits twice (503) then recovers.
        a, b = "https://rpc-a.example", "https://rpc-b.example"
        seq = {"n": 0}

        def b_answer():
            seq["n"] += 1
            if seq["n"] <= 2:
                return (503, {})
            return (200, self.GOOD)

        class SeqUrlRpc(_UrlRpc):
            def __call__(self, url, payload, timeout=90):
                self.calls.append((url, payload["method"]))
                if url == a:
                    status, body = 403, {}
                else:
                    status, body = b_answer()

                class R:
                    def json(self_inner):
                        return body

                r = R()
                r.status_code = status
                return r

        slept = []
        fake = SeqUrlRpc({a: (403, {}), b: None})
        with patch.object(probe, "RPCS", [a, b]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: slept.append(s)):
            self.assertEqual(probe.getlogs(self._params()), [])
        self.assertEqual(fake.calls.count((a, "eth_getLogs")), 1)
        self.assertEqual(seq["n"], 3)
        # Cooldown applies only to same-endpoint transient errors (the two 503s),
        # never to a capability failover.
        self.assertEqual(len(slept), 2)

    def test_all_endpoints_unusable_still_raises_runtimeerror(self):
        a, b = "https://rpc-a.example", "https://rpc-b.example"
        err = {"jsonrpc": "2.0", "id": 1,
               "error": {"code": -32005, "message": "limit exceeded"}}
        fake = _UrlRpc({a: (200, err), b: (500, {})})
        with patch.object(probe, "RPCS", [a, b]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                probe.getlogs(self._params())

    def test_single_incapable_endpoint_pool_raises_immediately(self):
        a = "https://rpc-a.example"
        fake = _UrlRpc({a: (403, {})})
        with patch.object(probe, "RPCS", [a]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                probe.getlogs(self._params())
        self.assertEqual(len(fake.calls), 1,
                         "an all-incapable pool must not spin retries")

    def test_happy_path_contacts_only_the_first_capable_endpoint(self):
        a = "https://rpc-a.example"
        fake = _UrlRpc({a: (200, self.GOOD)})
        with patch.object(probe, "RPCS", [a, "https://rpc-b.example"]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: None):
            self.assertEqual(probe.getlogs(self._params()), [])
        self.assertEqual(fake.calls, [(a, "eth_getLogs")])

    def test_refusal_is_not_memoized_across_calls(self):
        # Live finding (2026-08-22): a pooled Base RPC 403s eth_getLogs under
        # burst and serves the identical query minutes later. A refusal must
        # therefore poison only the current call — the NEXT call starts fresh
        # and may use the endpoint again.
        a, b = "https://rpc-a.example", "https://rpc-b.example"
        state = {"burst": True}
        good = self.GOOD

        class BurstyRpc(_UrlRpc):
            def __call__(self, url, payload, timeout=90):
                self.calls.append((url, payload["method"]))
                status, body = ((403, {}) if (url == a and state["burst"])
                                else (200, good))

                class R:
                    def json(self_inner):
                        return body

                r = R()
                r.status_code = status
                return r

        fake = BurstyRpc({})
        with patch.object(probe, "RPCS", [a, b]), \
             patch.object(probe, "rpc_post", fake), \
             patch.object(probe.time, "sleep", lambda s: None):
            # During the burst: a refuses once, b serves.
            self.assertEqual(probe.getlogs(self._params()), [])
            burst_calls_a = fake.calls.count((a, "eth_getLogs"))
            self.assertEqual(burst_calls_a, 1)
            # After the burst clears: a is asked again and serves.
            state["burst"] = False
            self.assertEqual(probe.getlogs(self._params()), [])
            self.assertEqual(fake.calls[-1], (a, "eth_getLogs"),
                             "next call must retry the previously-refusing "
                             "endpoint instead of memoizing it as incapable")


if __name__ == "__main__":
    unittest.main()
