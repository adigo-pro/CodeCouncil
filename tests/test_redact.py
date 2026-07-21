"""redact tests: high-confidence credential shapes, and a negative case that
proves ordinary code isn't mangled."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import redact


class TestRedact(unittest.TestCase):
    def test_aws_access_key(self):
        secret = "AKIAABCDEFGHIJKLMNOP"
        out = redact.redact(f"aws_key = '{secret}'")
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:aws-key»", out)

    def test_nvidia_key(self):
        secret = "nvapi-" + "a" * 30
        out = redact.redact(f'NVIDIA_API_KEY="{secret}"')
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:nvidia-key»", out)

    def test_openai_style_key(self):
        secret = "sk-" + "B1c2D3e4F5g6H7i8J9k0L1m2"
        out = redact.redact(f"OPENAI_API_KEY={secret}")
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:openai-key»", out)

    def test_github_pat_classic(self):
        secret = "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7"
        out = redact.redact(f"remote: https://x-access-token:{secret}@github.com/foo/bar.git")
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:github-token»", out)

    def test_github_pat_fine_grained(self):
        secret = "github_pat_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
        out = redact.redact(secret)
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:github-token»", out)

    def test_slack_token(self):
        secret = "xoxb-" + "1234567890-abcdefghij"
        out = redact.redact(f"SLACK_TOKEN={secret}")
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:slack-token»", out)

    def test_pem_private_key_block(self):
        body_line = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 3
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            f"{body_line}\n"
            "-----END PRIVATE KEY-----\n"
        )
        out = redact.redact(pem)
        self.assertNotIn(body_line, out)
        self.assertIn("-----BEGIN PRIVATE KEY-----", out)
        self.assertIn("-----END PRIVATE KEY-----", out)
        self.assertIn("«REDACTED:private-key»", out)

    def test_assignment_keeps_variable_name(self):
        secret = "abcdefghijklmnopqrstuvwxyz012345"
        out = redact.redact(f'api_key = "{secret}"')
        self.assertNotIn(secret, out)
        self.assertIn("api_key", out)
        self.assertIn("«REDACTED:assignment»", out)

    def test_assignment_password_colon_form(self):
        secret = "Tr0ub4dor-and-3-more-Xyz"
        out = redact.redact(f"password: {secret}")
        self.assertNotIn(secret, out)
        self.assertIn("password", out)
        self.assertIn("«REDACTED:assignment»", out)

    def test_assignment_compound_names(self):
        # The keyword needs only to appear somewhere in the name -- this is
        # the single most common env-var naming convention (SECRET_KEY,
        # DB_PASSWORD, ...) and was missed by a stricter "name IS the
        # keyword" pattern.
        secret = "abcdefghijklmnopqrstuvwxyz012345"
        for name in ("SECRET_KEY", "DB_PASSWORD", "CLIENT_SECRET", "JWT_SECRET", "AUTH_TOKEN"):
            with self.subTest(name=name):
                out = redact.redact(f'{name} = "{secret}"')
                self.assertNotIn(secret, out)
                self.assertIn(name, out)
                self.assertIn("«REDACTED:assignment»", out)

    def test_assignment_incidental_substring_name_is_a_known_tradeoff(self):
        # "monkey" merely CONTAINS "key" -- broadening the keyword match to
        # catch compound names like SECRET_KEY means a name like this also
        # triggers, since there is no cheap way to tell "key as a real
        # credential-name component" from "key as a substring of an English
        # word" without name-boundary heuristics that would themselves risk
        # missing real compound names. This is accepted as a tolerable false
        # positive, guarded by the 16+ char value-length requirement: a
        # `monkey = <16+ char opaque-looking value>` assignment is itself an
        # unusual enough shape that flagging it costs little.
        secret = "abcdefghijklmnopqrstuvwxyz012345"
        out = redact.redact(f'monkey = "{secret}"')
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:assignment»", out)

    def test_assignment_special_char_tail_fully_redacted(self):
        # A special character right after the qualifying 16+ char charset
        # run must not leave a tail of the secret exposed past the marker --
        # special characters are common in exactly the password class this
        # pattern targets.
        out = redact.redact('PASSWORD = "abcdefgh12345678!tail%rest"')
        self.assertIn("«REDACTED:assignment»", out)
        self.assertNotIn("!tail", out)
        self.assertNotIn("tail", out)
        self.assertNotIn("rest", out)

    def test_assignment_stops_at_closing_quote(self):
        out = redact.redact('SECRET_KEY = "abcdefgh12345678!x"y')
        self.assertIn("«REDACTED:assignment»", out)
        self.assertNotIn("!x", out)
        self.assertTrue(out.endswith('"y'))

    def test_assignment_url_value_not_redacted(self):
        # "https" is only 5 chars before the "://" breaks the charset run --
        # well under the 16-char floor -- so a URL-shaped value must never
        # trigger redaction, even though its name contains "token".
        text = 'token_endpoint = "https://auth.example.com/oauth/token"'
        self.assertEqual(redact.redact(text), text)

    def test_short_value_not_redacted(self):
        # Below the 16-char floor -- not high-confidence, must be left alone.
        text = 'token = "short1"'
        self.assertEqual(redact.redact(text), text)

    def test_ordinary_comment_with_keyword_untouched(self):
        text = "# grab a token from the config and pass it along, no secret here"
        out = redact.redact(text)
        self.assertEqual(out, text)
        self.assertNotIn("REDACTED", out)

    def test_ordinary_code_untouched(self):
        text = (
            "def get_token(request):\n"
            "    \"\"\"Look up the session token for this request.\"\"\"\n"
            "    return request.headers.get('Authorization')\n"
        )
        out = redact.redact(text)
        self.assertEqual(out, text)


if __name__ == "__main__":
    unittest.main()
