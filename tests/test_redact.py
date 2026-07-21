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
