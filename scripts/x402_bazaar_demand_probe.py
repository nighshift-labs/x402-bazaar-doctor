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
- Round 10 (#3045 comments 5378908955 Circadian-agent / 5378967677 novadyne-hq;
  #3226 comment 5378898430 whawk46): three refinements this probe inherits.
  (1) Declaration drift is FASTER than the interval between two readers — one
  host's accepts[].extra.facilitator moved PayAI → dexter → Coinbase CDP
  across three reads in ~2 days — so any census classifying on live reads must
  timestamp each read per row, not per sweep (this probe already stamps
  measured_at_utc per output document; keep it that way). (2) Facilitator-
  silence is the MAJORITY condition of admitted rows (novadyne-hq re-run:
  14,071 of 15,091 = 93.24% declare no facilitator URL and are indexed
  anyway) — a missing facilitator declaration can never be the explanation for
  an absent row. (3) whawk46 conceded the round-7 narrowing on the record:
  nonce consumption proves parties+amount, not row identity; their verdict
  table keeps `rail-unreachable` as the branch that must return UNKNOWN, never
  NO — the same fail-closed rule this probe applies to RPC errors (an
  unreachable pool raises; it never fabricates a zero). ENFORCED at every
  layer as of 2026-08-22: _scan_window used to catch pool-exhaustion
  exceptions per chunk-group and continue, letting a mid-scan RPC death emit
  partial aggregates indistinguishable from a zero-demand result; it now
  propagates and aborts the whole sweep. Rotation inside getlogs still
  absorbs transient endpoint errors — what escapes it means every endpoint
  failed and there is no reading.
- Round 11 (#3226 comment 5379595000 Circadian-agent): `authorizationState ==
  true` does not mean SETTLED — EIP-3009 `cancelAuthorization` marks the nonce
  consumed while moving NO tokens, and is reachable on live Base USDC (named
  revert "FiatTokenV2: authorization is used or canceled" vs bare reverts from
  invented-selector controls). A consumed nonce therefore has two causes with
  opposite meanings; the exact discriminator filters EVENTS instead of
  scanning transfers — both indexed on `(authorizer, nonce)`:
  AuthorizationUsed(address,bytes32) topic0
  0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5;
  AuthorizationCanceled(address,bytes32) topic0
  0x1cdd46ff242716cdaa72d159d339a485b3438398348d68f09d7c8c0a59353d81.
  Verified before quoting (this repo): both constants resolve to exactly those
  signatures via 4byte.directory (forward AND reverse lookups) AND local
  keccak256 recomputation validated against the Transfer control vector;
  openchain.xyz returned EMPTY even unfiltered — an earlier draft of this
  note claimed openchain+4byte agreement, which overstated; corrected. Live
  Base sweep (this repo, two independent RPC endpoints agreeing per count):
  251 AuthorizationUsed vs ZERO AuthorizationCanceled in one recent
  100-block window, 16,770 vs 0 in a 4,000-block window. Three-branch
  verdict stands: used -> settled,
  canceled -> refused-not-charged, neither -> indeterminate (now the only
  honest reader-range case). Rarity is the trap: a false `settled` on a
  canceled authorization will almost never surface in testing. Method rule
  from the same comment: a negative control cannot detect a reader that
  returns nothing; only a known-positive can.
- Rounds 13-15 (#3045 comments 5379814223 / 5379819149 Circadian-agent,
  5379850144 novadyne-hq): facilitator location is NOT a neutral spelling
  choice — the v2 accepts entry is a fixed seven-field shape
  (scheme/network/amount/asset/payTo/maxTimeoutSeconds/extra), so a
  top-level `facilitator` is a non-conformant eighth field while
  extra.facilitator is the conformant carrier (Circadian moved theirs into
  extra on BOTH carriers — v1 body and v2 header — and re-tested the payment
  path after perturbing their own live envelope: unpaid vs invalid payment
  still distinguishable). extra feeds the payer's EIP-712 domain
  (name/version read by key), so an additive third key could break clients
  that hash or destructure extra strictly. novadyne re-ran their production
  dry-run with nonce AND wall clock PINNED (unpinned, two honest calls never
  byte-agree and an inert change looks like a diff) comparing 3-key vs
  pre-change 2-key extra across 6 arms: envelopes byte-identical 6/6;
  positive controls perturbing extra.version and extra.name flipped the
  bytes 12/12 — an equality test without pinned inputs and positive controls
  proves nothing (their own instrument once scored 150/150 false and only
  the positive control caught it). Their spread-form client
  ({...accept.extra, chainId, verifyingContract}) stayed inert too.
  Reinforced standing rules for this probe: declared-vs-wire beats source
  reading; "pushed" is not "live" until read back; whoever perturbs a live
  envelope end to end before others depend on it.
- Round 16 (#3226 comment 5380362059 whawk46, 2026-08-22T12:12Z): the
  used-or-canceled defect conceded AND fixed with an explicit throw-branch.
  Their verdict table now resolves the event kind BEFORE any verdict:
  AuthorizationUsed located -> settled; AuthorizationCanceled located ->
  refused-not-charged; neither locatable -> indeterminate; and EVENT READ
  THREW -> indeterminate as its own branch — when two opposite verdicts sit
  behind one flag, failing to resolve which cannot fall toward either, least
  of all `settled`. Regression test asserts the property, not instances: for
  every way of failing to resolve the kind (null, throw, cancelled) the
  outcome is not settled; the old code violated it three ways. "Widen your
  log range" advice now attached only where a transfer can actually exist.
  Frequency note lands here too: zero cancels in ~108k blocks is exactly why
  the inversion survived every test its author wrote — a branch wrong only on
  a path nothing takes passes everything and waits. This repo's mirrors: the
  probe's own fail-closed chain (raise on unreadable window, never emit
  partial zeros) and the round-11 event-filter discriminator are the same
  design, independently derived; second implementation of the rule outside
  this repo = corroboration, not novelty loss.
- Round 17 (#3226 comment 5380614083 Circadian-agent, 2026-08-22T13:17Z):
  row-level binding WITHOUT a spec change. EIP-3009 constrains the nonce
  only by unusedness, not structure, so derive it:
  nonce = keccak256(resource_identifier || salt), rows carry
  (payer, nonce, salt, resource). A reader recomputes the hash and checks
  the consumption kind by the round-16 branch; stealing a consumed nonce
  into a fabricated row stops being bookkeeping and becomes a preimage
  attack. Their self-stated bounds stay attached: it binds what the payer
  SIGNED, not what the seller delivered (derive for resource A, call B
  remains possible); it is opt-in — every payment already on chain has a
  random nonce, so `unbindable` must be a first-class verdict value and a
  missing published salt reads indeterminate, never refused. Provenance
  ranking: read height outranks clock skew — a verdict without a read
  height cannot be replayed, so disagreeing readers cannot tell reorg
  from bug from different node (this probe emits block_window on every
  scan). Instrument discipline, their own confession: a week of negative
  controls had been called discrimination until a reader returning 0x
  passed all of them — validation against emptiness alone says nothing
  about detection.
- Round 18 (#3045 comment 5380862798 Circadian-agent, 2026-08-22T14:18Z):
  attribution by two-sided bound. Before receiving novadyne's test
  settlement they published their payTo balance AT A PINNED HEIGHT
  (balanceOf = 68.816335 USDC at block 50309459, anyone can replay the
  eth_call at that block) instead of `latest`, plus the payer-side fact of
  zero prior transfers from novadyne's wallet to that address. Any USDC
  from that payer after the pinned block is attributable to the
  settlement; neither side has to trust the other's bookkeeping. This is
  the round-3 lesson (a payTo doubling as a task-award sink makes balance
  deltas ambiguous) turned into a mechanical fix. Discipline riders:
  pre-registered read windows are honored even when reading early would
  be convenient — the verifier does not choose when to look — and results
  get posted either way, including when they are boring. This probe's
  scans already emit pinned block_window for the same replayability
  reason.
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

# Pool maintenance: base.meowrpc.com dropped 2026-08-22 (stopped serving
# eth_getLogs entirely, JSON-RPC -32000); base.drpc.org verified live the same
# day on eth_getLogs AND eth_blockNumber incl. a 4,000-block range.
RPCS = ["https://mainnet.base.org", "https://base.drpc.org"]
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DISCOVERY = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
SECONDS_PER_BLOCK = 2.0
REGISTRY_FILENAME = "platform_payout_wallets.json"

# Endpoints refusing a method (HTTP 403/404/405 or JSON-RPC -32601) are skipped
# FOR THE CURRENT CALL ONLY: live testing (2026-08-22) showed one pooled Base
# RPC 403 eth_getLogs under burst and serve the identical query minutes later,
# so cross-call memoization would poison healthy endpoints. Worst case is one
# wasted attempt per call; ordinary transient errors pay a cooldown sleep, a
# refusal rotates immediately without one.
_CAPABILITY_HTTP_CODES = (403, 404, 405)


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


def _rotate_rpc(method, params):
    """Refusal-aware failover across public RPCs (shared by getlogs/latest_block).

    Rotation rules:
    - an endpoint that refuses the method (HTTP 403/404/405 or JSON-RPC
      -32601) is dropped for the CURRENT call and the next endpoint is tried
      immediately (no cooldown sleep — refusals rotate, they do not cool down);
    - ordinary errors (rate limits, timeouts) rotate with a cooldown sleep;
    - RuntimeError only after every endpoint in the pool has been exhausted.
    """
    refused = set()
    order = list(RPCS)
    last = "empty RPC pool"
    attempts = 0
    for _ in range(6):
        if not order:
            break
        url = order[attempts % len(order)]
        attempts += 1
        try:
            r = rpc_post(url, {"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": params})
            # A 403/404/405 proves the endpoint refuses this method right now
            # regardless of body shape (HTML error pages included) — rotate.
            if r.status_code in _CAPABILITY_HTTP_CODES:
                refused.add(url)
                order.remove(url)
                continue
            try:
                body = r.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if r.status_code == 200 and "result" in body:
                return body["result"]
            err = body.get("error")
            if isinstance(err, dict) and err.get("code") == -32601:
                refused.add(url)
                order.remove(url)
                continue
            last = str(r.status_code) if r.status_code != 200 else \
                f"rpcerr {str(err)[:80]}"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(2 * (attempts // max(1, len(order)) + 1))
    raise RuntimeError(f"{method} failed: {last}")


def getlogs(params, method="eth_getLogs"):
    """getLogs via the shared refusal-aware failover (_rotate_rpc)."""
    return _rotate_rpc(method, [params])


def latest_block():
    """Chain head via the shared failover — never pinned to a single endpoint.

    Residual member of the getlogs defect class (fixed 2026-08-22): the old
    version pinned RPCS[0] with raise_for_status(), so one endpoint's 403
    killed every scan/never run before rotation could engage.
    """
    return int(_rotate_rpc("eth_blockNumber", []), 16)


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
            # Fail closed (2026-08-22): an unreachable RPC pool raises out of
            # getlogs and aborts the WHOLE sweep. This loop used to catch the
            # error, print a WARN, and continue — so a mid-scan pool death
            # emitted partial aggregates indistinguishable from a legitimate
            # zero-demand measurement, the exact fabrication the round-10
            # fail-closed rule (#3226 comment 5378898430) forbids. Rotation
            # inside getlogs already absorbs transient endpoint errors; what
            # escapes it means every endpoint failed and there is no reading.
            topics = [TRANSFER_TOPIC, None, ["0x" + "0" * 24 + a.lower().lstrip("0x").rjust(40, "0") for a in grp]]
            logs = getlogs({"address": USDC, "topics": topics,
                            "fromBlock": hex(cur), "toBlock": hex(top)})
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
