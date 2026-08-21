# Deploy runbook — x402 diagnose endpoint ($0.50/call, native USDC on Base)

Status: **CODE-COMPLETE INCLUDING THE REAL VERIFIER; NOT DEPLOYED.** The only
remaining Halli-controlled decisions are hosting-platform acceptance and
(optional) domain. Everything else — including the payment verification that
used to be a deploy-time unknown — is now built, tested, and live-smoked.

## What exists today (verified in-repo)

- `tools/x402_endpoint.py` — Starlette app. `POST /diagnose` returns a
  protocol-correct x402 V2 `PAYMENT-REQUIRED` 402 (exact scheme, CAIP-2
  `eip155:8453`, Circle USDC `0x8335…2913`, `payTo` = mission receive-only
  wallet, amount `50000` = $0.50), advertises itself via the Bazaar discovery
  extension, and **fails closed** without a wired verifier (paid retry →
  `501 payment_not_verified`, nothing charged).
- `tools/x402_verifier.py` — **the real verifier, now implemented.** Delegates
  EIP-3009 signature/balance/nonce checks to the official `x402` package's
  HTTP facilitator client. Env-activated:
  - `X402_FACILITATOR_URL` — set it → verify-only gate; unset → fail-closed.
  - `X402_FACILITATOR_HEADERS` — optional JSON object with extra auth headers
    (e.g. CDP API key pairs).
  - `X402_AUTO_SETTLE=1` — also settle after verification and record the
    transaction hash. Default is verify-only; auto-settle is an owner call.
  - `/health` reports the active gate (`fail-closed` / `verify_only via …` /
    `verify_and_settle via …`).
- `tools/test_x402_endpoint.py` (14 tests) + `tools/test_x402_verifier.py`
  (22 tests) — all passing; full tools suite 362/362.
- Live smoke on 127.0.0.1:8787 with the verifier active against the real
  x402.org facilitator: `/health` 200 showing `verify_only via
  https://x402.org/facilitator`; unpaid `/diagnose` → 402; paid retry with a
  fake signature → reached the real facilitator and was rejected (402 +
  honest `PAYMENT-RESPONSE`); `/sample` 200.

## Deploy-critical fact learned 2026-08-21 (live-tested)

The free public facilitator at `https://x402.org/facilitator` does **not**
support scheme `exact` on Base **mainnet** (`eip155:8453`) — its
`/supported` list carries `exact` only for testnets (Base Sepolia
`eip155:84532`, stellar/hedera testnet, …). A paid retry routed there fails
verification with `No facilitator registered for scheme: exact and network:
eip155:8453`. Therefore production needs a Base-mainnet-capable facilitator,
which in practice means **Coinbase CDP's facilitator** (auth via API key
headers — exactly what `X402_FACILITATOR_HEADERS` exists for).

## Halli-controlled steps (the only blockers)

1. **Hosting platform.** Pick and accept terms on one of: Fly.io, Railway,
   Render, or any VPS already owned by Halli. The worker cannot accept
   platform ToS or create the account.
2. **Facilitator account (needed for real Base-mainnet settlement).** Create
   the CDP (coinbase.com/developer-platform) project and generate the API key
   pair. The worker cannot accept the CDP terms. Alternative: any other
   Base-mainnet facilitator service; the verifier only needs a URL + headers.

   Concrete CDP wiring (what "paste the keys" means once created):

   ```
   X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
   X402_FACILITATOR_HEADERS={"Authorization":"Bearer <access-token>"}
   ```

   CDP authenticates with a JWT minted per-request from the API key pair
   (`apiKeyId` / `apiKeySecret` from the CDP console), so the header above is
   generated at deploy time, not pasted as a static secret. If the platform
   cannot run the JWT mint step, prefer a facilitator service that accepts a
   static key header — the verifier treats headers as opaque JSON either way.
   Confirm the target answers `GET /supported` with `exact` +
   `eip155:8453` before wiring; the free `https://x402.org/facilitator`
   fails this check (testnet-only for `exact`).
3. **Domain (optional).** A stable HTTPS origin so the resource URL in the
   402 is durable; a `*.fly.dev` / `*.onrender.com` subdomain is fine to
   start.
4. **Submitter contact email (one decision, needed before directory
   submission).** The x402-list.com submission API requires a contact email.
   Preferred: a dedicated project address on a domain/mailbox Halli controls
   (e.g. an address on the domain that ends up hosting the endpoint). If none
   exists and none will be created, say so and the worker submits with the
   project identity string in `notes` only after Halli names an address —
   the worker cannot create mailboxes or use a personal identity.

## Worker-executable steps once hosting + facilitator exist

1. Push the repo (already public) and point the platform at
   `tools/x402_endpoint.py` (uvicorn entrypoint, `PORT` env respected).
2. Install deps: `pip install "x402[all]" starlette uvicorn`.
3. Set env: `X402_FACILITATOR_URL=<facilitator base URL>`,
   `X402_FACILITATOR_HEADERS=<JSON auth headers>`, and only if Halli opts
   in: `X402_AUTO_SETTLE=1`. Check `GET /health` reports the expected gate.
4. Re-run the test suites (44 tests in this repo; the mission tools suite is
   367) and one live $0.50 self-test call.
5. Directory + Bazaar submission, Nostr announcement, and offer-page link
   updates — all worker-executable; see the next section.

## Post-deploy distribution: directory submission (pinned 2026-08-21T14:11Z)

Two surfaces carry listings; both are worker-executable after deploy.

**A. x402-list.com (explicit submission, verified live this date).**

- Auto-probes each listed endpoint path for HTTP 402, then manual review.
- Pricing rules (from /submit and /llms.txt, read live): free from the
  mission's own domain; **$1 one-off x402-payment if the service URL is on a
  free compute host** (vercel.app, workers.dev, fly.dev "and similar") — buys
  queue placement, never a listing, never refunded; $0.50 anti-spam fee if
  resubmitting within 14 days of a rejection; static hosts (github.io) and
  dev tunnels (ngrok, trycloudflare) rejected at any price. The outgoing $1
  is **not worker-executable** (receive-only posture) — one more reason the
  hosted origin should be the mission's own domain or paid hosting.
- Exact call (fill origin + email from Halli's decisions above):

  ```bash
  curl -X POST https://x402-list.com/api/v1/submit \
    -H 'Content-Type: application/json' -d @- <<'EOF'
  {
    "url": "<ORIGIN>",
    "email": "<HALLI-CHOSEN PROJECT EMAIL>",
    "service_name": "x402 Bazaar Doctor",
    "description": "Diagnostics endpoint: why a settled x402 payment has no Bazaar discovery row. Deterministic offline classifier; returns diagnosis + recommended actions.",
    "website_url": "https://github.com/nighshift-labs/x402-bazaar-doctor",
    "category": "Verification",
    "endpoints": ["/diagnose"],
    "notes": "AI-operated project identity nighshift-labs. Free self-serve classifier at the repo; this endpoint is the $0.50/call machine path."
  }
  EOF
  ```

- Valid categories (live `/api/v1/categories`, 2026-08-21): AI, Blockchain,
  Compute, Content, Data, Finance, Verification, Other. Schema:
  `POST /api/v1/submit` (`ServiceSubmissionRequest`; required: url, email,
  service_name, description, website_url, category, endpoints).

**B. x402 Bazaar (passive but verify it).** x402-list auto-imports Bazaar rows
(`imported:bazaar` provenance), and our own 49-operator census shows catalogued
rows answer **402 unpaid** with the declared verb — which `/diagnose` does by
construction (verified protocol-correct 402 in live smoke). After the first
real settlement, poll discovery by exact resource URL for ≥10 minutes
(classifier rule), then check whether x402-list has picked the service up;
if not, surface A covers it explicitly.

**C. Then:** publish the prepared Nostr endpoint post
(`outreach/posts/2026-08-21-x402-endpoint-machine-path.txt`) with the real
origin, and swap the offer page's "pending hosting" line for the live URL.

## Cost/price sanity

- $0.50/call vs. the lane's $0.50–$5 target: entry price, volume-first.
- Facilitator fees are set by the facilitator provider, not this service.
- Kill criterion unchanged: zero payment-path touches beyond publication by
  2026-08-28 kills the paid wrapper; the classifier stays.

## Security notes

- The service is stateless and read-only: it never queries chains, never
  signs, never holds keys. Verification and settlement are performed by the
  configured facilitator; receiving is via the mission's receive-only wallet.
- `X402_FACILITATOR_HEADERS` may contain API-key material: set it as a
  platform secret, never in code or git. This repo stays secret-free; the
  wallet address is public chain data.
- The only mutable state is the injected verifier; deployments should keep
  the app process unprivileged.
- Default mode is verify-only; enabling `X402_AUTO_SETTLE` is an explicit
  owner decision recorded at deploy time.
