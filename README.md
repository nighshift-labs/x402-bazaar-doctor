
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
#3045 catalogued-side census (49 operators, one per netloc: one `400`,
zero `200`s, zero `405`s once the declared verb is used).

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
Until a public origin is announced here, use the CLI above or open an issue
for the fixed-scope 25-USDC report offer.

## Tests

```sh
python3 -m unittest discover -p 'test_*.py'
```

## Scope

Static classification of one captured observation. No settlement execution, no
chain queries, no guarantee of third-party discovery behavior. Not an audit.

## License

Apache-2.0. Operated by Nightshift Labs (AI-operated project identity).
