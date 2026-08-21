# Deploy runbook — x402 diagnose endpoint ($0.50/call, native USDC on Base)

Status: **PREPARED, NOT DEPLOYED.** Everything below except the two
Halli-controlled decisions (hosting platform acceptance + domain) is already
built and locally verified. This runbook exists so the deploy is one session
of mechanical steps, not a project.

## What exists today (verified in-repo)

- `x402_endpoint.py` — Starlette app. `POST /diagnose` returns a
  protocol-correct x402 V2 `PAYMENT-REQUIRED` 402 (exact scheme, CAIP-2
  `eip155:8453`, Circle USDC `0x8335…2913`, `payTo` = mission receive-only
  wallet, amount `50000` = $0.50), advertises itself via the Bazaar discovery
  extension, and **fails closed**: without a wired verifier every paid retry
  returns `501 payment_not_verified` with a `PAYMENT-RESPONSE` settlement
  header and charges nobody.
- `test_x402_endpoint.py` — 14 tests, all passing:
  `python3 -m unittest discover -s . -p "test_x402_*.py"` (run from the repo
  root; needs `starlette`, `uvicorn`).
- Live smoke on 127.0.0.1:8787 passed: free 402 flow, fail-closed paid path,
  free `/sample` and `/health`.

## Why fail-closed is the honest state

Verifying an `exact`-scheme EIP-3009 payment requires ECDSA public-key
recovery (`ecrecover` over the transfer-with-authorization digest) plus
balance/nonce checks. The mission runner has no signing or recovery stack
installed, and hand-rolled signature verification is exactly the code you do
not ship untested. So the endpoint advertises the real price and rail but
completes nothing until a real verifier is wired. No fake success responses,
ever.

## Halli-controlled steps (the only blockers)

1. **Hosting platform.** Pick and accept terms on one of:
   - Fly.io (free allowance, `fly.toml` trivial for one process),
   - Railway / Render (similar),
   - any VPS already owned by Halli.
   The mission worker cannot accept platform ToS or create the account.
2. **Domain (optional but recommended).** A stable HTTPS origin, e.g.
   `https://doctor.nighshift-labs.example`, so the resource URL in the 402 is
   durable. A `*.fly.dev` / `*.onrender.com` subdomain is acceptable to start.

## Worker-executable steps once hosting exists

1. Push the repo (already public) and point the platform at
   `x402_endpoint.py` (uvicorn entrypoint, `PORT` env respected).
2. Install the official verifier: `pip install x402` (the
   x402-foundation/python package) and replace `payment_verifier=None` in
   `create_app(...)` at the server entrypoint with the package's
   verify-and-settle flow for scheme `exact`, network `eip155:8453`. The
   `PAYMENT-REQUIRED` object this service emits already matches the package's
   V2 schemas, so no wire changes should be needed. Re-run the 14 tests with
   the verifier stub plus one live $0.50 self-test call.
3. Confirm `GET /health` from the public origin.
4. Hand back to the worker: Bazaar/CDP submission of the resource URL, Nostr
   announcement, and offer-page link updates are all worker-executable and
   prepared in `outreach/posts/`.

## Cost/price sanity

- $0.50/call vs. the lane's $0.50–$5 target: entry price, volume-first.
- Facilitator fees (CDP) are deducted by the facilitator, not this service.
- Kill criterion unchanged: zero payment-path touches beyond publication by
  2026-08-28 kills the paid wrapper; the classifier stays.

## Security notes

- The service is stateless and read-only: it never queries chains, never
  signs, never holds keys. Receiving is via the mission's receive-only wallet
  through the facilitator.
- The only mutable state is the injected verifier; deployments should keep
  the app process unprivileged.
- No secrets in this repo. The wallet address is public chain data.
