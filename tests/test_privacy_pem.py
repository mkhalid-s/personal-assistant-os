"""PEM private-key redaction tests — closes the gap identified in the
block-completeness audit (Aug 2026). Runs apply_privacy_filters directly."""

from __future__ import annotations

import sqlite3
import textwrap
import unittest

from personal_assistant.db import initialize_schema
from personal_assistant.privacy import apply_privacy_filters

# A realistic-looking (but invalid) RSA PEM block for testing.
_FAKE_RSA_KEY = textwrap.dedent("""\
    -----BEGIN RSA PRIVATE KEY-----
    MIIEowIBAAKCAQEA2a2rwplBQLzHPZe5RJr9vNMlYR0f23fHilyCuLTgJhm3KYVV
    YvN+SM0bMLfMBFhSKvXCAFQkEAoFCaRnq/bvFuOEexDalAqCGLzHPZe5RJQTEST
    MFooTESTkeyTESTdataHEREforTESTingPurposesONLYnotARealKeyAtAllXXXX
    -----END RSA PRIVATE KEY-----""")

_FAKE_EC_KEY = textwrap.dedent("""\
    -----BEGIN EC PRIVATE KEY-----
    MHQCAQEEIBkg4TESTKEY123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJ
    -----END EC PRIVATE KEY-----""")

_FAKE_PKCS8_KEY = textwrap.dedent("""\
    -----BEGIN PRIVATE KEY-----
    MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7TESTPKCS8KEY
    -----END PRIVATE KEY-----""")

_FAKE_OPENSSH_KEY = textwrap.dedent("""\
    -----BEGIN OPENSSH PRIVATE KEY-----
    b3BlbnNzaC1rZXktdjEAAAABbm9uZQAAAARub25lAAAABAAAADMAAAALc3NoLXJzYQAAAQ
    TESTOPENSSHKEYDATA123456789AAAA==
    -----END OPENSSH PRIVATE KEY-----""")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_schema(conn)
    return conn


class PemBlockRedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _redact(self, text: str) -> str:
        return apply_privacy_filters(self.conn, text)

    # ── full multi-line blocks ──────────────────────────────────────────────

    def test_rsa_private_key_block_is_redacted(self) -> None:
        result = self._redact(_FAKE_RSA_KEY)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result)
        self.assertNotIn("MIIEowIBAAKCAQEA", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_ec_private_key_block_is_redacted(self) -> None:
        result = self._redact(_FAKE_EC_KEY)
        self.assertNotIn("BEGIN EC PRIVATE KEY", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_pkcs8_private_key_block_is_redacted(self) -> None:
        result = self._redact(_FAKE_PKCS8_KEY)
        self.assertNotIn("BEGIN PRIVATE KEY", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_openssh_private_key_block_is_redacted(self) -> None:
        result = self._redact(_FAKE_OPENSSH_KEY)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_key_embedded_in_prose_is_redacted(self) -> None:
        text = f"Here is the key I found:\n\n{_FAKE_RSA_KEY}\n\nPlease store it safely."
        result = self._redact(text)
        self.assertIn("Here is the key I found", result)
        self.assertIn("Please store it safely", result)
        self.assertNotIn("MIIEowIBAAKCAQEA", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_multiple_keys_in_same_text_both_redacted(self) -> None:
        text = f"{_FAKE_RSA_KEY}\n\nSome text\n\n{_FAKE_EC_KEY}"
        result = self._redact(text)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result)
        self.assertNotIn("BEGIN EC PRIVATE KEY", result)

    # ── truncated / inline header-only ─────────────────────────────────────

    def test_header_only_paste_is_redacted(self) -> None:
        # User pastes just the header line (body cut off mid-paste)
        result = self._redact("key starts with -----BEGIN RSA PRIVATE KEY-----")
        self.assertIn("[REDACTED_SECRET]", result)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result)

    def test_pkcs8_header_only_is_redacted(self) -> None:
        result = self._redact("-----BEGIN PRIVATE KEY-----")
        self.assertIn("[REDACTED_SECRET]", result)

    def test_encrypted_private_key_header_redacted(self) -> None:
        result = self._redact("-----BEGIN ENCRYPTED PRIVATE KEY-----")
        self.assertIn("[REDACTED_SECRET]", result)

    # ── case insensitivity ─────────────────────────────────────────────────

    def test_lowercase_begin_marker_is_redacted(self) -> None:
        result = self._redact("-----begin rsa private key-----")
        self.assertIn("[REDACTED_SECRET]", result)

    # ── policy gate ────────────────────────────────────────────────────────

    def test_pem_not_redacted_when_secrets_disabled(self) -> None:
        self.conn.execute("INSERT INTO assistant_policies (key, value) VALUES ('redact_secrets','false')")
        self.conn.commit()
        result = self._redact(_FAKE_RSA_KEY)
        self.assertIn("BEGIN RSA PRIVATE KEY", result)

    # ── non-private key PEM not affected ───────────────────────────────────

    def test_public_key_pem_not_redacted(self) -> None:
        # Public key PEM blocks are not sensitive — leave them intact
        public_pem = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\n-----END PUBLIC KEY-----"
        result = self._redact(public_pem)
        self.assertIn("BEGIN PUBLIC KEY", result)

    def test_certificate_pem_not_redacted(self) -> None:
        cert_pem = "-----BEGIN CERTIFICATE-----\nMIIDXTCCAkWgAwIBAgIJANOCTEST\n-----END CERTIFICATE-----"
        result = self._redact(cert_pem)
        self.assertIn("BEGIN CERTIFICATE", result)

    # ── existing patterns unaffected ───────────────────────────────────────

    def test_other_secrets_still_redacted_alongside_pem(self) -> None:
        text = f"token ghp_ABCDEFGHIJKLMNOPQRSTUVWX12\n\n{_FAKE_RSA_KEY}"
        result = self._redact(text)
        self.assertNotIn("ghp_", result)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result)
        self.assertEqual(result.count("[REDACTED_SECRET]"), 2)


if __name__ == "__main__":
    unittest.main()
