/**
 * Read-only, path-jailed repo tools for the Critic's judgment turns (Task 4
 * follow-up). pi's *builtin* read/grep/find/ls resolve absolute and `~`
 * paths outside cwd — the `--tools` allowlist only restricts which tool
 * *names* are active, not which paths a tool may touch. Builtins therefore
 * cannot be safely offered to a judgment turn: cwd=<watched repo> plus
 * builtin `read` would let the model open ~/.codecouncil/env (cleartext
 * NVIDIA_API_KEY) or ~/.ssh/*.
 *
 * These four tools (repo_read, repo_grep, repo_find, repo_ls) are jailed to
 * the repo instead: every path argument is resolved and realpath-checked
 * against the jail root before any filesystem access (see ./jail.mjs), and
 * no path component may be .git or .codecouncil (the critic's own
 * workspace is invisible — the same discipline persona.md already states
 * for its staging directory). Names are deliberately distinct from the
 * builtins ("repo_*") so the critic/main.py JUDGE_TOOLS allowlist can never
 * accidentally select a builtin with the same short name.
 *
 * @sinclair/typebox is imported statically (top-level) here, not lazily
 * inside the factory — a lazy `await import("@sinclair/typebox")` deferred
 * into the extension factory was tried first and measured unreliable: it
 * fails fast with "Cannot find package '@sinclair/typebox'" during a real
 * `pi -p` turn (confirmed by direct testing), even though the identical
 * dynamic import silently appears to succeed under `pi --list-models`. A
 * static top-level import is what pi's own bundled extensions use and is
 * what was confirmed reliable end-to-end, including an actual tool call.
 * The jail logic itself lives in ./jail.mjs, which has zero non-builtin
 * imports and stays importable — and unit-testable — via plain `node`.
 */

import { readdirSync, readFileSync, realpathSync } from "node:fs";
import { relative } from "node:path";
import { Type } from "@sinclair/typebox";

import { FORBIDDEN_COMPONENTS, globToRegex, jailPath, walk } from "./jail.mjs";

const MAX_READ_LINES = 2000;
const MAX_GREP_MATCHES = 200;
const MAX_FIND_RESULTS = 500;
const MAX_LS_ENTRIES = 500;

function errorResult(text) {
  return { content: [{ type: "text", text: `Error: ${text}` }] };
}

export default function (pi) {
  pi.registerTool({
    name: "repo_read",
    label: "Repo Read",
    description: "Read a file from the repo (jailed to the repo root). Optional 1-based line range.",
    parameters: Type.Object({
      path: Type.String({ description: "Path relative to the repo root" }),
      start_line: Type.Optional(Type.Integer({ description: "1-based first line (inclusive)" })),
      end_line: Type.Optional(Type.Integer({ description: "1-based last line (inclusive)" })),
    }),
    async execute(_toolCallId, params) {
      const jail = jailPath(process.cwd(), params.path);
      if (!jail.ok) return errorResult(jail.reason);
      let text;
      try {
        text = readFileSync(jail.path, "utf-8");
      } catch (e) {
        return errorResult(`cannot read ${params.path}: ${e.message}`);
      }
      const lines = text.split("\n");
      const start = params.start_line && params.start_line > 1 ? Math.floor(params.start_line) : 1;
      const endRequested = params.end_line ? Math.floor(params.end_line) : lines.length;
      const end = Math.min(lines.length, Math.max(start, endRequested), start + MAX_READ_LINES - 1);
      const body = lines
        .slice(start - 1, end)
        .map((line, i) => `${start + i}\t${line}`)
        .join("\n");
      const note = end < Math.min(lines.length, endRequested) ? `\n… [truncated at ${MAX_READ_LINES} lines]` : "";
      return { content: [{ type: "text", text: (body || "(empty file)") + note }] };
    },
  });

  pi.registerTool({
    name: "repo_grep",
    label: "Repo Grep",
    description: "Search file contents in the repo for a regex pattern (jailed to the repo root).",
    parameters: Type.Object({
      pattern: Type.String({ description: "Regex pattern to search for" }),
      path: Type.Optional(Type.String({ description: "Directory to scope the search to (default: repo root)" })),
    }),
    async execute(_toolCallId, params) {
      const scope = params.path || ".";
      const jail = jailPath(process.cwd(), scope);
      if (!jail.ok) return errorResult(jail.reason);
      let regex;
      try {
        regex = new RegExp(params.pattern);
      } catch (e) {
        return errorResult(`bad pattern: ${e.message}`);
      }
      const root = realpathSync(process.cwd());
      const budget = { count: 0 };
      const matches = [];
      for (const file of walk(jail.path, budget)) {
        if (matches.length >= MAX_GREP_MATCHES) break;
        let text;
        try {
          text = readFileSync(file, "utf-8");
        } catch {
          continue; // binary or unreadable — skip
        }
        const relFile = relative(root, file);
        const lines = text.split("\n");
        for (let i = 0; i < lines.length && matches.length < MAX_GREP_MATCHES; i++) {
          if (regex.test(lines[i])) {
            matches.push(`${relFile}:${i + 1}: ${lines[i].slice(0, 300)}`);
          }
        }
      }
      const note = matches.length >= MAX_GREP_MATCHES ? `\n… [capped at ${MAX_GREP_MATCHES} matches]` : "";
      return { content: [{ type: "text", text: (matches.join("\n") || "no matches") + note }] };
    },
  });

  pi.registerTool({
    name: "repo_find",
    label: "Repo Find",
    description: "Find files in the repo whose path matches a glob pattern (jailed to the repo root).",
    parameters: Type.Object({
      pattern: Type.String({ description: "Glob pattern, e.g. '**/*.py' or 'tests/test_*.py'" }),
    }),
    async execute(_toolCallId, params) {
      const root = process.cwd();
      const jail = jailPath(root, ".");
      if (!jail.ok) return errorResult(jail.reason);
      let regex;
      try {
        regex = globToRegex(params.pattern);
      } catch (e) {
        return errorResult(`bad pattern: ${e.message}`);
      }
      const realRoot = realpathSync(root);
      const budget = { count: 0 };
      const hits = [];
      for (const file of walk(jail.path, budget)) {
        if (hits.length >= MAX_FIND_RESULTS) break;
        const relFile = relative(realRoot, file);
        if (regex.test(relFile)) hits.push(relFile);
      }
      const note = hits.length >= MAX_FIND_RESULTS ? `\n… [capped at ${MAX_FIND_RESULTS} results]` : "";
      return { content: [{ type: "text", text: (hits.join("\n") || "no matches") + note }] };
    },
  });

  pi.registerTool({
    name: "repo_ls",
    label: "Repo Ls",
    description: "List a directory's contents in the repo (jailed to the repo root).",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Directory relative to the repo root (default: root)" })),
    }),
    async execute(_toolCallId, params) {
      const target = params.path || ".";
      const jail = jailPath(process.cwd(), target);
      if (!jail.ok) return errorResult(jail.reason);
      let entries;
      try {
        entries = readdirSync(jail.path, { withFileTypes: true });
      } catch (e) {
        return errorResult(`cannot list ${target}: ${e.message}`);
      }
      const visible = entries.filter((e) => !FORBIDDEN_COMPONENTS.has(e.name));
      const lines = visible
        .slice(0, MAX_LS_ENTRIES)
        .map((e) => (e.isDirectory() ? `${e.name}/` : e.name))
        .sort();
      const note = visible.length > MAX_LS_ENTRIES ? `\n… [capped at ${MAX_LS_ENTRIES} entries]` : "";
      return { content: [{ type: "text", text: (lines.join("\n") || "(empty directory)") + note }] };
    },
  });
}
