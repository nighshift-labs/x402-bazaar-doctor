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
  `501 payment_not_verified`, nothing charged). The bare uvicorn
  `create_app --factory` entrypoint wires the verifier from the environment
  ONLY when `X402_WIRE_VERIFIER=1` is set — without it the deploy boots
  fail-closed even with facilitator vars present (`/health` carries a loud
  WARNING); with it, missing/malformed facilitator config aborts boot.
- `tools/x402_verifier.py` — **the real verifier, now implemented.** Delegates
  EIP-3009 signature/balance/nonce checks to the official `x402` package's
  HTTP facilitator client. Env-activated:
  - `X402_FACILITATOR_URL` — set it → verify-only gate; unset → fail-closed.
  - `X402_FACILITATOR_HEADERS` — optional JSON object of STATIC auth headers
    (facilitators with a long-lived key only).
  - `X402_FACILITATOR_HEADERS_COMMAND` — JSON argv array run ONCE PER
    REQUEST; stdout parsed as grouped auth-header JSON
    (`{"verify": {...}, "settle": {...}}`). This is the seam for facilitators
    whose auth is minted fresh per call (CDP JWTs). A command that fails at
    startup aborts the boot — fail closed, never unauthenticated.
  - `X402_AUTO_SETTLE=1` — also settle after verification and record the
    transaction hash. Default is verify-only; auto-settle is an owner call.
  - `/health` reports the active gate (`fail-closed` / `verify_only via …` /
    `verify_and_settle via …`).
- `tools/test_x402_endpoint.py` (19 tests) + `tools/test_x402_verifier.py`
  (28 tests) — all passing; full tools suite 440/440 (verified with the
  official `x402==2.20.0` package installed AND without it).
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

   **Concrete recommendation (added 2026-08-21T23:0xZ, pricing verified live):
   Render.** Cheapest path to a live origin, zero upfront cost, GitHub-native
   (the public repo already carries the endpoint at root). Click-list —
   everything after this is worker-executable:

   1. Create account at render.com (accept ToS — Halli step).
   2. New → Web Service → connect GitHub repo `nighshift-labs/x402-bazaar-doctor`
      (root directory). Runtime: Python 3.
      **Build command:** `pip install -r requirements.txt`
      (pinned install set: `x402[all]`, starlette, uvicorn, plus pyjwt and
      cryptography for the CDP mint script — pyjwt/cryptography happen to be
      transitive deps of `x402[all]` today, but a payment path does not run
      on transitive luck).
      **Start command:**
      `uvicorn x402_endpoint:create_app --factory --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`
      (**verified live 23:1xZ**: the app is a factory — `create_app`, not a
      module-level `app`; plain `:app` fails with
      `Attribute "app" not found in module`. Smoke after this fix: `/health`
      200 fail-closed, unpaid `/diagnose` → 402, `/sample` → 200.
      The `--proxy-headers --forwarded-allow-ips='*'` flags are also
      verified-live and REQUIRED behind Render's TLS proxy: without them the
      402 body's `resource` URL is built from `request.url` as
      `http://<internal>` and every paid verification would mismatch. With
      them, `X-Forwarded-Proto: https` + public Host reproduce the exact
      public origin (`https://…onrender.com/diagnose` confirmed). Trusting
      `'*'` is safe here because Render does not expose the service port to
      the public internet — only their proxy reaches it.)
   3. Instance: **Free** ($0/mo, verified on render.com/pricing) is fine for
      the demand measurement; it spins down when idle, so the first call after
      quiet periods pays a cold start. If that proves hostile to agent
      clients, one click to Starter ($7/mo, always-on) — decide on evidence,
      not anticipation.
   4. Add the CDP + verifier env vars from the wiring block below as Render
      environment variables (mark secrets). **`X402_WIRE_VERIFIER=1` is
      REQUIRED** — the bare uvicorn `--factory` call passes no arguments, so
      without that explicit flag the app deliberately boots fail-closed even
      with every facilitator var set (a misconfigured deploy must look broken:
      501 + a WARNING line on `/health`, never silently unpaid). With the flag
      set and the facilitator config missing/malformed, boot ABORTS instead of
      serving 501s. Deploy.
   5. Hand the worker the origin URL (e.g. `https://x402-bazaar-doctor.onrender.com`)
      — worker runs the `/supported` pre-flight, live smoke, directory
      submissions, and announcements.
   6. **Known trade-off of an `onrender.com` origin:** x402-list's $1
      outgoing-payment rule is DOMAIN-based ("vercel.app, workers.dev,
      fly.dev *and similar*"), not instance-tier based — a bare
      `*.onrender.com` URL likely triggers it, and the worker cannot spend.
      Consequence: with only the free subdomain, x402-list explicit
      submission stays Halli-gated (Halli pays the $1 or points decision (d)'s
      custom domain at the service); Bazaar passive import + Smithery + Glama
      remain fully worker-executable. A custom domain clears the fee entirely.

2. **Facilitator account (needed for real Base-mainnet settlement).** Create
   the CDP (coinbase.com/developer-platform) project and generate the API key
   pair. The worker cannot accept the CDP terms. Alternative: any other
   Base-mainnet facilitator service; the verifier only needs a URL + headers.

   Concrete CDP wiring (what "paste the keys" means once created):

   ```bash
   # 1. The mint script SHIPS IN THE PUBLIC REPO at deploy/mint_cdp_jwt.py
   #    (synced with the rest of the deploy set — verified present in the
   #    repo tree; tested: 17 unit tests + subprocess e2e + wire-level proof
   #    against the official x402 client's auth provider). It reads the CDP
   #    key pair from env and prints the grouped auth-header JSON the
   #    verifier expects, fresh per request. Host needs only:
   #    pip install pyjwt cryptography      (no cdp-sdk required)
   #
   # 2. Wire the verifier to it (env, not code):
   #    X402_WIRE_VERIFIER=1                (REQUIRED — see click-list step 4)
   export CDP_API_KEY_ID='<apiKeyId from the CDP console>'
   export CDP_API_KEY_SECRET='<base64 secret from the CDP console>'
   export X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
   export X402_FACILITATOR_HEADERS_COMMAND='["python3", "deploy/mint_cdp_jwt.py"]'

   # 3. Pre-flight on the host before going live:
   python3 deploy/mint_cdp_jwt.py --check   # mints + signature-verifies all
                                            # three route tokens to stderr;
                                            # exits nonzero on any problem
   ```

   The script mints one route-bound JWT per facilitator route (POST /verify,
   POST /settle, GET /supported) because the official x402 client calls
   create_headers() without route context while CDP binds every token to a
   single route via its uri claim — a /verify token cannot authenticate
   /settle. Key material stays in platform secrets/env; stdout carries only
   the grouped header JSON; failures exit nonzero with clean stdout (fail
   closed). Note --check validates structure and signatures locally; it
   cannot detect a well-formed-but-wrong key pair — that surfaces as a 401
   on first use against CDP.

   CDP authenticates with a JWT minted per-request from the API key pair
   (`apiKeyId` / `apiKeySecret` from the CDP console). The verifier's
   `HEADERS_COMMAND` seam exists exactly for this: it shells out per request,
   so tokens never go stale and key material stays inside the mint script /
   platform secrets — the verifier only consumes its stdout. Static
   `X402_FACILITATOR_HEADERS` will NOT work for CDP (a stale JWT means 401
   on the first paid call). If a facilitator service accepts a static key
   header instead, prefer it — fewer moving parts. Either way, confirm the
   target answers `GET /supported` with `exact` +
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
2. Install deps: `pip install -r requirements.txt`.
3. Set env: `X402_WIRE_VERIFIER=1` (**required** for the bare uvicorn factory
   to read the facilitator config at all — without it the app boots
   fail-closed by design), `X402_FACILITATOR_URL=<facilitator base URL>`,
   then either `X402_FACILITATOR_HEADERS=<static JSON>` (static-key
   facilitators) or `X402_FACILITATOR_HEADERS_COMMAND=<JSON argv>`
   (per-request mint, e.g. CDP — see the wiring block above), and only if
   Halli opts in: `X402_AUTO_SETTLE=1`. Check `GET /health` reports the
   expected gate (`fail-closed WARNING: …` means the flag is missing).
4. Re-run the test suites (75 x402-focused tests in the mission repo:
   endpoint 19 + verifier 28 + classifier 28; full tools suite is 440) and
   one live $0.50 self-test call.
5. Directory + Bazaar submission, Nostr announcement, and offer-page link
   updates — all worker-executable; see the next section.

## Post-deploy distribution: directory submission (pinned 2026-08-21T14:11Z)

Two surfaces carry listings; both are worker-executable after deploy.
**C.2 below (agent-tool registries, added 2026-08-21T22:4xZ) is now part of the
same one-pass deploy sequence — see
`research/2026-08-21-non-github-buyer-surface-map.md`.**

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

**C.2 Agent-tool registries (rotation-prep, measured 2026-08-21T22:4xZ).**
The MCP-directory class is live and carries a dense x402 trust-tooling cluster
(direct comparables: ontario-protocol useCount 982, TrustBench 1029). Same
hosting dependency as A — no new Halli decision, same origin unlocks it:

1. **Smithery** (primary): open `https://smithery.ai/new`, enter the public
   HTTPS URL of the MCP wrapper, complete publishing (account flow = Halli
   step, like hosting). Docs: `smithery.ai/docs/build/publish.md`
   (LLM-friendly at `/docs/llms.txt`). Static server card fallback:
   `/.well-known/mcp/server-card.json` if the scan can't auth.
   Search API is public/no-auth (`registry.smithery.ai/servers?q=...`) —
   use it to verify listing lands and to read `useCount` on 08-28.
2. **Glama** (secondary): add-server flow at `glama.ai/mcp/servers/add`;
   public search API `glama.ai/api/mcp/v1/servers?query=...` for verification.
3. Skip mcp.so ($39 paid listing), PulseMCP (submissions paused), OpenTools
   (/submit 404) — measured dead ends, do not re-probe blind.
4. Product shape: wrap the existing demand-probe subcommands
   (`fetch-bazaar/scan/validate/whois/never`) as read-only MCP tools —
   deterministic, keyless, already 99-tests-green upstream logic.
5. Measure on 08-28 together with payment-path touches: x402-list pickup,
   Smithery `useCount`, Glama presence.

## Cost/price sanity

- Default $0.50/call stands until Halli acks otherwise. Worker recommendation
  on record (2026-08-21 self-eval): launch at **$0.05** — peer median is
  $0.01, Verification peers $0.01–$0.05; against the measured-empty market,
  $0.05 measures demand for the diagnostic while $0.50 mostly measures price
  friction. Reprice upward on first repeat payer.
- Facilitator fees are set by the facilitator provider, not this service.
- Kill criterion unchanged: zero payment-path touches beyond publication by
  deploy+7d kills the paid wrapper; the classifier stays.

### Pricing-swap checklist (execute immediately on any pricing ack)

The reprice itself is one constant; everything else is consistency. USDC
amounts are 6-decimal smallest units: dollars × 1_000_000, so $0.50 →
`"50000"` and $0.05 → `"5000"`. Then update every surface in one pass:

1. `tools/x402_endpoint.py`: `PAYWALL_AMOUNT` (`50000`→`5000`) +
   `PAYWALL_PRICE_HUMAN` (`"$0.50 USDC"`→`"$0.05 USDC"`) + module docstring.
2. `tools/test_x402_endpoint.py` + any fixture asserting amount/price text.
3. Runbook title + lines mentioning `$0.50` (self-test call line included).
4. `deliverables/x402-bazaar-doctor-offer.md` machine-path line.
5. Queued Nostr endpoint post (`outreach/posts/2026-08-21-x402-endpoint-machine-path.txt`)
   — MUST land before rollover publication if the ack precedes it.
6. The staged x402-list submission curl's `notes` field ("$0.50/call").
7. Full tools suite green + py_compile before calling it done.

## Security notes

- The service is stateless and read-only: it never queries chains, never
  signs, never holds keys. Verification and settlement are performed by the
  configured facilitator; receiving is via the mission's receive-only wallet.
- `X402_FACILITATOR_HEADERS` may contain API-key material and
  `X402_FACILITATOR_HEADERS_COMMAND` embeds a command line: set both as
  platform secrets, never in code or git. This repo stays secret-free; the
  wallet address is public chain data.
- The only mutable state is the injected verifier; deployments should keep
  the app process unprivileged.
- Default mode is verify-only; enabling `X402_AUTO_SETTLE` is an explicit
  owner decision recorded at deploy time.
