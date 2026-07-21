/**
 * Path-jailing primitives for the Critic's judgment-turn repo tools
 * (critic/pi_extensions/repo_tools.mjs). Split into its own file, deliberately
 * free of any npm dependency (node builtins only), so `jailPath` stays
 * importable — and unit-testable — via plain `node`, independent of
 * whatever pi itself needs to load a tool-registering extension.
 *
 * (repo_tools.mjs needs a static top-level `import { Type } from
 * "@sinclair/typebox"` to describe its tool parameter schemas — a *dynamic*
 * `import()` deferred inside the extension factory was tried first and
 * measured unreliable: it fails fast with "Cannot find package
 * '@sinclair/typebox'" during a real `pi -p` turn even though the same
 * dynamic import silently appears to succeed under `pi --list-models`. A
 * static top-level import is what pi's own bundled extensions use and is
 * what was confirmed reliable end-to-end, including an actual tool call.
 * But a static top-level typebox import in the SAME file as `jailPath`
 * would make the whole module unimportable via bare `node` — bare node has
 * no access to pi's own node_modules, so any top-level import of a package
 * pi bundles fails module evaluation before any export becomes reachable.
 * Hence: this file has zero non-builtin imports, and repo_tools.mjs imports
 * from here instead of duplicating the logic.)
 */

import { readdirSync, realpathSync } from "node:fs";
import { isAbsolute, join, relative, sep } from "node:path";

export const FORBIDDEN_COMPONENTS = new Set([".git", ".codecouncil"]);

// Simple safety valve so a judgment turn can't accidentally burn its whole
// beat walking a huge tree (e.g. a repo with a large node_modules/ checked
// into working state). Not a security boundary — just a cost cap.
export const MAX_WALK_ENTRIES = 5000;

/**
 * Resolve `userPath` against `root`, refusing to leave the jail.
 *
 * Returns `{ ok: true, path: <realpath> }` on success or
 * `{ ok: false, reason: <string> }` on rejection. Never throws — callers
 * (tool `execute()` functions) turn a rejection into an error string for
 * the model, not an exception.
 */
export function jailPath(root, userPath) {
  if (typeof userPath !== "string" || userPath.length === 0) {
    return { ok: false, reason: "path is required" };
  }
  // Reject absolute paths and home-relative (~) paths BEFORE any joining —
  // path.join would otherwise happily concatenate them into a path that
  // still contains the substring "root", so the containment check below
  // must not be the only line of defense.
  if (isAbsolute(userPath) || userPath.startsWith("~")) {
    return { ok: false, reason: `path must be relative to the repo root, not absolute/home: ${userPath}` };
  }

  let realRoot;
  try {
    realRoot = realpathSync(root);
  } catch {
    return { ok: false, reason: "repo root not found" };
  }

  const joined = join(realRoot, userPath);
  let resolved;
  try {
    resolved = realpathSync(joined);
  } catch {
    return { ok: false, reason: `not found: ${userPath}` };
  }

  if (resolved !== realRoot && !resolved.startsWith(realRoot + sep)) {
    return { ok: false, reason: `outside the repo: ${userPath}` };
  }

  const rel = relative(realRoot, resolved);
  const parts = rel === "" ? [] : rel.split(sep);
  if (parts.some((p) => FORBIDDEN_COMPONENTS.has(p))) {
    return { ok: false, reason: `in the critic's own workspace, not a finding target: ${userPath}` };
  }

  return { ok: true, path: resolved };
}

/** Minimal glob → RegExp: `*` = any run of non-separator chars, `**` = any
 * run of chars (crosses directories), `?` = one non-separator char. */
export function globToRegex(glob) {
  let re = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*") {
      if (glob[i + 1] === "*") {
        re += ".*";
        i++;
      } else {
        re += "[^/]*";
      }
    } else if (c === "?") {
      re += "[^/]";
    } else if ("\\^$.|?*+()[]{}".includes(c)) {
      re += "\\" + c;
    } else if (c === "/") {
      re += sep === "\\" ? "\\\\" : "/";
    } else {
      re += c;
    }
  }
  return new RegExp("^" + re + "$");
}

/** Recursively yield realpaths of regular files under `dir` (which must
 * already be jail-checked), skipping .git/.codecouncil and symlinks (a
 * symlink could point outside the jail; simplest safe choice is to never
 * follow one during a tree walk). Bounded by MAX_WALK_ENTRIES. */
export function* walk(dir, budget) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (budget.count >= MAX_WALK_ENTRIES) return;
    budget.count++;
    if (entry.isSymbolicLink()) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (FORBIDDEN_COMPONENTS.has(entry.name)) continue;
      yield* walk(full, budget);
    } else if (entry.isFile()) {
      yield full;
    }
  }
}
