"""Tests for deploy/mint_cdp_jwt.py — CDP JWT mint script for the x402 endpoint.

Pins the CDP auth contract exactly as documented at
docs.cdp.coinbase.com/api-reference/v2/authentication (read 2026-08-21):
Ed25519 seed signing, header {alg: EdDSA, typ: JWT, kid, nonce}, claims
{sub, iss: cdp, aud: [cdp_service], nbf, exp, uri: "METHOD host path"}.
Also pins the grouped stdout shape that tools/x402_verifier.py's
X402_FACILITATOR_HEADERS_COMMAND seam parses, and the fail-closed behavior
(bad env → nonzero exit, nothing on stdout).
"""
import base64
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_HERE = Path(__file__).resolve()
REPO = _HERE.parents[1] if (_HERE.parents[1] / "deploy" / "mint_cdp_jwt.py").exists() \
    else _HERE.parents[0]
_spec = importlib.util.spec_from_file_location(
    "mint_cdp_jwt", REPO / "deploy" / "mint_cdp_jwt.py")
mint = importlib.util.module_from_spec(_spec)
sys.modules["mint_cdp_jwt"] = mint
_spec.loader.exec_module(mint)


def _make_keypair():
    """Return (private_key, cdp_style_secret_b64) — seed||public, b64."""
    key = Ed25519PrivateKey.generate()
    pub_bytes = key.public_key().public_bytes_raw()
    seed_bytes = key.private_bytes_raw()
    assert len(seed_bytes) == 32 and len(pub_bytes) == 32
    return key, base64.b64encode(seed_bytes + pub_bytes).decode()


_KEY, _SECRET_B64 = _make_keypair()
KID = "11111111-2222-3333-4444-555555555555"
HOST = "api.cdp.coinbase.com"


def _decode(token: str, key=_KEY.public_key()) -> tuple[dict, dict]:
    """Return (claims, header) with signature verified."""
    claims = pyjwt.decode(token, key, algorithms=["EdDSA"], audience="cdp_service")
    header = pyjwt.get_unverified_header(token)
    return claims, header


class TestLoadSigningKey(unittest.TestCase):
    def test_accepts_cdp_style_64byte_secret(self):
        key = mint.load_signing_key(_SECRET_B64)
        self.assertEqual(
            key.public_key().public_bytes_raw(), _KEY.public_key().public_bytes_raw()
        )

    def test_rejects_wrong_length(self):
        short = base64.b64encode(b"x" * 32).decode()
        with self.assertRaises(mint.MintError):
            mint.load_signing_key(short)

    def test_rejects_non_base64(self):
        with self.assertRaises(mint.MintError):
            mint.load_signing_key("!!!not base64!!!")


class TestMintToken(unittest.TestCase):
    def test_header_matches_cdp_contract(self):
        token = mint.mint_token(_KEY, KID, "POST", "/platform/v2/x402/verify", HOST)
        header = pyjwt.get_unverified_header(token)
        self.assertEqual(header["alg"], "EdDSA")
        self.assertEqual(header["typ"], "JWT")
        self.assertEqual(header["kid"], KID)
        self.assertTrue(header["nonce"])

    def test_claims_match_cdp_contract(self):
        token = mint.mint_token(_KEY, KID, "POST", "/platform/v2/x402/verify", HOST)
        claims, _ = _decode(token)
        self.assertEqual(claims["sub"], KID)
        self.assertEqual(claims["iss"], "cdp")
        self.assertEqual(claims["aud"], ["cdp_service"])
        self.assertEqual(claims["uri"], f"POST {HOST}/platform/v2/x402/verify")
        self.assertGreaterEqual(claims["exp"] - claims["nbf"], mint.DEFAULT_TTL)

    def test_uri_is_route_bound(self):
        t_verify = mint.mint_token(_KEY, KID, "POST", "/platform/v2/x402/verify", HOST)
        t_settle = mint.mint_token(_KEY, KID, "POST", "/platform/v2/x402/settle", HOST)
        c_verify, _ = _decode(t_verify)
        c_settle, _ = _decode(t_settle)
        self.assertNotEqual(c_verify["uri"], c_settle["uri"])

    def test_nonces_unique_across_mints(self):
        tokens = [
            mint.mint_token(_KEY, KID, "POST", "/platform/v2/x402/verify", HOST)
            for _ in range(3)
        ]
        headers = [pyjwt.get_unverified_header(t)["nonce"] for t in tokens]
        self.assertEqual(len(set(headers)), 3)

    def test_signature_verifies_against_derived_public_key(self):
        token = mint.mint_token(_KEY, KID, "GET", "/platform/v2/x402/supported", HOST)
        _decode(token)  # raises if signature invalid

    def test_custom_now_and_ttl(self):
        token = mint.mint_token(
            _KEY, KID, "POST", "/platform/v2/x402/verify", HOST,
            expires_in=60, now=1_000_000,
        )
        claims = pyjwt.decode(
            token, _KEY.public_key(), algorithms=["EdDSA"],
            audience="cdp_service",
            options={"verify_exp": False},  # token is intentionally expired
        )
        self.assertEqual(claims["nbf"], 1_000_000 - 1)
        self.assertEqual(claims["exp"], 1_000_000 + 60)


class TestGroupedHeaders(unittest.TestCase):
    def test_all_three_route_groups_present_and_bound(self):
        grouped = mint.build_grouped_headers(KID, _SECRET_B64, HOST)
        self.assertEqual(set(grouped), {"verify", "settle", "supported"})
        routes = {
            "verify": f"POST {HOST}/platform/v2/x402/verify",
            "settle": f"POST {HOST}/platform/v2/x402/settle",
            "supported": f"GET {HOST}/platform/v2/x402/supported",
        }
        for group, headers in grouped.items():
            self.assertEqual(list(headers), ["Authorization"])
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            claims, _ = _decode(headers["Authorization"].split(" ", 1)[1])
            self.assertEqual(claims["uri"], routes[group])

    def test_each_token_independently_fresh(self):
        grouped = mint.build_grouped_headers(KID, _SECRET_B64, HOST)
        nonces = [
            pyjwt.get_unverified_header(h["Authorization"].split(" ", 1)[1])["nonce"]
            for h in grouped.values()
        ]
        self.assertEqual(len(set(nonces)), 3)


class TestSelfCheck(unittest.TestCase):
    def test_self_check_passes_on_valid_material(self):
        line = mint._self_check(KID, _SECRET_B64, HOST, mint.DEFAULT_TTL)
        self.assertIn("OK", line)
        self.assertIn("3 route-bound", line)

    def test_self_check_fails_on_malformed_secret(self):
        # A well-formed-but-wrong 64-byte Ed25519 key is locally
        # indistinguishable from the real one — it signs and self-verifies
        # consistently; only CDP can tell. Only true malformation is
        # detectable here.
        bad = base64.b64encode(b"\x01" * 63).decode()
        with self.assertRaises(mint.MintError):
            mint._self_check(KID, bad, HOST, mint.DEFAULT_TTL)


class TestMainStdoutContract(unittest.TestCase):
    """The verifier parses stdout as the grouped JSON — pin that exactly."""

    def _run(self, env=None, argv=None):
        import io
        saved = {k: v for k, v in os.environ.items() if k.startswith("CDP_")}
        try:
            for k in list(saved):
                os.environ.pop(k, None)
            for k, v in (env or {}).items():
                os.environ[k] = v
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with patch.object(sys, "stdout", buf_out), \
                 patch.object(sys, "stderr", buf_err):
                rc = mint.main(argv or [])
        finally:
            for k in list(os.environ):
                if k.startswith("CDP_"):
                    del os.environ[k]
            os.environ.update(saved)
        return buf_out.getvalue(), buf_err.getvalue(), rc

    def test_happy_path_stdout_is_exact_grouped_json(self):
        env = {
            "CDP_API_KEY_ID": KID,
            "CDP_API_KEY_SECRET": _SECRET_B64,
        }
        out, err, rc = self._run(env=env)
        self.assertEqual(rc, 0)
        grouped = json.loads(out)
        self.assertEqual(set(grouped), {"verify", "settle", "supported"})
        for group, headers in grouped.items():
            self.assertEqual(list(headers), ["Authorization"])
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
        # stderr stays clean of token material
        self.assertNotIn("eyJ", err)

    def test_missing_env_fails_closed_with_clean_stdout(self):
        out, err, rc = self._run(env={})
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("CDP_API_KEY_ID", err)

    def test_bad_secret_fails_closed_with_clean_stdout(self):
        env = {"CDP_API_KEY_ID": KID, "CDP_API_KEY_SECRET": "!!!bad!!!"}
        out, err, rc = self._run(env=env)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")

    def test_check_mode_writes_diagnostic_to_stderr_only(self):
        env = {"CDP_API_KEY_ID": KID, "CDP_API_KEY_SECRET": _SECRET_B64}
        out, err, rc = self._run(env=env, argv=["--check"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("OK", err)
        self.assertNotIn("eyJ", err)


if __name__ == "__main__":
    unittest.main()
