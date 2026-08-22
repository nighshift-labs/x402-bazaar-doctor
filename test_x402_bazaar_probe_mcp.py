"""Tests for the read-only MCP wrapper around the demand probe (TDD).

Contract under test:
- tool registration surface (six read-only tools, all described);
- provenance/status tool reports the upstream probe hash;
- every measurement tool runs OFFLINE deterministically against a synthetic
  index fixture with the RPC layer monkeypatched (no network in tests);
- argument validation rejects malformed wallets and absurd windows;
- handler errors are contained into structured JSON (a failing tool must
  never crash the session);
- nothing writes to stdout during handler execution (stdout is the MCP
  transport stream — a stray print corrupts the protocol);
- end-to-end: a real session over the actual script file via the official
  stdio client initializes, lists tools, and completes a call.
"""
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import uvicorn

HERE = Path(__file__).resolve().parent


def _script_path():
    """Locate the MCP wrapper across layouts.

    Monorepo keeps wrapper+test together in tools/; the published repo
    installs the test at root and the wrapper under scripts/.
    """
    for cand in (HERE / "x402_bazaar_probe_mcp.py",
                 HERE / "scripts" / "x402_bazaar_probe_mcp.py"):
        if cand.exists():
            return cand
    raise SystemExit(
        f"x402_bazaar_probe_mcp.py not found near {HERE}")


SCRIPT = _script_path()

EXPECTED_TOOLS = {
    "probe_status",
    "probe_validate",
    "bazaar_snapshot",
    "wallet_whois",
    "wallet_scan",
    "wallet_never",
}

A = "0x" + "a" * 40
B = "0x" + "b" * 40
PAYER = "0x" + "c" * 40
UNKNOWN = "0x" + "d" * 40

BAZAAR = [
    {"resource": "https://a.example.com/api",
     "accepts": [{"scheme": "exact", "payTo": A}]},
    {"resource": "https://b.example.com/api",
     "accepts": [{"scheme": "exact", "payTo": B}]},
]

TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55"
                  "a4df523b3ef")


def _load():
    spec = importlib.util.spec_from_file_location("x402_bazaar_probe_mcp",
                                                  SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _server_python():
    """Interpreter for the stdio E2E session.

    sys.executable (not the hardcoded 'python3'): under a venv the script's
    mcp dependency lives in that venv; a bare 'python3' on PATH would boot
    the server without it and the session would die at initialize().
    """
    return sys.executable


def _topic_addr(addr40):
    return "0x" + "0" * 24 + addr40[2:].lower()


class ProbeMCPTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        # Tests call chain-scan handlers back-to-back; neutralize the
        # production cooldown (a dedicated test exercises it explicitly).
        self._ri = mock.patch.object(self.mod, "SCAN_RATE_MIN_INTERVAL", 0.0)
        self._ri.start()
        self.mod._scan_calls[0] = 0
        self.mod._last_scan_at[0] = 0.0
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        json.dump(BAZAAR, self.tmp)
        self.tmp.close()

    def tearDown(self):
        self._ri.stop()
        Path(self.tmp.name).unlink(missing_ok=True)

    # ---- registration surface -------------------------------------------

    def test_tool_surface(self):
        tools = asyncio.run(self.mod.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertEqual(names, EXPECTED_TOOLS)
        for t in tools:
            self.assertTrue(t.description, f"{t.name} lacks a description")

    def test_probe_status_provenance(self):
        out = json.loads(self._call("probe_status", {}))
        self.assertTrue(out["read_only"])
        self.assertEqual(len(out["upstream_sha256"]), 64)
        self.assertEqual(set(out["tools"]), EXPECTED_TOOLS)

    def test_upstream_probe_hash_matches_disk(self):
        # Derive the upstream from the loaded module - no hardcoded layout.
        want = hashlib.sha256(self.mod.UPSTREAM.read_bytes()).hexdigest()
        out = json.loads(self._call("probe_status", {}))
        self.assertEqual(out["upstream_sha256"], want)

    def test_flat_layout_regression(self):
        """The published repo installs this wrapper under scripts/ next to
        the probe (no parent/scripts path exists). The resolver must find
        the upstream there too, not only in the monorepo's tools/ layout."""
        flat = HERE / ".flat_layout_regression"
        try:
            (flat / "scripts").mkdir(parents=True)
            probe_src = self.mod.UPSTREAM.read_bytes()
            (flat / "scripts" / "x402_bazaar_demand_probe.py").write_bytes(
                probe_src)
            (flat / "scripts" / "x402_bazaar_probe_mcp.py").write_bytes(
                SCRIPT.read_bytes())
            spec = importlib.util.spec_from_file_location(
                "x402_bazaar_probe_mcp_flat",
                flat / "scripts" / "x402_bazaar_probe_mcp.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertTrue(mod.UPSTREAM.exists())
            self.assertEqual(mod.UPSTREAM.parent, flat / "scripts")
        finally:
            import shutil
            shutil.rmtree(flat, ignore_errors=True)

    # ---- snapshot / whois (pure, offline) -------------------------------

    def test_snapshot_inline_and_path_agree(self):
        via_path = json.loads(self._call(
            "bazaar_snapshot", {"index": self.tmp.name}))
        via_inline = json.loads(self._call(
            "bazaar_snapshot", {"index": BAZAAR}))
        self.assertEqual(via_path, via_inline)
        self.assertEqual(via_path["resources_total"], 2)
        hosts = [r["host"] for r in via_path["rows"]]
        self.assertEqual(hosts, ["a.example.com", "b.example.com"])
        self.assertEqual(via_path["rows"][0]["paytos"], [A.lower()])

    def test_snapshot_pagination(self):
        out = json.loads(self._call(
            "bazaar_snapshot", {"index": BAZAAR, "offset": 1, "limit": 1}))
        self.assertEqual(out["returned"], 1)
        self.assertEqual(out["rows"][0]["host"], "b.example.com")

    def test_whois_membership(self):
        out = json.loads(self._call(
            "wallet_whois",
            {"index": BAZAAR, "wallets": [A.upper(), UNKNOWN]}))
        by = {r["wallet"]: r for r in out}
        self.assertTrue(by[A.lower()]["in_index"])
        self.assertEqual(by[A.lower()]["rows"], 1)
        self.assertIn("https://a.example.com/api", by[A.lower()]["resources"])
        self.assertFalse(by[UNKNOWN.lower()]["in_index"])

    # ---- scan / never (RPC monkeypatched) -------------------------------

    def _offline(self, logs_by_call=None):
        """Patch latest_block + getlogs; getlogs returns scripted logs."""
        blk = mock.patch.object(self.mod.PROBE, "latest_block",
                                return_value=100_000_000)
        gl = mock.patch.object(self.mod.PROBE, "getlogs",
                               side_effect=logs_by_call or (lambda p: []))
        return blk, gl

    def test_wallet_scan_zero_window_offline(self):
        blk, gl = self._offline()
        with blk, gl:
            out = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A, B], "hours": 24, "registry": "none"}))
        self.assertEqual(out["addresses_scanned"], 2)
        self.assertEqual(out["addresses_with_incoming_usdc"], 0)
        self.assertEqual(out["total_payments"], 0)
        self.assertEqual(out["window_hours"], 24)

    def test_wallet_scan_records_hit_and_payer(self):
        log = {"topics": [TRANSFER_TOPIC, _topic_addr(PAYER),
                          _topic_addr(A)],
               "data": hex(2_500_000)}  # 2.5 USDC
        blk, gl = self._offline(lambda params: [log])
        with blk, gl:
            out = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A], "hours": 1, "registry": "none"}))
        self.assertEqual(out["total_payments"], 1)
        self.assertAlmostEqual(out["total_usdc"], 2.5)
        row = out["per_address"][A.lower()]
        self.assertEqual(row["distinct_payers"], 1)
        self.assertIsNone(row["payers"][0]["platform"])

    def test_wallet_never_bounded_vs_unbounded_classification(self):
        blk, gl = self._offline()
        with blk, gl:
            bounded = json.loads(self._call(
                "wallet_never",
                {"wallets": [A], "hours": 24, "history_days": 7,
                 "registry": "none"}))
            unbounded = json.loads(self._call(
                "wallet_never",
                {"wallets": [A], "hours": 24, "history_days": None,
                 "registry": "none"}))
        self.assertEqual(bounded["history_bounded"], True)
        self.assertEqual(bounded["per_address"][A.lower()]["classification"],
                         "no_transfers_in_history_window")
        # history_days=null is CAPPED at MAX_HISTORY_DAYS on the remote
        # surface (a true block-0 sweep would hold a rate slot for hours),
        # so the classification is bounded too - never the CLI's
        # never_received claim.
        self.assertEqual(unbounded["history_days_requested"], None)
        self.assertEqual(unbounded["history_days_applied"],
                         self.mod.MAX_HISTORY_DAYS)
        self.assertEqual(unbounded["history_bounded"], True)
        self.assertEqual(unbounded["per_address"][A.lower()]["classification"],
                         "no_transfers_in_history_window")
        self.assertIn("capped", unbounded["note"])

    def test_wallet_never_defaults_are_bounded(self):
        """Remote default must NOT be the CLI's unbounded history sweep."""
        captured = {}

        def fake_never(wallets, hours, history_days=None, registry_path="auto"):
            captured["history_days"] = history_days
            return {"per_address": {}}

        blk = mock.patch.object(self.mod.PROBE, "latest_block",
                                return_value=100_000_000)
        gl = mock.patch.object(self.mod.PROBE, "getlogs",
                               side_effect=lambda p: [])
        nv = mock.patch.object(self.mod.PROBE, "never_received",
                               side_effect=fake_never)
        with blk, gl, nv:
            json.loads(self._call("wallet_never",
                                  {"wallets": [A], "registry": "none"}))
        self.assertEqual(captured["history_days"], 7.0,
                         "remote default must bound the history sweep")

    # ---- instrument control ---------------------------------------------

    def test_validate_ok_and_broken(self):
        blk, gl = self._offline(lambda p: [{"data": "0x01"}])
        with blk, gl:
            ok = json.loads(self._call("probe_validate", {}))
        self.assertTrue(ok["instrument_ok"])
        blk, gl = self._offline(lambda p: [])
        with blk, gl:
            broken = json.loads(self._call("probe_validate", {}))
        self.assertFalse(broken["instrument_ok"])

    # ---- validation + error containment ----------------------------------

    def test_bad_wallet_rejected_structured(self):
        out = json.loads(self._call(
            "wallet_scan", {"wallets": ["0x1234", A], "registry": "none"}))
        self.assertEqual(out["status"], "error")
        self.assertIn("0x1234", out["error"])

    def test_checksummed_wallet_accepted(self):
        """0X-prefix / EIP-55 forms must normalize, not reject."""
        blk, gl = self._offline()
        with blk, gl:
            out = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A.upper()], "hours": 1, "registry": "none"}))
        self.assertNotIn("status", out)
        self.assertEqual(out["addresses_scanned"], 1)

    def test_absurd_window_rejected_structured(self):
        out = json.loads(self._call(
            "wallet_scan", {"wallets": [A], "hours": 99999,
                            "registry": "none"}))
        self.assertEqual(out["status"], "error")

    def test_empty_wallets_rejected(self):
        out = json.loads(self._call("wallet_scan",
                                    {"wallets": [], "registry": "none"}))
        self.assertEqual(out["status"], "error")

    def test_unknown_tool_raises_tool_error(self):
        """FastMCP raises ToolError for unknown tools (framework contract)."""
        with self.assertRaises(Exception) as ctx:
            self._call("definitely_not_a_tool", {})
        self.assertIn("Unknown tool", str(ctx.exception))

    def test_missing_index_path_is_structured_error(self):
        out = json.loads(self._call(
            "bazaar_snapshot", {"index": "/nonexistent/x.json"}))
        self.assertEqual(out["status"], "error")

    def test_handlers_keep_stdout_silent(self):
        """stdout IS the MCP stdio transport: handlers must never print."""
        buf = io.StringIO()
        blk, gl = self._offline()
        with contextlib.redirect_stdout(buf), blk, gl:
            self._call("wallet_scan",
                       {"wallets": [A], "hours": 1, "registry": "none"})
            self._call("wallet_never",
                       {"wallets": [A], "hours": 1, "registry": "none"})
            self._call("probe_status", {})
        self.assertEqual(buf.getvalue(), "",
                         "handler wrote to stdout: would corrupt MCP stdio")

    # ---- rate limiting ----------------------------------------------------

    def test_rate_limit_blocks_after_max_calls(self):
        blk, gl = self._offline()
        with mock.patch.object(self.mod, "SCAN_RATE_MAX_CALLS", 2), blk, gl:
            first = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A], "hours": 1, "registry": "none"}))
            second = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A], "hours": 1, "registry": "none"}))
            third = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A], "hours": 1, "registry": "none"}))
        self.assertNotIn("status", first)
        self.assertNotIn("status", second)
        self.assertEqual(third["status"], "error")
        self.assertIn("rate limit", third["error"])

    def test_cooldown_enforced_between_calls(self):
        blk, gl = self._offline()
        with mock.patch.object(self.mod, "SCAN_RATE_MIN_INTERVAL", 60.0), \
                blk, gl:
            first = json.loads(self._call(
                "wallet_scan",
                {"wallets": [A], "hours": 1, "registry": "none"}))
            second = json.loads(self._call(
                "wallet_never",
                {"wallets": [A], "hours": 1, "registry": "none"}))
        self.assertNotIn("status", first)
        self.assertEqual(second["status"], "error")
        self.assertIn("cooldown", second["error"])

    # ---- helpers ---------------------------------------------------------

    def _call(self, name, args):
        res = asyncio.run(self.mod.mcp.call_tool(name, args))
        # SDK 1.28 returns [content_blocks, structured_mirror]; the JSON text
        # block is content_blocks[0].
        self.assertGreaterEqual(len(res), 1)
        blocks = res[0]
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0].type, "text")
        return blocks[0].text


class StdioEndToEnd(unittest.TestCase):
    """Real session over the actual script file via the official client."""

    def test_session_initialize_list_call(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(
                command=_server_python(), args=[str(SCRIPT)])
            async with stdio_client(params) as (rd, wr):
                async with ClientSession(rd, wr) as sess:
                    await sess.initialize()
                    tools = (await sess.list_tools()).tools
                    self.assertTrue(EXPECTED_TOOLS <=
                                    {t.name for t in tools})
                    res = await sess.call_tool("probe_status", {})
                    payload = json.loads(res.content[0].text)
                    self.assertTrue(payload["read_only"])

        asyncio.run(asyncio.wait_for(run(), timeout=60))


class StreamableHttpEndToEnd(unittest.TestCase):
    """Streamable HTTP surface: ASGI app serves the same six tools."""

    def test_asgi_app_initialize_list_call(self):
        mod = _load()
        app = mod.streamable_http_app()
        self.assertTrue(callable(app))
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def run():
            async with streamablehttp_client(
                    "http://127.0.0.1:8931/mcp") as (rd, wr, _):
                async with ClientSession(rd, wr) as sess:
                    await sess.initialize()
                    tools = (await sess.list_tools()).tools
                    self.assertTrue(EXPECTED_TOOLS <=
                                    {t.name for t in tools})
                    res = await sess.call_tool("probe_status", {})
                    payload = json.loads(res.content[0].text)
                    self.assertTrue(payload["read_only"])

        config = uvicorn.Config(app, host="127.0.0.1", port=8931,
                                log_level="error", lifespan="on")
        server = uvicorn.Server(config)
        # Plain thread, not get_event_loop().run_in_executor: after prior
        # async tests ran, 3.11 has no current event loop in MainThread and
        # the deprecated getter raises. server.run() manages its own loop.
        task = threading.Thread(target=server.run, daemon=True)
        task.start()

        deadline = time.time() + 30
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        try:
            asyncio.run(asyncio.wait_for(run(), timeout=60))
        finally:
            server.should_exit = True
            end = time.time() + 15
            while task.is_alive() and time.time() < end:
                time.sleep(0.05)
            if task.is_alive():
                # Streamable-HTTP shutdown can outwait a lingering SSE GET
                # stream under load; force-exit rather than hang the suite.
                server.should_exit = True
                server.force_exit = True
                task.join(10)
            self.assertFalse(task.is_alive(), "uvicorn did not shut down")


if __name__ == "__main__":
    unittest.main()
