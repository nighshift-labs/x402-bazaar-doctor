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
  python3 scripts/x402_bazaar_demand_probe.py whois /tmp/bazaar_all.json 0xADDR [0xADDR...]
  python3 scripts/x402_bazaar_demand_probe.py never --hours 24 0xADDR [0xADDR...]

2026-08-21 refinements from x402-foundation/x402#3226 review:
- Facilitator-routed `exact` settlements still emit plain USDC Transfer(to=payTo)
  logs (transferWithAuthorization: facilitator is tx sender, payer->payee stays in
  the event), so getLogs-on-to sees them. The old general routing caveat applies
  only to schemes settling off the USDC contract or batched into internal moves.
- A zero-in-window reading and a never-received wallet are different claims;
  explorer /counters endpoints can report zeros for addresses with known transfers.
  The `never` command reads raw Transfer history instead of trusting summaries.
- Wallet-level output cannot attribute inflows to endpoints: a payTo address can
  double as a bounty/task-award sink (#3226 round 3 — a "settled seller" reading
  decomposed exactly into 3 task awards + 5 bounty payouts from two payers, zero
  endpoint revenue). Endpoint-level claims need price-and-amount matching against
  a specific resource, not wallet totals.
- Round-4 mitigation (#3226 comment 5374882218): a REGISTRY of known platform
  payout wallets with per-entry provenance lets anyone reclassify an inflow the
  moment the payer is recognised. Both initial rows were verified first-hand from
  chain data before entry (Taskmarket: published settlementTxHash whose on-chain
  sender is the wallet; Frantic: public amount+timestamp join). `scan` labels
  recognized payers per address (fixtures/platform_payout_wallets.json,
  resolved relative to this script); an UNRECOGNIZED payer carries
  null labels — absence of a label is not evidence of absence.
- Rounds 5–6 (#3226 comments 5375415095 whawk46 / 5375549093 Circadian-agent):
  the rail itself can re-derive a settlement label — `authorizationState(from,
  nonce)` on the USDC contract returns true once an `exact` authorization is
  consumed. Validated live with a flipped-nonce control. Design consequences
  for any provenance field: carry `(payer, nonce)`, not a bare label — nonce
  recovery from raw calldata is shape-dependent (direct USDC
  `transferWithAuthorization` vs nested inside Multicall3 `aggregate3`, both
  observed same day), and a bare label forces every reader to re-implement
  that archaeology; and verdicts need three values (settled /
  refused-not-charged / indeterminate — an unconsumed-but-in-window
  authorization is undecided, not verify-only; observed ~360s payer windows
  are library defaults, n=2). No public index row exposes `(payer, nonce)`
  yet, so this probe records the design rather than implementing it.
- Round 7 (#3226 comment 5376906327 Circadian-agent): self-correction that
  narrows the round-5/6 guarantee — decoding one of their own settled
  payments shows an EIP-3009 authorization signs
  (from, to, value, validAfter, validBefore, nonce) with NO resource
  identifier. A consumed nonce therefore proves *a* payment to this payee
  at this amount happened, not that THIS row was paid for: at one price
  point any settled nonce "verifies" every same-price row the payee serves,
  and copying a real consumed nonce into a fabricated row passes. Row-level
  binding needs the facilitator to sign the row or the resource bound into
  a payer-signed payload — named but undecided design space. Two more edges
  kept explicit: unconsumed-and-expired cannot separate "facilitator tried,
  verification failed" from "never tried" on chain, and a nonce consumed in
  a later-reorged block reads true then false (the predicate's one
  non-monotonic case).
- Rounds 8–9 (#3045 comments 5377827193 Nikolife2016 / 5377864669 novadyne-hq):
  the −1…−2000 ms "call band" is manufactured at COUNTER MATERIALIZATION, not
  written at settle time. Ground-truth witness on one row (payer-side held):
  insertion shows lca == lup (byte-identical) with no counter key; hours later
  the counter lands and lastCalledAt is rewritten BACKWARD to the facilitator
  call time while lastUpdated stays at the insertion write — so a populated
  lastCalledAt still is not evidence of a call, and byte-identical equality is
  merely the transient pre-materialization state of settle-created rows.
  novadyne-hq's unified model for anomalous calls_zero rows: same-batch
  insertion writes (five zerion.io routes stamped within 187 ms) where a later
  lastUpdated-only touch advances lup while lca freezes; pre-registered
  falsifier (2026-08-29): any row whose lca moves FORWARD into the call band
  while its counter stays 0 breaks the model. For this probe: wallet-level
  reads stay valid, but any future timestamp-based inference must treat the
  call band as write-latency recorded in reverse, not as payment timing.
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

RPCS = ["https://mainnet.base.org", "https://base.meowrpc.com"]
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DISCOVERY = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
SECONDS_PER_BLOCK = 2.0
REGISTRY_FILENAME = "platform_payout_wallets.json"


def default_registry_path():
    """Locate the payer registry relative to this script (repo or flat layout)."""
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "fixtures" / REGISTRY_FILENAME,
                 here / "fixtures" / REGISTRY_FILENAME,
                 here.parent / "tools" / "fixtures" / REGISTRY_FILENAME):
        if cand.exists():
            return str(cand)
    return None


def load_payer_registry(path):
    """Load the platform payout wallet registry -> {lower_addr: {platform, provenance}}.

    Raises ValueError on any entry missing address/platform/provenance: a row
    without provenance is exactly the inference-in-disguise this file exists to
    prevent (#3226 round 4).
    """
    data = json.load(open(path))
    reg = {}
    for w in data.get("wallets", []):
        addr = w.get("address")
        if not (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42):
            raise ValueError(f"registry entry without valid address: {w!r}")
        if not w.get("platform") or not w.get("provenance"):
            raise ValueError(f"registry entry without platform+provenance: {addr}")
        reg[addr.lower()] = {
            "platform": w["platform"],
            "provenance": w["provenance"],
            "role": w.get("role"),
        }
    return reg


def _load_registry_arg(registry_path):
    """Resolve the scan --registry argument: None=disabled, 'auto'/missing=default."""
    if registry_path is None or registry_path == "none":
        return {}
    if registry_path in ("", "auto"):
        p = default_registry_path()
        return load_payer_registry(p) if p else {}
    return load_payer_registry(registry_path)


def label_payers(registry_map, payers):
    """Map each payer address to its platform label (or None when unrecognized)."""
    out = {}
    for p in payers:
        hit = registry_map.get(p.lower())
        out[p.lower()] = ({"platform": hit["platform"],
                           "provenance": hit["provenance"]} if hit else None)
    return out


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


def scan(paytos, hours, registry_path="auto"):
    L = latest_block()
    start = L - int(hours * 3600 / SECONDS_PER_BLOCK)
    hits, totals, senders = _scan_window(paytos, start, L)
    registry_map = _load_registry_arg(registry_path)
    result = {
        "measured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": hours,
        "block_window": [start, L],
        "addresses_scanned": len(paytos),
        "addresses_with_incoming_usdc": len(hits),
        "total_payments": sum(hits.values()),
        "total_usdc": round(sum(totals.values()), 6),
        "per_address": {a: {"payments": hits[a], "usdc_in": round(totals[a], 6),
                            "distinct_payers": len(senders[a]),
                            "payers": [
                                {"payer": p,
                                 **(label_payers(registry_map, [p])[p.lower()] or
                                    {"platform": None, "provenance": None})}
                                for p in sorted(senders[a])],
                            # A scanned wallet that IS a known platform payout
                            # wallet is labeled itself: its inflows are platform
                            # funding, not endpoint revenue (#3226 round 4).
                            **({"wallet_label":
                                dict(label_payers(registry_map, [a])[a.lower()],
                                     source="registry_self")
                                if label_payers(registry_map, [a])[a.lower()]
                                else {}}
                               if registry_map else {})}
                        for a in hits},
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


def whois(bazaar, wallets):
    """Index-membership check: which of `wallets` appear as seller payTo rows?"""
    want = {w.lower() for w in wallets}
    rows_by_wallet = defaultdict(list)
    for it in bazaar:
        h = host_of(it.get("resource") or "")
        for acc in it.get("accepts") or []:
            p = acc.get("payTo")
            if isinstance(p, str) and p.lower() in want:
                rows_by_wallet[p.lower()].append({
                    "resource": it.get("resource"),
                    "host": h,
                    "scheme": acc.get("scheme"),
                })
    return [{
        "wallet": w,
        "in_index": w in rows_by_wallet,
        "rows": len(rows_by_wallet.get(w, [])),
        "resources": [r["resource"] for r in rows_by_wallet.get(w, [])][:10],
        "hosts": sorted({r["host"] for r in rows_by_wallet.get(w, []) if r["host"]}),
    } for w in sorted(want)]


def _scan_window(paytos, lo, hi):
    """One windowed Transfer(to=payTo) sweep; returns per-wallet aggregates."""
    hits = defaultdict(int)
    totals = defaultdict(float)
    senders = defaultdict(set)
    CHUNK = 10000       # base.org public RPC max getLogs window
    GROUP = 400         # keep multi-address filters well under payload caps
    cur = lo
    while cur <= hi:
        top = min(cur + CHUNK - 1, hi)
        for gi in range(0, len(paytos), GROUP):
            grp = paytos[gi:gi + GROUP]
            # Seller wallets are EOAs and emit no events: the event source MUST be
            # the USDC contract, with recipients selected via Transfer topics[2].
            # (2026-08-21 lesson: address=<seller wallets> returns empty for any
            # market and fabricates a "zero demand" result.)
            topics = [TRANSFER_TOPIC, None, ["0x" + "0" * 24 + a.lower().lstrip("0x").rjust(40, "0") for a in grp]]
            try:
                logs = getlogs({"address": USDC, "topics": topics,
                                "fromBlock": hex(cur), "toBlock": hex(top)})
            except Exception as e:
                print(f"WARN group@{cur}-{top} idx{gi}: {e}", file=sys.stderr)
                continue
            for lg in logs:
                to = "0x" + lg["topics"][2][-40:].lower()
                hits[to] += 1
                d = lg.get("data", "0x")
                totals[to] += int(d, 16) / 1e6 if d not in ("0x", "") else 0
                senders[to].add("0x" + lg["topics"][1][-40:].lower())
        cur = top + 1
    return hits, totals, senders


def never_received(paytos, hours, history_days=None, registry_path="auto"):
    """Split zero-in-window from never-received by reading Transfer history.

    A 24h zero is one measurement; a wallet that has NEVER received anything is a
    stronger claim. Explorer /counters endpoints can report zeros for addresses
    with known transfers (#3226, 2026-08-21), so this reads raw logs instead.
    With history_days set, the history pass is bounded and an empty history is
    reported as no_transfers_in_history_window — NOT as never_received.
    """
    L = latest_block()
    start = L - int(hours * 3600 / SECONDS_PER_BLOCK)
    if history_days is None:
        hist_start, bounded = 0, False
    else:
        hist_start, bounded = L - int(history_days * 86400 / SECONDS_PER_BLOCK), True
    win_hits, win_totals, win_senders = _scan_window(paytos, start, L)
    hist_hits, hist_totals, hist_senders = _scan_window(paytos, hist_start, L)
    registry_map = _load_registry_arg(registry_path)
    per_address = {}
    for w in paytos:
        recent_n = win_hits.get(w, 0)
        hist_n = hist_hits.get(w, 0)
        if recent_n:
            cls = "received_in_window"
        elif hist_n:
            cls = "zero_in_window"
        else:
            cls = "no_transfers_in_history_window" if bounded else "never_received"
        per_address[w] = {
            "window_payments": recent_n,
            "window_usdc_in": round(win_totals.get(w, 0.0), 6),
            "history_payments": hist_n,
            "history_usdc_in": round(hist_totals.get(w, 0.0), 6),
            "distinct_payers_all_time": len(hist_senders.get(w, set())),
            "payers": [
                {"payer": p,
                 **(label_payers(registry_map, [p])[p.lower()] or
                    {"platform": None, "provenance": None})}
                for p in sorted(hist_senders.get(w, set()))
            ],
            # Self-label when the scanned wallet is itself a known platform
            # payout wallet: its inflows are platform funding (#3226 round 4).
            **({"wallet_label":
                dict(label_payers(registry_map, [w])[w.lower()], source="registry_self")
                if label_payers(registry_map, [w])[w.lower()]
                else {}}
               if registry_map else {}),
            "classification": cls,
        }
    result = {
        "measured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": hours,
        "block_window": [start, L],
        "history_block_start": hist_start,
        "history_bounded": bounded,
        "addresses_scanned": len(paytos),
        "zero_in_window": sum(1 for v in per_address.values()
                              if v["classification"] == "zero_in_window"),
        "never_received": sum(1 for v in per_address.values()
                              if v["classification"] == "never_received"),
        "per_address": per_address,
    }
    summary = {k: v for k, v in result.items() if k != "per_address"}
    print(json.dumps(summary, indent=1))
    return result


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
    s.add_argument("--registry", default="auto",
                   help="payer registry JSON: path, 'auto' (default; repo fixture "
                        "when present) or 'none' to disable labeling")
    v = sub.add_parser("validate")
    w = sub.add_parser("whois")
    w.add_argument("bazaar_json")
    w.add_argument("wallets", nargs="+")
    n = sub.add_parser("never")
    n.add_argument("--hours", type=float, default=24)
    n.add_argument("--history-days", type=float, default=None,
                   help="bound the history pass; empty history is then reported "
                        "as no_transfers_in_history_window, not never_received")
    n.add_argument("--out", default=None)
    n.add_argument("--registry", default="auto",
                   help="payer registry JSON: path, 'auto' (default; repo fixture "
                        "when present) or 'none' to disable labeling")
    n.add_argument("wallets", nargs="+")
    args = ap.parse_args()

    if args.cmd == "fetch-bazaar":
        return fetch_bazaar(args.out)
    if args.cmd == "validate":
        return validate_instrument()
    if args.cmd == "whois":
        bazaar = json.load(open(args.bazaar_json))
        rows = whois(bazaar, args.wallets)
        print(json.dumps(rows, indent=1))
        return 0
    if args.cmd == "never":
        wallets = [w.lower() for w in args.wallets]
        print(f"classifying {len(wallets)} wallets: {args.hours}h window vs history...")
        result = never_received(wallets, args.hours, history_days=args.history_days,
                                registry_path=args.registry)
        if args.out:
            json.dump(result, open(args.out, "w"), indent=1)
            print(f"wrote {args.out}")
        return 0

    bazaar = json.load(open(args.bazaar_json))
    paytos, _hosts = collect_paytos(bazaar, top_hosts=args.top_hosts)
    print(f"scanning {len(paytos)} seller payTo wallets for {args.hours}h of USDC Transfers...")
    result = scan(paytos, args.hours, registry_path=args.registry)
    if args.out:
        json.dump(result, open(args.out, "w"), indent=1)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
