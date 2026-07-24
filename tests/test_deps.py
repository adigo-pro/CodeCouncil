"""Dependency provenance (critic/deps.py): typo-suspect imports and
new-dependency-line extraction for the receipt's supply-chain section."""

import unittest
from pathlib import Path
from unittest import mock

from critic import deps, screen
from tests.test_screen import diff


class TestWithinOneEdit(unittest.TestCase):
    def test_equal_strings(self):
        self.assertTrue(deps._within_one_edit("requests", "requests"))

    def test_single_substitution(self):
        self.assertTrue(deps._within_one_edit("numpy", "numpx"))

    def test_single_deletion(self):
        self.assertTrue(deps._within_one_edit("requsts", "requests"))

    def test_single_insertion(self):
        self.assertTrue(deps._within_one_edit("numpyy", "numpy"))

    def test_adjacent_transposition(self):
        self.assertTrue(deps._within_one_edit("nupmy", "numpy"))

    def test_two_substitutions_not_within_one(self):
        self.assertFalse(deps._within_one_edit("nnnpy", "numpy"))

    def test_length_delta_two_not_within_one(self):
        self.assertFalse(deps._within_one_edit("np", "numpy"))

    def test_unrelated_words_not_within_one(self):
        self.assertFalse(deps._within_one_edit("kubernetes", "requests"))


class TestSuspiciousImports(unittest.TestCase):
    def test_requsts_flags_naming_requests(self):
        signals = deps.suspicious_imports({"requsts": "app.py"})
        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s["kind"], "typo-suspect-import")
        self.assertEqual(s["cwe"], "slopsquatting")
        self.assertEqual(s["file"], "app.py")
        self.assertEqual(s["line"], 0)
        self.assertIn("import requsts", s["evidence"])
        self.assertIn("'requests'", s["evidence"])

    def test_requests_itself_never_flags(self):
        self.assertEqual(deps.suspicious_imports({"requests": "app.py"}), [])

    def test_numpyy_flags_naming_numpy(self):
        signals = deps.suspicious_imports({"numpyy": "app.py"})
        self.assertEqual(len(signals), 1)
        self.assertIn("'numpy'", signals[0]["evidence"])

    def test_stdlib_name_never_flags(self):
        self.assertEqual(deps.suspicious_imports({"jsonn": "app.py"}), [])

    def test_far_unknown_name_does_not_flag(self):
        # "definitely_not_a_real_pkg_xyz" is nowhere near any known package —
        # suspicious_imports must not claim it; it falls through to the
        # separate unresolvable-import check instead.
        self.assertEqual(
            deps.suspicious_imports({"definitely_not_a_real_pkg_xyz": "app.py"}), [])

    def test_multiple_names_each_evaluated_independently(self):
        signals = deps.suspicious_imports({"requsts": "a.py", "flask": "b.py"})
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["file"], "a.py")


class TestIsTypoSuspect(unittest.TestCase):
    def test_known_name_not_suspect(self):
        self.assertFalse(deps.is_typo_suspect("requests"))

    def test_typo_name_is_suspect(self):
        self.assertTrue(deps.is_typo_suspect("requsts"))

    def test_far_name_not_suspect(self):
        self.assertFalse(deps.is_typo_suspect("definitely_not_a_real_pkg_xyz"))


class TestNewDependencyLines(unittest.TestCase):
    def test_requirements_txt_lines_extracted(self):
        d = diff("requirements.txt", ["requests==2.31.0", "# a comment", "", "flask>=2.0"])
        self.assertEqual(deps.new_dependency_lines(d),
                         ["requests==2.31.0", "flask>=2.0"])

    def test_requirements_dev_txt_matches_prefix(self):
        d = diff("requirements-dev.txt", ["pytest==8.0.0"])
        self.assertEqual(deps.new_dependency_lines(d), ["pytest==8.0.0"])

    def test_pyproject_dependency_entries_extracted(self):
        d = diff("pyproject.toml", ['"requests>=2.0",', "[tool.other]", '"flask",'])
        self.assertEqual(deps.new_dependency_lines(d), ['"requests>=2.0",', '"flask",'])

    def test_package_json_dependency_entries_extracted(self):
        d = diff("package.json", ['"lodash": "^4.17.21",', "{", '"left-pad": "1.3.0"'])
        self.assertEqual(deps.new_dependency_lines(d),
                         ['"lodash": "^4.17.21",', '"left-pad": "1.3.0"'])

    def test_unrelated_files_ignored(self):
        d = diff("app.py", ["import requests"])
        self.assertEqual(deps.new_dependency_lines(d), [])

    def test_empty_diff(self):
        self.assertEqual(deps.new_dependency_lines(""), [])


class TestScreenOrdering(unittest.TestCase):
    """Typo-suspect runs before, and instead of, unresolvable-import for the
    same name — one signal per name (brief's ordering requirement)."""

    def test_typo_suspect_excludes_name_from_unresolvable_check(self):
        d = diff("app.py", ["import requsts"])
        with mock.patch.object(screen, "resolve_new_imports") as m:
            m.return_value = []
            signals = screen.screen(d, repo=Path("/tmp"))
        # resolve_new_imports must have been called with requsts excluded
        called_names = m.call_args[0][0]
        self.assertNotIn("requsts", called_names)
        kinds = [s["kind"] for s in signals]
        self.assertIn("typo-suspect-import", kinds)
        self.assertNotIn("unresolvable-import", kinds)

    def test_unknown_far_name_falls_through_to_unresolvable_with_repo(self):
        d = diff("app.py", ["import definitely_not_a_real_pkg_xyz"])
        signals = screen.screen(d, repo=Path.cwd())
        kinds = [s["kind"] for s in signals]
        self.assertNotIn("typo-suspect-import", kinds)
        self.assertIn("unresolvable-import", kinds)

    def test_unknown_far_name_no_repo_produces_no_signals(self):
        d = diff("app.py", ["import definitely_not_a_real_pkg_xyz"])
        self.assertEqual(screen.screen(d, repo=None), [])

    def test_typo_suspect_name_flagged_even_without_repo(self):
        # pure check, no subprocess needed -- should not require a repo
        d = diff("app.py", ["import requsts"])
        signals = screen.screen(d, repo=None)
        kinds = [s["kind"] for s in signals]
        self.assertIn("typo-suspect-import", kinds)


if __name__ == "__main__":
    unittest.main()
