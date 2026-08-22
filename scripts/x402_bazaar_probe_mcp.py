#!/usr/bin/env python3
"""Read-only MCP wrapper around the x402 Bazaar demand probe.

Exposes the deterministic measurement logic of
scripts/x402_bazaar_demand_probe.py as six MCP tools so agent-tool registries
(Smithery / Glama / mcpservers.org) and any MCP client can call it directly:

- probe_status    provenance: upstream probe hash, tool list, read-only flag
- probe_validate  instrument control: does Base USDC actually emit Transfers?
- bazaar_snapshot paginated view of the public CDP discovery index
- wallet_whois    index-membership check for seller payTo wallets
- wallet_scan     USDC inflow aggregates over a recent window
- wallet_never    zero-in-window vs bounded-history vs never-received

Boundaries (unchanged from the CLI):
READ-ONLY: public RPCs and the public CDP discovery endpoint only; no keys,
no payment headers, no signing, no custody, no writes of any kind.
Chain-touching tools are rate-limited per process to keep a public deployment
from hammering free public RPCs.

Transport notes:
- stdout IS the MCP stdio transport stream; the upstream CLI functions print()
  human summaries, so every handler runs with stdout/stderr contained.
- Unlike the CLI default, wallet_never bounds its history sweep (7 days;
  all-time requests are capped at 30 days) so remote callers always get a
  visibly bounded classification rather than an unbounded never_received
  claim.
"""
import contextlib
import hashlib
import io
import json
import re
import time
from pathlib import Path
from typing import Any, List, Optional, Union
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent


def _find_upstream():
    """Locate the wrapped probe across layouts.

    The local monorepo keeps this wrapper in tools/ with the probe under
    scripts/; the published repo installs both under scripts/. Resolve
    whichever layout this file was loaded from so the same file works in
    both places unchanged.
    """
    for cand in (HERE.parent / "scripts" / "x402_bazaar_demand_probe.py",
                 HERE / "x402_bazaar_demand_probe.py"):
        if cand.exists():
            return cand
    raise SystemExit(
        "x402_bazaar_demand_probe.py not found next to "
        f"{Path(__file__).name} (looked in {HERE} and "
        f"{HERE.parent / 'scripts'})")


UPSTREAM = _find_upstream()

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "x402_bazaar_demand_probe", str(UPSTREAM))
PROBE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PROBE)

mcp = FastMCP("x402-bazaar-demand-probe")

# Per-process rate limit for chain-touching tools: max calls AND min seconds
# between calls, enforced jointly. Generous for humans/agents doing real
# diligence; hostile to RPC-hammering loops.
SCAN_RATE_MAX_CALLS = 20
SCAN_RATE_MIN_INTERVAL = 5.0
_last_scan_at = [0.0]
_scan_calls = [0]

MAX_HOURS = 24 * 30          # one month window cap for scan
# Remote history-sweep cap: 30d keeps any single wallet_never call within
# ~1-2 min over public RPCs (10k-block chunks at ~2 s/block). Deeper
# history belongs to the CLI script run offline, not a synchronous tool
# call. Matches the ecosystem's own l30Days measurement vocabulary.
MAX_HISTORY_DAYS = 30
MAX_WALLETS = 1000           # payload cap per call
MAX_PAYERS_PER_WALLET = 20   # per-wallet payer list cap in tool output


def _upstream_sha256():
    return hashlib.sha256(UPSTREAM.read_bytes()).hexdigest()


def _ok(payload):
    return json.dumps(payload, indent=1)


def _err(msg):
    return json.dumps({"status": "error", "error": msg}, indent=1)


def _rate_limit():
    """Return None when allowed, else an error message string."""
    now = time.monotonic()
    if _scan_calls[0] >= SCAN_RATE_MAX_CALLS:
        return (f"process rate limit reached ({SCAN_RATE_MAX_CALLS} "
                f"chain-scan calls); restart the server to continue")
    wait = SCAN_RATE_MIN_INTERVAL - (now - _last_scan_at[0])
    if wait > 0:
        return (f"chain-scan cooldown: retry in {int(wait) + 1}s "
                f"(min {SCAN_RATE_MIN_INTERVAL:.0f}s between calls)")
    return None


def _mark_call():
    _last_scan_at[0] = time.monotonic()
    _scan_calls[0] += 1


def _norm_wallets(wallets):
    if not isinstance(wallets, list) or not wallets:
        raise ValueError("wallets must be a non-empty array of addresses")
    if len(wallets) > MAX_WALLETS:
        raise ValueError(f"too many wallets (max {MAX_WALLETS})")
    out = []
    for w in wallets:
        # Normalize case FIRST: accept 0X-prefixed and EIP-55 checksummed
        # forms; validation happens on the lowered string.
        if (not isinstance(w, str) or not w.lower().startswith("0x")
                or len(w) != 42):
            raise ValueError(f"invalid wallet address: {w!r}")
        out.append(w.lower())
    return sorted(set(out))


def _clamp_hours(hours):
    h = float(hours)
    if not (0 < h <= MAX_HOURS):
        raise ValueError(f"hours must be in (0, {MAX_HOURS}]")
    return h


def _load_index(index):
    """Accept inline JSON array or a path to one."""
    if isinstance(index, (str, Path)):
        p = Path(index)
        if not p.exists():
            raise FileNotFoundError(f"index file not found: {p}")
        data = json.loads(p.read_text())
    else:
        data = index
    if not isinstance(data, list):
        raise ValueError("index must be a JSON array of bazaar resources")
    return data


def _host_of(url):
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return None


def _run_quiet(fn, *a, **kw):
    """Run a probe function, containing its stdout/stderr prints.

    The upstream CLI functions print human summaries; under MCP stdio the
    process stdout IS the transport stream, so those bytes must never leak.
    """
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), \
            contextlib.redirect_stderr(buf_err):
        return fn(*a, **kw)


@mcp.tool()
def probe_status() -> str:
    """Provenance and surface description for this MCP server.

    Returns the SHA-256 of the wrapped demand-probe script (so callers can
    pin exactly which logic served their request), the tool list, and the
    read-only boundary statement.
    """
    return _ok({
        "server": "x402-bazaar-demand-probe",
        "read_only": True,
        "upstream_sha256": _upstream_sha256(),
        "upstream": (f"{UPSTREAM.parent.name}/{UPSTREAM.name} - deterministic "
                     "on-chain demand measurement for x402 Bazaar sellers"),
        "tools": ["probe_status", "probe_validate", "bazaar_snapshot",
                  "wallet_whois", "wallet_scan", "wallet_never"],
        "boundaries": ("read-only public data only: Base mainnet via public "
                       "RPCs + the public CDP discovery index; no keys, no "
                       "payment headers, no signing, no custody"),
        "chain_scan_rate_limit": {
            "max_calls_per_process": SCAN_RATE_MAX_CALLS,
            "min_interval_seconds": SCAN_RATE_MIN_INTERVAL,
            "applies_to": ["wallet_scan", "wallet_never", "probe_validate"],
        },
    })


@mcp.tool()
def probe_validate() -> str:
    """Instrument control: verify the USDC Transfer instrument is live.

    Counts USDC-wide Transfer events over the last 50 blocks on Base mainnet.
    A zero here means the instrument is broken and any zero-demand reading is
    worthless - this is the control that validates every other measurement.
    Rate-limited per process.
    """
    limited = _rate_limit()
    if limited:
        return _err(limited)
    _mark_call()
    rc = _run_quiet(PROBE.validate_instrument)
    out = {
        "instrument_ok": rc == 0,
        "note": ("control passed"
                 if rc == 0 else
                 "control FAILED: zeros from this instrument are meaningless"),
    }
    return _ok(out)


@mcp.tool()
def bazaar_snapshot(index: Union[List[Any], str], offset: int = 0,
                    limit: int = 50) -> str:
    """View the public x402 Bazaar discovery index (paginated, read-only).

    Pass either an inline JSON array of index rows, or a path to a file
    produced by the fetch-bazaar CLI command. Each row is reduced to its
    stable shape: resource URL, host, and seller payTo addresses. Defaults
    to the first 50 rows; use offset/limit to page through.
    """
    try:
        data = _load_index(index)
        offset = int(offset)
        limit = int(limit)
        if offset < 0 or not (1 <= limit <= 200):
            raise ValueError("offset must be >= 0 and limit in [1, 200]")
    except Exception as e:
        return _err(str(e))
    rows = []
    for it in data:
        paytos = sorted({acc.get("payTo").lower()
                         for acc in it.get("accepts") or []
                         if isinstance(acc.get("payTo"), str)
                         and acc.get("payTo").startswith("0x")
                         and len(acc.get("payTo")) == 42})
        rows.append({"resource": it.get("resource"),
                     "host": _host_of(it.get("resource") or ""),
                     "paytos": paytos})
    rows.sort(key=lambda r: (r["resource"] or ""))
    page = rows[offset:offset + limit]
    return _ok({
        "resources_total": len(rows),
        "offset": offset,
        "returned": len(page),
        "rows": page,
        "fetch_hint": ("full pulls can exceed tens of thousands of rows; "
                       "page with offset/limit or fetch offline via the "
                       "fetch-bazaar CLI command"),
    })


@mcp.tool()
def wallet_whois(index: Union[List[Any], str], wallets: List[str]) -> str:
    """Which bazaar index rows point at these seller wallets?

    Answers index-membership: for each wallet, whether it appears as a payTo
    address anywhere in the provided index (inline array or file path), how
    many rows reference it, and up to ten resource URLs plus hosts.
    """
    try:
        want = set(_norm_wallets(wallets))
        data = _load_index(index)
    except Exception as e:
        return _err(str(e))
    rows_by_wallet = {}
    for it in data:
        host = _host_of(it.get("resource") or "")
        for acc in it.get("accepts") or []:
            p = acc.get("payTo")
            if isinstance(p, str) and p.lower() in want:
                rows_by_wallet.setdefault(p.lower(), []).append({
                    "resource": it.get("resource"),
                    "host": host,
                    "scheme": acc.get("scheme"),
                })
    out = [{
        "wallet": w,
        "in_index": w in rows_by_wallet,
        "rows": len(rows_by_wallet.get(w, [])),
        "resources": [r["resource"] for r in rows_by_wallet.get(w, [])][:10],
        "hosts": sorted({r["host"] for r in rows_by_wallet.get(w, [])
                         if r["host"]}),
    } for w in sorted(want)]
    return _ok(out)


@mcp.tool()
def wallet_scan(wallets: List[str], hours: float = 24.0,
                registry: str = "auto") -> str:
    """Measure incoming native-USDC on Base for seller wallets over a window.

    Aggregates eth_getLogs Transfer(to=payTo) events against public RPCs.
    Returns totals plus per-wallet payment counts, USDC sums, distinct payers,
    and payer labels from the platform-payout registry where recognized
    (registry='none' disables labeling). Rate-limited per process;
    hours capped at 720.
    """
    try:
        wl = _norm_wallets(wallets)
        hours = _clamp_hours(hours)
    except Exception as e:
        return _err(str(e))
    limited = _rate_limit()
    if limited:
        return _err(limited)
    _mark_call()
    try:
        result = _run_quiet(PROBE.scan, wl, hours, registry_path=registry)
    except Exception as e:
        return _err(f"scan failed: {e}")
    per = result.get("per_address") or {}
    trimmed = {a: {k: v for k, v in row.items() if k != "payers"}
               for a, row in per.items()}
    for a, row in trimmed.items():
        if row.get("distinct_payers"):
            row["payers_capped_at"] = MAX_PAYERS_PER_WALLET
            row["payers"] = (per[a].get("payers") or [])[:MAX_PAYERS_PER_WALLET]
    return _ok({**{k: v for k, v in result.items() if k != "per_address"},
                "per_address": trimmed})


@mcp.tool()
def wallet_never(wallets: List[str], hours: float = 24.0,
                 history_days: Optional[float] = 7.0,
                 registry: str = "auto") -> str:
    """Classify wallets: received-in-window / zero-in-window / never-received.

    Reads raw Transfer history instead of trusting explorer counters. The
    remote surface bounds sweeps tighter than the CLI: the default is 7
    days, and an all-time request (history_days=null) is capped at 30 days
    because a true block-0 sweep would hold a rate slot for hours on Base.
    Both bounds are reported in the response, so a fresh wallet reads
    no_transfers_in_history_window rather than the stronger never_received;
    the response note states the cap when null was requested. Rate-limited
    per process.
    """
    try:
        wl = _norm_wallets(wallets)
        hours = _clamp_hours(hours)
        hd = None if history_days is None else float(history_days)
        if hd is not None and not (0 < hd <= MAX_HISTORY_DAYS):
            raise ValueError(
                f"history_days must be null or in (0, {MAX_HISTORY_DAYS}]")
        if hd is None:
            # Remote surface: a true all-time sweep reads from block 0
            # (tens of millions of chunks on Base) and would hold a rate
            # slot for hours. Cap it at the max bound and say so in the
            # output - the caller still gets a bounded, honest answer.
            hd = MAX_HISTORY_DAYS
            capped = True
        else:
            capped = False
    except Exception as e:
        return _err(str(e))
    limited = _rate_limit()
    if limited:
        return _err(limited)
    _mark_call()
    try:
        result = _run_quiet(PROBE.never_received, wl, hours,
                            history_days=hd, registry_path=registry)
    except Exception as e:
        return _err(f"classification failed: {e}")
    if capped:
        result["history_days_requested"] = None
        result["history_days_applied"] = MAX_HISTORY_DAYS
        result["note"] = (
            "all-time sweeps are capped at "
            f"{MAX_HISTORY_DAYS:.0f}d on this server; the empty-history "
            "classification is bounded accordingly")
    per = result.get("per_address") or {}
    trimmed = {a: {k: v for k, v in row.items() if k != "payers"}
               for a, row in per.items()}
    for a, row in trimmed.items():
        if row.get("distinct_payers_all_time"):
            row["payers_capped_at"] = MAX_PAYERS_PER_WALLET
            row["payers"] = (per[a].get("payers") or [])[:MAX_PAYERS_PER_WALLET]
    return _ok({**result, "per_address": trimmed})


if __name__ == "__main__":
    mcp.run()
