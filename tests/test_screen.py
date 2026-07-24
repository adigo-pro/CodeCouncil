"""Mechanical screening (critic/screen.py): the documented AI-code failure modes."""

import unittest
from pathlib import Path

from critic import prompt, screen


def diff(path: str, added: list[str], removed: list[str] | None = None) -> str:
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
             "@@ -1,5 +1,5 @@"]
    lines += [f"-{ln}" for ln in (removed or [])]
    lines += [f"+{ln}" for ln in added]
    return "\n".join(lines) + "\n"


class TestSecurityPatterns(unittest.TestCase):
    def test_fstring_sql_flagged(self):
        d = diff("app.py", ['cursor.execute(f"SELECT * FROM users WHERE id={uid}")'])
        kinds = [s["kind"] for s in screen.scan_patterns(d)]
        self.assertEqual(kinds, ["sql-injection"])

    def test_parameterized_sql_not_flagged(self):
        d = diff("app.py", ['cursor.execute("SELECT * FROM users WHERE id=%s", (uid,))'])
        self.assertEqual(screen.scan_patterns(d), [])

    def test_shell_true_and_os_system_flagged(self):
        d = diff("run.py", ["subprocess.run(cmd, shell=True)", "os.system(user_cmd)"])
        kinds = [s["kind"] for s in screen.scan_patterns(d)]
        self.assertEqual(kinds, ["command-injection", "command-injection"])

    def test_unsafe_yaml_and_pickle_flagged_safe_loader_not(self):
        d = diff("cfg.py", ["data = yaml.load(text)",
                            "obj = pickle.loads(blob)",
                            "ok = yaml.load(text, Loader=yaml.SafeLoader)"])
        kinds = [s["kind"] for s in screen.scan_patterns(d)]
        self.assertEqual(kinds, ["unsafe-deserialization", "unsafe-deserialization"])

    def test_eval_on_variable_flagged_literal_eval_not(self):
        d = diff("calc.py", ["result = eval(expr)",
                             "safe = ast.literal_eval('[1]')"])
        kinds = [s["kind"] for s in screen.scan_patterns(d)]
        self.assertEqual(kinds, ["eval-injection"])

    def test_one_line_can_carry_two_classes(self):
        # council catch: a break after the first match hid the second class
        d = diff("run.py", ["os.system(eval(user_input))"])
        kinds = sorted(s["kind"] for s in screen.scan_patterns(d))
        self.assertEqual(kinds, ["command-injection", "eval-injection"])

    def test_comment_lines_ignored(self):
        d = diff("app.py", ["# cursor.execute(f\"SELECT {x}\") -- old approach"])
        self.assertEqual(screen.scan_patterns(d), [])


class TestTestWeakening(unittest.TestCase):
    def test_removed_test_function_flagged(self):
        d = diff("tests/test_app.py", ["pass"], removed=["def test_edge_case(self):"])
        kinds = [s["kind"] for s in screen.scan_test_weakening(d)]
        self.assertEqual(kinds, ["test-removed"])

    def test_assertions_removed_without_replacement_flagged(self):
        d = diff("tests/test_app.py", ["x = compute()"],
                 removed=["assert x == 42", "assert y == 7"])
        kinds = [s["kind"] for s in screen.scan_test_weakening(d)]
        self.assertEqual(kinds, ["assertions-weakened"])

    def test_assertion_refactor_not_flagged(self):
        d = diff("tests/test_app.py",
                 ["assert result == expected", "assert other == 7"],
                 removed=["assert x == 42", "assert y == 7"])
        self.assertEqual(screen.scan_test_weakening(d), [])

    def test_non_test_files_ignored(self):
        d = diff("app.py", [], removed=["assert invariant, 'must hold'"])
        self.assertEqual(screen.scan_test_weakening(d), [])


class TestTestIntegrity(unittest.TestCase):
    """Task 2: session-level verdict — strengthened/unchanged/weakened —
    feeding the receipt and the done-gate."""

    def test_empty_diff_unchanged_zeros(self):
        self.assertEqual(screen.test_integrity(""), {
            "verdict": "unchanged", "tests_added": 0, "tests_removed": 0,
            "asserts_added": 0, "asserts_removed": 0,
        })

    def test_no_test_files_touched_unchanged_zeros(self):
        d = diff("app.py", ["assert invariant, 'must hold'"], removed=["x = 1"])
        self.assertEqual(screen.test_integrity(d), {
            "verdict": "unchanged", "tests_added": 0, "tests_removed": 0,
            "asserts_added": 0, "asserts_removed": 0,
        })

    def test_strengthened_asserts_added_none_removed(self):
        d = diff("tests/test_app.py", ["assert x == 1", "assert y == 2"])
        result = screen.test_integrity(d)
        self.assertEqual(result["verdict"], "strengthened")
        self.assertEqual(result["asserts_added"], 2)
        self.assertEqual(result["asserts_removed"], 0)

    def test_strengthened_new_test_function(self):
        d = diff("tests/test_app.py", ["def test_new(self):", "    pass"])
        result = screen.test_integrity(d)
        self.assertEqual(result["verdict"], "strengthened")
        self.assertEqual(result["tests_added"], 1)

    def test_unchanged_refactor_equal_assert_counts(self):
        d = diff("tests/test_app.py",
                 ["assert result == expected", "assert other == 7"],
                 removed=["assert x == 42", "assert y == 7"])
        result = screen.test_integrity(d)
        self.assertEqual(result["verdict"], "unchanged")
        self.assertEqual(result["asserts_added"], 2)
        self.assertEqual(result["asserts_removed"], 2)

    def test_weakened_test_function_removed(self):
        d = diff("tests/test_app.py", ["pass"], removed=["def test_edge_case(self):"])
        result = screen.test_integrity(d)
        self.assertEqual(result["verdict"], "weakened")
        self.assertEqual(result["tests_removed"], 1)

    def test_weakened_test_removed_even_if_another_added(self):
        # a removed test always counts as weakened, regardless of additions
        # elsewhere — matches scan_test_weakening's "test-removed" signal
        d = diff("tests/test_app.py", ["def test_new(self):", "    pass"],
                 removed=["def test_old(self):"])
        result = screen.test_integrity(d)
        self.assertEqual(result["verdict"], "weakened")
        self.assertEqual(result["tests_added"], 1)
        self.assertEqual(result["tests_removed"], 1)

    def test_weakened_net_negative_assertions(self):
        d = diff("tests/test_app.py", ["x = compute()"],
                 removed=["assert x == 42", "assert y == 7"])
        result = screen.test_integrity(d)
        self.assertEqual(result["verdict"], "weakened")
        self.assertEqual(result["asserts_removed"], 2)
        self.assertEqual(result["asserts_added"], 0)


class TestHallucinatedImports(unittest.TestCase):
    def test_new_import_names_parsed_from_added_python_only(self):
        d = diff("app.py", ["import requests_wrong", "from flask_fake import x"]) \
            + diff("notes.md", ["import not_code"])
        self.assertEqual(screen.new_import_names(d),
                         {"requests_wrong": "app.py", "flask_fake": "app.py"})

    def test_unresolvable_import_flagged_stdlib_not(self):
        d = diff("app.py", ["import json", "import definitely_not_a_real_pkg_xyz"])
        signals = screen.resolve_new_imports(screen.new_import_names(d), Path.cwd())
        self.assertEqual([s["kind"] for s in signals], ["unresolvable-import"])
        self.assertIn("definitely_not_a_real_pkg_xyz", signals[0]["evidence"])


class TestScreenAndPrompt(unittest.TestCase):
    def test_no_repo_skips_import_resolution(self):
        d = diff("app.py", ["import definitely_not_a_real_pkg_xyz"])
        self.assertEqual(screen.screen(d, repo=None), [])

    def test_signal_cap(self):
        d = diff("app.py", ["os.system(c%d)" % i for i in range(20)])
        self.assertEqual(len(screen.screen(d)), screen.MAX_SIGNALS)

    def test_prompt_without_signals_is_byte_identical(self):
        base = prompt.build_prompt([], None, "version: 1\n- rule")
        with_none = prompt.build_prompt([], None, "version: 1\n- rule", signals=None)
        self.assertEqual(base, with_none)

    def test_prompt_renders_signals_section(self):
        sig = [{"kind": "sql-injection", "cwe": "CWE-89", "file": "app.py",
                "line": 3, "evidence": "cursor.execute(f\"...\")"}]
        text = prompt.build_prompt([], None, "version: 1\n- rule", signals=sig)
        self.assertIn("MECHANICAL SCREENING SIGNALS", text)
        self.assertIn("[sql-injection · CWE-89] app.py:3", text)


if __name__ == "__main__":
    unittest.main()
