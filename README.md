# x402 Bazaar Doctor

Deterministic diagnostic for x402 payments that **settle successfully but never
appear in Bazaar discovery**. Classifies the failure from one captured,
redacted observation — offline, no network calls, no chain queries.

The v1-envelope root cause and the absent-vs-empty response distinction were
publicly confirmed in
[x402-foundation/x402#3045](https://github.com/x402-foundation/x402/issues/3045).

## Why

"Settlement succeeded, `validate` passed 25/25, discovery shows nothing" is a
real, documented x402 operator failure. The discriminators are subtle:

- a v1 payment envelope gets its Bazaar extension ignored even when
  `/v2/x402/validate` passes;
- an absent settle-response `bazaar` key is NOT the same as an empty `{}`
  outcome (`e30=` base64 decodes to `{}`) — parsers that default missing keys
  erase the distinction;
- `rejected` / `processing` / `success` statuses isolate validator-ingest,
  queue-delay, and post-acceptance indexing respectively.

## Diagnoses

| Diagnosis | Meaning |
|---|---|
| `v1_envelope_extension_ignored` | v1 envelope dropped the extension; move to v2 (top-level `extensions`, ResourceInfo `resource`) |
| `bazaar_response_absent` | extension not processed at all; capture raw response before changing route metadata |
| `catalog_rejected` | ingest rejected the extension; inspect `rejectedReason` against the schema |
| `catalog_processing` | asynchronous indexing still running; poll by exact resource URL + settlement time |
| `success_but_not_indexed` | settlement and ingest OK; fault is in storage/indexing/discovery filtering |
| `indexed_ok` / `verify_discovery` | healthy, or your discovery poll was too short |

## Usage

```sh
python3 x402_bazaar_doctor.py observation.json
```

Observation fields: `payment_scheme_version` (1|2),
`extensions_bazaar_key_present` (bool), `settle_response_bazaar_present`
(bool), `bazaar_status` (`success|processing|rejected|null`),
`discovery_row_present` (bool/null after a ≥10-minute poll), optional
`rejected_reason`, `resource_url`.

Redact signatures, keys, and credentials before sharing observations anywhere.

## Payable endpoint ($0.50/call, native USDC on Base)

The same classifier runs as an x402 V2-payable HTTP resource:

- `POST /diagnose` — unpaid requests get a standard x402 `402` with a
  `PAYMENT-REQUIRED` header (exact scheme, `eip155:8453`, USDC `0x8335…2913`);
  paid retries carry `PAYMENT-SIGNATURE` and return the classified report plus
  a `PAYMENT-RESPONSE` settlement header.
- `GET /sample` — free trial observation. `GET /health` — free.

Code is live in this repo (`x402_endpoint.py`, `test_x402_endpoint.py`,
14 tests). **Public deployment is pending hosting acceptance** — the service
fails closed until then; see [deploy/x402-endpoint-runbook.md](deploy/x402-endpoint-runbook.md).
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
