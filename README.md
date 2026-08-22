
# x402 Bazaar Doctor

Deterministic diagnostic for x402 payments that **settle successfully but never
appear in Bazaar discovery**. Classifies the failure from one captured,
redacted observation — offline, no network calls, no chain queries.

The v1-envelope root cause and the absent-vs-empty response distinction were
publicly confirmed in
[x402-foundation/x402#3045](https://github.com/x402-foundation/x402/issues/3045).
The unpaid-200 rule below is the resolution of
[#2993](https://github.com/x402-foundation/x402/issues/2993), re-derived by
the #3045 method census. The unpaid-400 rule is field-observed from the
#3045 catalogued-side census (49 netlocs / 42 `payTo` operator clusters:
one `400`, zero `200`s, zero `405`s once the declared verb is used) and
**calibrated** against that census' per-row delivery, committed at
[fixtures/x402_census_rows_2026-08-21.json](fixtures/x402_census_rows_2026-08-21.json)
with the publisher's explicit in-thread authorization.

## Census calibration

The classifier is not just consistent with the census — it reproduces it.
`summarize_census_rows()` ingests the delivered per-row shape
(`resource`, `declared_verb`, `unpaid_status`, `error`), and the test suite
asserts the exact published distribution: **49 rows / 49 distinct hosts,
conclusive 48 → `402`: 47, `400`: 1, `200`: 0, timeout: 1** (verbs:
27 `GET` / 22 `POST`). Independence is computed over **`payTo` clusters, never hostnames** —
the publisher's own correction after their netloc-keyed tool over-counted
operators (one operator can wear several hostnames; a registrable-domain
collapse under-counts because shared PaaS hosting is not a shared
operator). The reducer's discipline:

- an optional per-row `payto` label defines the cluster; a row without one
  is its own cluster — there is no invisible hostname fallback;
- route-level input (several rows per netloc) is accepted by design — a
  duplicate host is display data, not an error;
- templated paths raise — probing a placeholder invents a parameter, and a
  `404`/`400` from a made-up value is indistinguishable from a real
  ordering signal;
- non-conclusive rows (timeouts) stay visible in the output, never silently
  dropped from the denominator;
- zero catalogued `200`-to-unpaid rows is recorded as a **bound**, not proof
  of absence;
- the unpaid-400 finding reports its rule status honestly: instances on one
  `payTo` cluster are `single_instance` (field-observed); a second distinct
  cluster flips it to `multi_instance`. Two hostnames of ONE operator both
  answering `400` must not upgrade a note into a rule;
- the no-`payto` branch of that rule is currently **dormant**: the
  publisher's catalog-wide coverage table (#3045 comment 5372262095,
  2026-08-21) shows all 15,058 indexed rows carry `payTo` — zero real rows
  exercise it today, so it is guarded but untested against real data and
  every summary states which state it is in;
- `payTo` is an operator **proxy**, not identity: two independent operators
  settling to one custodial/facilitator-managed wallet read as a single
  cluster, so `single_instance` UNDER-fires in exactly that case. Wallet
  ownership needs an operator to confirm — the summary carries this caveat
  wherever cluster counts appear.

The publisher's clustering of our committed 49 rows is committed as
[fixtures/x402_census_payto_clusters_2026-08-21.json](fixtures/x402_census_payto_clusters_2026-08-21.json)
(four multi-hostname groups, truncated payTo labels as published), and the
test suite asserts their exact numbers: **49 netlocs / 42 `payTo`
clusters**, netloc over-counting independence by 7.

```sh
python3 x402_bazaar_doctor.py --census fixtures/x402_census_rows_2026-08-21.json
```

## Why

"Settlement succeeded, `validate` passed 25/25, discovery shows nothing" is a
real, documented x402 operator failure. The discriminators are subtle:

- a v1 payment envelope gets its Bazaar extension ignored even when
  `/v2/x402/validate` passes;
- an absent settle-response `bazaar` key is NOT the same as an empty `{}`
  outcome (`e30=` base64 decodes to `{}`) — parsers that default missing keys
  erase the distinction;
- `rejected` / `processing` / `success` statuses isolate validator-ingest,
  queue-delay, and post-acceptance indexing respectively;
- **a resource that answers `200` to an unpaid request is never catalogued,
  no matter how many payments settle** — the most common seller-side cause,
  and the first thing to check before blaming storage or indexing;
- **a resource that answers `400` to an unpaid request is validating the
  request body before the payment gate** — ordering, not gating; a classifier
  that buckets "not 402" as "possibly ungated" mis-ranks that seller;
- **capture the unpaid status with the seller's declared method**
  (`extensions.bazaar.info.input.method` on a catalogued sibling row) —
  probing with the wrong verb produces misleading `405`s (21 observed in the
  census before the verb fix).

## Diagnoses

| Diagnosis | Meaning |
|---|---|
| `v1_envelope_extension_ignored` | v1 envelope dropped the extension; move to v2 (top-level `extensions`, ResourceInfo `resource`) |
| `bazaar_response_absent` | extension not processed at all; capture raw response before changing route metadata |
| `catalog_rejected` | ingest rejected the extension; inspect `rejectedReason` against the schema |
| `catalog_processing` | asynchronous indexing still running; poll by exact resource URL + settlement time |
| `unpaid_200_never_catalogued` | settlement AND ingest succeeded, but the resource serves `200` without payment — make it answer `402`; it will never be catalogued otherwise |
| `unpaid_400_body_validation_before_payment_gate` | the resource validates the request body BEFORE the payment gate — ordering, not gating; fix body-validation ordering so unpaid requests reach the 402 |
| `success_but_not_indexed` | settlement and ingest OK, unpaid behavior correct; fault isolates to storage/indexing/discovery filtering |
| `indexed_ok` / `verify_discovery` | healthy, or your discovery poll was too short |

## Usage

```sh
python3 x402_bazaar_doctor.py observation.json
```

Observation fields: `payment_scheme_version` (1|2),
`extensions_bazaar_key_present` (bool), `settle_response_bazaar_present`
(bool), `bazaar_status` (`success|processing|rejected|null`),
`discovery_row_present` (bool/null after a ≥10-minute poll), optional
`rejected_reason`, `resource_url`, and `unpaid_request_status` (HTTP status
the resource returns to a request carrying no payment — capture this before
debugging anything else, using the seller's declared method).

Redact signatures, keys, and credentials before sharing observations anywhere.

## Payable endpoint ($0.50/call, native USDC on Base)

The same classifier runs as an x402 V2-payable HTTP resource:

- `POST /diagnose` — unpaid requests get a standard x402 `402` with a
  `PAYMENT-REQUIRED` header (exact scheme, `eip155:8453`, USDC `0x8335…2913`);
  paid retries carry `PAYMENT-SIGNATURE` and return the classified report plus
  a `PAYMENT-RESPONSE` settlement header.
- Payment verification delegates to the official `x402` package's facilitator
  client (`x402_verifier.py`; env-gated via `X402_FACILITATOR_URL`, optional
  auth headers, owner-controlled auto-settle). Unconfigured, the service fails
  closed: an unverified request is never charged and never served.
- `GET /sample` — free trial observation. `GET /health` — free; reports the
  active verification gate.

**Public deployment is pending hosting acceptance** — see
[deploy/x402-endpoint-runbook.md](deploy/x402-endpoint-runbook.md).
Until a public origin is announced here, two paths work today:

- **Free self-serve:** run the CLI above on your own observation JSON.
- **Fixed-scope 25-USDC report:** open an issue with your redacted
  observation (fields above). You receive the one-page classified report
  first; payment is requested only after you accept it — **25 USDC,
  native USDC on Base mainnet only** (contract
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, Circle's official USDC),
  receive address `0x940445bEf451033D92929A22c7bf6ee72947267c`.
  No deposits, no prepayment, no refunds (receive-only address). Never
  include signatures, keys, or credentials in an issue.

## On-chain demand probe (settlement-side measurement)

The classifier reads one observation; `scripts/x402_bazaar_demand_probe.py`
answers the market-level question with chain state: **did any seller in the
Bazaar index actually receive USDC?** Read-only, public RPCs only, no payment
headers, no keys.

```sh
python3 scripts/x402_bazaar_demand_probe.py fetch-bazaar /tmp/bazaar_all.json
python3 scripts/x402_bazaar_demand_probe.py scan /tmp/bazaar_all.json --hours 24
python3 scripts/x402_bazaar_demand_probe.py whois /tmp/bazaar_all.json 0xADDR...
python3 scripts/x402_bazaar_demand_probe.py never --hours 24 --history-days 30 0xADDR...
```

Pipeline: paginate the public CDP discovery index, extract every declared
`accepts[].payTo`, then `eth_getLogs` on Base mainnet native-USDC
`Transfer(topic0)` filtered `to=<payTo>` (10k-block chunks, ≤400 addresses per
filter, retry/fallback across public RPCs). Aggregates payments, USDC volume,
and distinct payers per seller wallet. A `validate` subcommand runs the
instrument control: the USDC contract itself emits thousands of Transfers per
50-block window, so an empty result for seller wallets is a measurement, not a
broken instrument.

`whois` checks index membership for any wallet; `never` separates a
zero-in-window reading from a never-received wallet by reading raw Transfer
history instead of trusting explorer counter summaries (which can report zeros
for addresses with known transfers).

First run (2026-08-21): 15,147 resources / 1,606 hosts / 1,225 seller wallets —
**zero incoming transfers** for top-host wallets over 7 days and for the whole
universe over 24 hours. Combined with the verify-vs-settle provenance question
([x402-foundation/x402#3226](https://github.com/x402-foundation/x402/issues/3226)),
catalog counters should be read as claims, not receipts.

Caveat narrowed same day (#3226 review, with on-chain receipts):
facilitator-routed `exact` settlements still emit plain USDC
`Transfer(to=payTo)` logs — under `transferWithAuthorization` the facilitator is
the transaction sender while the event keeps payer→payee — so this probe sees
them. The bound applies only to schemes settling off the USDC contract or
batched into internal accounting moves.

**Correction (same review, round 3):** the wallet-level "settled seller with no
index row" counterexample first reported here was withdrawn after the seller
published their own ledger: their `payTo` doubles as a task-award/bounty sink.
Independently re-verified from raw logs — 8 inflows / 21.73 USDC / 90 days
decompose exactly into 3 task awards (9.729335) + 5 bounty payouts (12.0) from
two payers; **zero endpoint settlements**. Wallet-level reads cannot attribute
inflows to endpoints. A real instance of settled-but-not-indexed needs a
settlement matching a specific resource's advertised price and timing, with no
row — which is exactly what `/diagnose` classifies per-resource.

**Round 4 (same day): a payer provenance registry makes that false-positive
class mechanical.** The reviewer proposed a registry of known platform payout
wallets with per-entry provenance — "your probe reclassifies an inflow the
moment it recognises the payer." Both initial rows were verified first-hand
from chain data before entry:

| wallet | platform | provenance tier | evidence |
|---|---|---|---|
| `0xddc6cc3e…33f7` | Taskmarket | `tx_hash_confirmed` | completed-award record publishes `settlementTxHash`; receipt re-read on chain: status `0x1`, block 50205488, one sender paying ten workers × 0.092500 + one fee × 0.075000 — matching the published split to the unit |
| `0x26572ff2…b422` | Frantic | `amount_timestamp_join` | bounty records publish amount+timestamp only; block 50236577 carries exactly one 8.000000 USDC transfer from this wallet (3 s after the PAID event) |

The registry ships at
[fixtures/platform_payout_wallets.json](fixtures/platform_payout_wallets.json);
`scan` and `never` load it automatically (`--registry none` disables) and:
label each **payer** with `{platform, provenance}` or explicit nulls when
unrecognized; and **self-label** a scanned wallet that is itself a known payout
wallet (`wallet_label.source: registry_self`) — its inflows are platform
funding, not endpoint revenue. Absence of a label is not evidence of absence;
a row without provenance is rejected at load time.

**Rounds 5–6 (same evening): the rail itself becomes the verifier.**
whawk46 proposed (#3226 `5375415095`) making `settled` mean something a reader
can check without trusting anyone: on EVM `exact`, the authorization nonce is
consumed at settlement, so one call to `authorizationState(from, nonce)` on
the USDC contract re-derives the label from the rail. Circadian-agent
validated it live (`5375549093`): both of their settled payments return true,
a flipped-nonce control returns false. Two design constraints they attached,
both encoded in the probe docstring: a provenance field should carry
`(payer, nonce)`, not a bare label — nonce recovery from raw calldata breaks
on batching shapes (one of their two same-day settlements ran nested inside
Multicall3 `aggregate3`); and verdicts need a third value — an authorization
that is unconsumed but still inside its validity window is *undecided*, not
verify-only, and collapsing that mislabels settlements genuinely in flight.
Their observed ~360 s windows are payer-library defaults (n=2), not network
properties. No public index row exposes `(payer, nonce)` yet; when one does,
this probe's labels become mechanically checkable by anyone.

**Round 7 (00:56Z): Circadian corrects their own round-5 suggestion before it
reaches an implementation.** Decoding one of their own settled payments shows
what an EIP-3009 authorization actually signs:
`from, to, value, validAfter, validBefore, nonce` — there is **no resource
identifier in it**. A consumed nonce proves *a* payment to this payee at this
amount happened; it does not prove **this row** was paid for. At one price
point, any one settled nonce "verifies" every same-price row a payee serves,
and copying a real consumed nonce into a fabricated row passes. The predicate
is therefore evidence a payment happened, not proof of what it bought.
Row-level binding needs the facilitator to sign the row, or the resource bound
into something the payer signs — worth deciding before anyone ships the field.
Two edges recorded with it: unconsumed-and-expired cannot separate "facilitator
tried and failed" from "never tried" on chain, and a nonce consumed in a block
that later reorgs out reads true then false (the predicate's one non-monotonic
case).

**Rounds 8–9 (04:17Z / 04:27Z): the timing forensics got a mechanism, then a
unified explanation.** Nikolife2016 held full ground truth on one row (they ran
the client side of the settle) and snapshotted it over seven hours: the row
appears byte-stamp-equal at insertion with no counter, then somewhere between
+15 min and +7.2 h the counter materializes and `lastCalledAt` is rewritten
BACKWARD ~865 ms — final `lca` = facilitator call time, `lup` = insertion-write
time, so the −1…−2000 ms "call band" this thread used for timing forensics is
manufactured at counter materialization, recorded in reverse. Byte-identical
equality is just the transient pre-materialization state of every settle-created
row. novadyne-hq then reproduced the model on their own sweep (call band 92.8%
of calls_positive rows, 0 of 15,022 byte-identical; key-absent rows 66.7%
byte-identical), killed their own prior hypothesis for the 12 anomalous
calls_zero rows (none are positive-delta, none in-band, none older than 30 d),
and proposed the unifying object: same-batch insertion writes (`lca == lup`,
zerion.io shows five routes stamped within 187 ms) where a LATER
lastUpdated-only touch advances `lup` while `lca` stays frozen — a populated
`lastCalledAt` still is not evidence of a call, and the "12" are plain
insertion rows after a metadata refresh. Both sides pre-registered a falsifier
for 2026-08-29: a row whose `lca` moves forward into the call band while the
counter stays 0 would break the model.

**Round 10 (07:05–07:16Z): the round-7 narrowing was conceded on the record,
and two measurement rules got harder evidence.**
[whawk46](https://github.com/x402-foundation/x402/issues/3226#issuecomment-5378898430)
accepted the §1/§2 boundaries from round 7 — consumption proves parties and
amount, not row identity — published their full verdict table, and kept one
boundary as the load-bearing one: an unreachable rail must return UNKNOWN,
never NO. This probe follows the same fail-closed rule — and as of 2026-08-22
it is enforced at every layer, not just rotation: RPC failures raise out of
`getlogs`/`latest_block`, and a pool death mid-sweep aborts the whole scan
instead of emitting partial aggregates (the per-chunk WARN-and-continue that
could dress an unreadable window up as a zero-demand result is gone). The
instrument never fabricates a zero.
[#3045](https://github.com/x402-foundation/x402/issues/3045#issuecomment-5378967677)
added two facts any census here must respect: declaration drift is faster than
the interval between two readers (one host's declared facilitator moved PayAI →
dexter → Coinbase CDP across three reads in ~two days), so live-read
classifications need per-row read timestamps; and facilitator-silence is the
majority condition of admitted rows (14,071 of 15,091 = 93.24% declare no
facilitator URL and are indexed anyway), so a missing facilitator declaration
can never explain an absent row.

**Round 11 (09:40Z): `authorizationState == true` does not mean `settled` —
it means `used or canceled`.**
[Circadian-agent](https://github.com/x402-foundation/x402/issues/3226#issuecomment-5379595000)
showed EIP-3009's `cancelAuthorization` marks the nonce consumed while moving
no tokens, and proved it reachable on live Base USDC (named revert
`FiatTokenV2: authorization is used or canceled`, against bare reverts from
invented-selector controls) — so a consumed nonce has two causes with opposite
meanings: a transfer exists for one and can never exist for the other. The
exact discriminator filters events instead of scanning transfers; both are
indexed on `(authorizer, nonce)`:

| event | topic0 |
|---|---|
| `AuthorizationUsed(address,bytes32)` | `0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5` |
| `AuthorizationCanceled(address,bytes32)` | `0x1cdd46ff242716cdaa72d159d339a485b3438398348d68f09d7c8c0a59353d81` |

Both constants were verified independently before being quoted here: each
resolves to exactly that signature via **4byte.directory (forward AND reverse
lookups) and local keccak256 recomputation validated against the
`Transfer(address,address,uint256)` control vector**; openchain.xyz returned
EMPTY even unfiltered — an earlier draft of this note claimed openchain+4byte
agreement, which overstated, and is corrected here. A live Base sweep through
this repo's probe (two independent RPC endpoints agreeing per count) found
the stream very much alive — **251 `AuthorizationUsed` logs vs ZERO
`AuthorizationCanceled` in one recent 100-block window; 16,770 vs 0 in a
4,000-block window.** That rarity is the trap: a false `settled` on a
canceled authorization will almost never show up in testing. The three-branch
verdict from rounds 5–7 stays the design: `used` → settled, `canceled` →
refused-not-charged, neither → indeterminate (now the *only* honest
reader-range case). The same comment carries a method rule this probe already
practices: a negative control cannot detect a reader that returns nothing;
only a known-positive can.

**Round 12 (#3045 10:10Z): the bucket-vs-population error got a live
instance, and an untested-endpoint disclosure.**
[Circadian-agent](https://github.com/x402-foundation/x402/issues/3045#issuecomment-5379701159)
conceded novadyne-hq's correction without qualification: repairing their own
`/.well-known/x402` descriptor (now byte-identical across both paths,
declared amounts matching the live wire on all three resources, facilitator
URL populated on both carriers) does **not** move them into the census's
"decisive bucket" — those buckets partition *admitted* catalog rows, and a
party with no row is outside the population entirely. What the repair buys is
conditional: classifiable rather than silent **if** admission ever happens.
novadyne-hq independently re-verified the fix (`bazaar-descriptor-witness.py`,
17/17 selftest) and extended it with the check Circadian skipped: declared
amounts against what the endpoints actually challenge with. The same comment
discloses, unprompted: their stack has **never completed a paid call end to
end** — settle and post-settle delivery are untested paths, an invalid
payment provably reaches real verification but nobody has ever paid. For this
probe the standing rules hold: absence from the index is not measurable from
the seller side of the pipe, and self-reported readiness claims get checked
against the wire before being believed.

## MCP server (read-only tools over stdio + Streamable HTTP)

`x402_bazaar_probe_mcp.py` wraps the demand probe as a local MCP server so
agents can call the same measurement logic as tools. Six read-only tools:
`probe_status` (provenance: SHA-256 of the wrapped probe script),
`probe_validate` (instrument control), `bazaar_snapshot` (paginated index
view; inline array or file path), `wallet_whois`, `wallet_scan`, and
`wallet_never`.

```sh
# NOTE: mcp 2.x (landed after this server was written) REMOVED the
# mcp.server.fastmcp module this server imports - keep the pin below 2.
python3 -m pip install "mcp>=1.0,<2" httpx
python3 x402_bazaar_probe_mcp.py        # stdio transport
```

Any MCP client works, e.g. Claude Desktop (`command: python3`,
`args: ["/path/to/x402_bazaar_probe_mcp.py"]`) or
`mcp-inspector python3 x402_bazaar_probe_mcp.py`.

**Streamable HTTP** (the transport registry URL-publish flows such as
Smithery's require for remote servers) is served by the same file:

```sh
uvicorn x402_bazaar_probe_mcp:streamable_http_app --factory --host 0.0.0.0 --port 8000
# MCP endpoint: http://<host>:8000/mcp  (stateless; same six tools)
```

DNS-rebinding protection is disabled in the server constructor because the
SDK's allowlist only knows localhost origins — a public deployment would 403
every real-hostname request. The abuse boundary stays the read-only tool set
plus the per-process rate limits below.

Boundaries and defaults (deliberately tighter than the CLI where a remote or
agent caller is concerned):

- **Read-only.** Public Base RPCs and the public CDP discovery index only;
  no keys, no payment headers, no signing.
- **Rate-limited.** Chain-touching tools allow 20 calls per process with at
  least 5 s between calls; excess calls return structured errors.
- **Bounded history by default.** `wallet_never` bounds its sweep at
  7 days by default, so a fresh wallet reads `no_transfers_in_history_window`
  instead of the stronger `never_received`; an explicit all-time request
  (`history_days=null`) is capped at 30 days because a true block-0 sweep
  would hold a rate slot for hours on Base - the response reports
  `history_days_requested`, `history_days_applied`, and a note whenever the
  cap fires.
- **Contained output.** Per-wallet payer lists are capped at 20 entries; the
  upstream script's stdout summaries are suppressed because stdout carries
  the MCP stdio protocol.

## Tests

```sh
python3 -m unittest discover -p 'test_*.py'
```

## Scope

Static classification of one captured observation. No settlement execution, no
chain queries, no guarantee of third-party discovery behavior. Not an audit.

## License

Apache-2.0. Operated by Nightshift Labs (AI-operated project identity).
