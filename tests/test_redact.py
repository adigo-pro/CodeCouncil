"""redact tests: high-confidence credential shapes, and a negative case that
proves ordinary code isn't mangled."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import redact


class TestRedact(unittest.TestCase):
    def test_aws_access_key(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
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
        secret = "github_pat_11EXAMPLE0000_FAKEtokenForTestsONLYnotReal0000"
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

    def test_assignment_incidental_substring_name_not_redacted(self):
        # The keyword must be a DELIMITED token in the name, not a substring
        # of a longer word: "tokenizer" (token+izer), "keywords" (key+words),
        # "monkey"/"turkey" (…+key) merely embed a keyword and are ordinary
        # identifiers. Redacting them (the old substring behavior) manufactured
        # false secret-in-code findings, since the critic is taught the marker
        # IS a confirmed secret. The 16+ char value guard is not enough on its
        # own -- ordinary long values (model names, slugs) trip it.
        value = "abcdefghijklmnopqrstuvwxyz012345"
        for name in ("tokenizer", "keywords", "monkey", "turkey_city", "keyword_count"):
            with self.subTest(name=name):
                text = f'{name} = "{value}"'
                self.assertEqual(redact.redact(text), text)

    def test_assignment_camelcase_name_redacted(self):
        # camelCase credential names (apiKey, myToken) are real and common;
        # the keyword there follows a lowercase letter, so it's matched by the
        # camelCase arm of the boundary rule rather than the delimiter arm.
        secret = "abcdefghijklmnopqrstuvwxyz012345"
        for name in ("apiKey", "myToken", "dbPassword"):
            with self.subTest(name=name):
                out = redact.redact(f'{name} = "{secret}"')
                self.assertNotIn(secret, out)
                self.assertIn("«REDACTED:assignment»", out)

    def test_pem_private_key_inside_diff_is_redacted(self):
        # In a `git diff` every line carries a +/-/space marker (the primary
        # capture path). The END line then reads `\n+-----END …`; the body
        # must still be redacted, or a committed key leaks unredacted into
        # observations/prompts/receipts.
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 3
        diff = (
            "+-----BEGIN PRIVATE KEY-----\n"
            f"+{body}\n"
            "+-----END PRIVATE KEY-----"
        )
        out = redact.redact(diff)
        self.assertNotIn(body, out)
        self.assertIn("«REDACTED:private-key»", out)

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

    def test_github_oauth_token(self):
        # gh[ousr]_ — GitHub's non-classic first-party token shapes
        # (oauth/user-to-server/server-to-server/refresh).
        secret = "gho_EXAMPLE0FAKEtokenFORtestsONLYnotReal"
        out = redact.redact(secret)
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:github-token»", out)

    def test_groq_key(self):
        secret = "gsk_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        out = redact.redact(f"GROQ_API_KEY={secret}")
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:groq-key»", out)

    def test_stripe_live_secret_key(self):
        secret = "sk_live_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        out = redact.redact(f"STRIPE_SECRET_KEY={secret}")
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:stripe-key»", out)

    def test_stripe_live_restricted_key(self):
        secret = "rk_live_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        out = redact.redact(secret)
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:stripe-key»", out)

    def test_google_api_key(self):
        secret = "AIza" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R"
        self.assertEqual(len(secret), 39)
        out = redact.redact(f'GOOGLE_API_KEY = "{secret}"')
        self.assertNotIn(secret, out)
        self.assertIn("«REDACTED:google-key»", out)

    def test_bare_jwt(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = redact.redact(f"Authorization: Bearer {jwt}")
        self.assertNotIn(jwt, out)
        self.assertIn("«REDACTED:jwt»", out)

    def test_url_with_password_redacted(self):
        secret = "hunter2pass"
        text = f"postgres://dbuser:{secret}@db.example.com:5432/prod"
        out = redact.redact(text)
        self.assertNotIn(secret, out)
        self.assertIn("postgres://dbuser:", out)
        self.assertIn("«REDACTED:url-credential»@db.example.com", out)

    def test_normal_url_without_credentials_untouched(self):
        text = "See https://example.com/docs/setup for details."
        out = redact.redact(text)
        self.assertEqual(out, text)

    def test_url_with_bare_username_no_password_untouched(self):
        # user@host with no ":password" segment must not be treated as a
        # credential URL.
        text = "git clone ssh://git@github.com/foo/bar.git"
        out = redact.redact(text)
        self.assertEqual(out, text)


if __name__ == "__main__":
    unittest.main()


class TestStripControls(unittest.TestCase):
    """Model-authored findings are printed to the developer's terminal and
    injected into the coding agent's context. A raw ANSI escape in that text
    can repaint the line above it — e.g. overwrite a HIGH severity label — so
    a finding could be made to look like something it isn't."""

    def test_csi_colour_sequences_removed(self):
        out = redact.strip_controls("issue \x1b[31mred\x1b[0m text")
        self.assertEqual(out, "issue red text")

    def test_cursor_movement_removed(self):
        """The dangerous ones: move-up + erase-line can rewrite prior output."""
        out = redact.strip_controls("safe\x1b[1A\x1b[2Kforged HIGH severity")
        self.assertNotIn("\x1b", out)
        self.assertEqual(out, "safeforged HIGH severity")

    def test_osc_sequence_removed(self):
        out = redact.strip_controls("a\x1b]0;window title\x07b")
        self.assertEqual(out, "ab")

    def test_c0_controls_removed_but_whitespace_kept(self):
        out = redact.strip_controls("a\x00b\x07c\td\ne\rf")
        self.assertEqual(out, "abc\td\ne\rf")

    def test_ordinary_text_untouched(self):
        text = "safe_divide(1, 0) raises ZeroDivisionError — see utils.py:42"
        self.assertEqual(redact.strip_controls(text), text)

    def test_empty_input(self):
        self.assertEqual(redact.strip_controls(""), "")


class TestSanitize(unittest.TestCase):
    def test_strips_and_redacts(self):
        out = redact.sanitize("key: \x1b[31mAKIAIOSFODNN7EXAMPLE\x1b[0m")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertNotIn("\x1b", out)

    def test_escape_split_secret_still_redacted(self):
        """Stripping runs BEFORE redaction, so an escape spliced into the
        middle of a credential can't break it past the patterns."""
        out = redact.sanitize("AKIA\x1b[0mIOSFODNN7EXAMPLE")
        self.assertNotIn("IOSFODNN7EXAMPLE", out)
        self.assertIn("REDACTED", out)
