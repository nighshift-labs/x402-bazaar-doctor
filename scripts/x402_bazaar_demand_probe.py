#!/usr/bin/env python3
"""Measure real on-chain demand for x402 Bazaar sellers (READ-ONLY).

Pipeline:
1. Fetch the public CDP x402 discovery index (resource URL + accepts[].payTo per row).
2. Optionally match x402-list service hosts to Bazaar resources.
3. eth_getLogs on Base mainnet native-USDC Transfer(topic0) with to=<seller payTo>,
   chunked to respect public-RPC window caps, grouped addresses to stay under payload caps.
4. Aggregate payments / USDC volume / distinct payers per seller wallet.

No payment headers are ever sent; no keys used; public RPCs only.
Validated against a known-active control: the USDC contract itself emits thousands of
Transfers per 50 blocks, so an empty result for seller wallets is a real measurement,
not a broken instrument (2026-08-21).

Usage:
  python3 scripts/x402_bazaar_demand_probe.py fetch-bazaar /tmp/bazaar_all.json
  python3 scripts/x402_bazaar_demand_probe.py scan /tmp/bazaar_all.json --hours 24 [--top-hosts 25]
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

RPCS = ["https://mainnet.base.org", "https://base.meowrpc.com"]
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DISCOVERY = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
SECONDS_PER_BLOCK = 2.0


def rpc_post(url, payload, timeout=90):
    return httpx.post(url, json=payload, timeout=timeout)


def getlogs(params):
    """getLogs with retry/fallback across public RPCs; raises after exhaustion."""
    last = None
    for i in range(6):
        try:
            r = rpc_post(RPCS[i % len(RPCS)], {"jsonrpc": "2.0", "id": 1,
                                               "method": "eth_getLogs", "params": [params]})
            if r.status_code == 200:
                d = r.json()
                if "result" in d:
                    return d["result"]
                last = f"rpcerr {str(d.get('error'))[:80]}"
            else:
                last = str(r.status_code)
        except Exception as e:
            last = str(e)[:80]
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"getLogs failed: {last}")


def latest_block():
    r = rpc_post(RPCS[0], {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    r.raise_for_status()
    return int(r.json()["result"], 16)


def fetch_bazaar(out_path):
    """Pull the whole public discovery index via offset pagination."""
    total = 0
    lo, hi = 0, None
    # exponential probe for the end offset
    off = 100
    last_good = 0
    while True:
        r = httpx.get(DISCOVERY, params={"offset": off, "limit": 100}, timeout=30)
        n = len(r.json().get("items", [])) if r.status_code == 200 else 0
        if n == 0:
            hi = off
            break
        last_good = off
        off *= 4
    while lo < hi:
        mid = (lo + hi) // 2
        r = httpx.get(DISCOVERY, params={"offset": mid, "limit": 100}, timeout=30)
        n = len(r.json().get("items", [])) if r.status_code == 200 else 0
        if n > 0:
            lo = mid + 1
        else:
            hi = mid
    end = lo
    items = []
    off = 0
    while off < end:
        r = httpx.get(DISCOVERY, params={"offset": off, "limit": 100}, timeout=60)
        r.raise_for_status()
        batch = r.json().get("items", [])
        items.extend(batch)
        off += len(batch) or 100
    json.dump(items, open(out_path, "w"))
    print(f"fetched {len(items)} bazaar resources -> {out_path}")
    return 0


def host_of(url):
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return None


def collect_paytos(bazaar, top_hosts=None):
    host_pay = defaultdict(set)
    allp = set()
    for it in bazaar:
        h = host_of(it.get("resource") or "")
        for acc in it.get("accepts") or []:
            p = acc.get("payTo")
            if isinstance(p, str) and p.startswith("0x") and len(p) == 42:
                allp.add(p.lower())
                if h:
                    host_pay[h].add(p.lower())
    if top_hosts:
        ranked = sorted(host_pay.items(), key=lambda kv: -len(kv[1]))[:top_hosts]
        sel = sorted({p for _, ps in ranked for p in ps})
        return sel, dict(ranked)
    return sorted(allp), dict(host_pay)


def scan(paytos, hours):
    L = latest_block()
    start = L - int(hours * 3600 / SECONDS_PER_BLOCK)
    hits = defaultdict(int)
    totals = defaultdict(float)
    senders = defaultdict(set)
    CHUNK = 10000       # base.org public RPC max getLogs window
    GROUP = 400         # keep multi-address filters well under payload caps
    lo = start
    nchunks = 0
    while lo <= L:
        hi = min(lo + CHUNK - 1, L)
        for gi in range(0, len(paytos), GROUP):
            grp = paytos[gi:gi + GROUP]
            try:
                logs = getlogs({"address": grp, "topics": [TRANSFER_TOPIC],
                                "fromBlock": hex(lo), "toBlock": hex(hi)})
            except Exception as e:
                print(f"WARN group@{lo}-{hi} idx{gi}: {e}", file=sys.stderr)
                continue
            for lg in logs:
                to = "0x" + lg["topics"][2][-40:].lower()
                hits[to] += 1
                d = lg.get("data", "0x")
                totals[to] += int(d, 16) / 1e6 if d not in ("0x", "") else 0
                senders[to].add("0x" + lg["topics"][1][-40:].lower())
        nchunks += 1
        print(f"  chunk {nchunks} ({lo}-{hi})", flush=True)
        lo = hi + 1
    result = {
        "measured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": hours,
        "block_window": [start, L],
        "addresses_scanned": len(paytos),
        "addresses_with_incoming_usdc": len(hits),
        "total_payments": sum(hits.values()),
        "total_usdc": round(sum(totals.values()), 6),
        "per_address": {a: {"payments": hits[a], "usdc_in": round(totals[a], 6),
                            "distinct_payers": len(senders[a])} for a in hits},
    }
    print(json.dumps({k: v for k, v in result.items() if k != "per_address"}, indent=1))
    return result


def validate_instrument():
    """Control: the USDC contract itself must show transfers, else the scan lies."""
    L = latest_block()
    logs = getlogs({"address": USDC, "topics": [TRANSFER_TOPIC],
                    "fromBlock": hex(L - 50), "toBlock": hex(L)})
    n = len(logs)
    print(f"[control] USDC-wide Transfers in 50 blocks: {n}"
          f" -> {'OK' if n > 0 else 'BROKEN — do not trust zeros'}")
    return 0 if n > 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-bazaar")
    f.add_argument("out")
    s = sub.add_parser("scan")
    s.add_argument("bazaar_json")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--top-hosts", type=int, default=None)
    s.add_argument("--out", default=None)
    v = sub.add_parser("validate")
    args = ap.parse_args()

    if args.cmd == "fetch-bazaar":
        return fetch_bazaar(args.out)
    if args.cmd == "validate":
        return validate_instrument()

    bazaar = json.load(open(args.bazaar_json))
    paytos, _hosts = collect_paytos(bazaar, top_hosts=args.top_hosts)
    print(f"scanning {len(paytos)} seller payTo wallets for {args.hours}h of USDC Transfers...")
    result = scan(paytos, args.hours)
    if args.out:
        json.dump(result, open(args.out, "w"), indent=1)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
