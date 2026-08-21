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


if __name__ == "__main__":
    unittest.main()
