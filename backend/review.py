#!/usr/bin/env python3
"""GrapeRoot Review — AI PR reviewer using the dual-graph context engine.

Usage:
    python3 review.py <github-pr-url>          # review and post comments
    python3 review.py <github-pr-url> --dry    # review, print only (no post)
    python3 review.py <github-pr-url> --vs     # side-by-side vs plain Claude

Requires:
    GITHUB_TOKEN  — GitHub personal access token (repo + pull_request scope)
    ANTHROPIC_API_KEY — for Claude

The tool:
    1. Fetches the PR diff and linked issue/description from GitHub
    2. Runs graph_impact on changed files to get blast radius
    3. Reads changed symbols at the symbol level (not full files)
    4. Calls Claude with diff + graph context + linked issue
    5. Posts inline comments ranked: CRITICAL → HIGH → MEDIUM (style suppressed)
    6. Saves review decision to context store for memory across PRs
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_KEY       = os.environ.get("OPENAI_API_KEY", "")
MCP_PORT         = int(os.environ.get("GRAPEROOT_PORT", os.environ.get("DG_MCP_PORT", "8765")))
GR_GRAPH_CONTEXT = os.environ.get("GR_GRAPH_CONTEXT", "")  # injected by webhook server
MAX_DIFF_CHARS   = 24_000
MAX_COMMENTS     = 25

USE_OPENAI = bool(OPENAI_KEY and not ANTHROPIC_KEY)
MODEL          = os.environ.get("OPENAI_MODEL", "gpt-4o") if USE_OPENAI else os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh(path: str, method: str = "GET", body: dict | None = None) -> Any:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"GitHub API error {e.code}: {e.read()[:200].decode()}", file=sys.stderr)
        raise


def gh_file(owner: str, repo: str, path: str, ref: str) -> str:
    """Fetch decoded file content from GitHub at a specific ref. Returns '' on error."""
    import base64
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [ctx] could not fetch {path}@{ref[:7]}: {e}", file=sys.stderr)
    return ""


def gh_search_imports(owner: str, repo: str, filename: str) -> list[str]:
    """Find files in the repo that import from filename (best-effort via GitHub search)."""
    stem = filename.rsplit("/", 1)[-1].replace(".py", "")
    req = urllib.request.Request(
        f"https://api.github.com/search/code?q={urllib.parse.quote(stem)}+repo:{owner}/{repo}&per_page=10",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            items = json.loads(r.read()).get("items", [])
        return [i["path"] for i in items if i["path"] != filename][:5]
    except Exception:
        return []


def build_file_context(owner: str, repo: str, changed_files: list[str],
                       base_sha: str, head_sha: str, max_chars: int = 40000) -> str:
    """
    Fetch full content of changed files at base + head, plus files that import them.
    Returns a formatted string to inject into the review prompt as codebase context.
    This is the no-clone alternative to the AST graph — gives the model full visibility
    into what existed before the PR and what adjacent files look like.
    """
    # High-signal filename patterns — fetch these first regardless of diff order.
    # Only truly general patterns (code that tends to contain bugs), not domain-specific.
    _PRIORITY = re.compile(
        r'(math|calc|util|helper|valid|auth|crypt|sign|hash|schema|model'
        r'|handler|middleware|guard|policy|permission|sanitiz|escape'
        r'|response|request|error|exception|retry|timeout|parse|serial|deserial'
        r'|encode|decode|transform|convert|format|process)',
        re.I
    )
    priority = [f for f in changed_files if _PRIORITY.search(f.rsplit('/', 1)[-1])]
    others   = [f for f in changed_files if f not in priority]
    ordered  = priority + others  # high-signal files first

    parts: list[str] = []
    budget = max_chars

    for path in ordered[:14]:          # up from 6 → 14
        if budget <= 200:
            break

        per_file_base = min(8000, budget // max(1, len(ordered[:14]) - ordered[:14].index(path)))

        # Base (before PR) — FULL content, critical for spotting omissions
        base = gh_file(owner, repo, path, base_sha)
        if base:
            snippet = base[:per_file_base]
            parts.append(f"### {path}  [BASE — before this PR]\n```\n{snippet}\n{'...(truncated)' if len(base) > len(snippet) else ''}\n```")
            budget -= len(snippet)

        # Head (after PR) for new files or heavily modified ones
        head = gh_file(owner, repo, path, head_sha)
        if head and head != base:
            snippet = head[:min(5000, budget)]
            parts.append(f"### {path}  [HEAD — after this PR]\n```\n{snippet}\n{'...(truncated)' if len(head) > len(snippet) else ''}\n```")
            budget -= len(snippet)

    # Find and include a few files that import from the changed files
    already = set(changed_files)
    for path in changed_files[:3]:
        if budget <= 0:
            break
        for importer in gh_search_imports(owner, repo, path):
            if importer in already or budget <= 0:
                continue
            content = gh_file(owner, repo, importer, head_sha)
            if content:
                snippet = content[:min(1500, budget)]
                parts.append(f"### {importer}  [imports from changed file]\n```\n{snippet}\n```")
                budget -= len(snippet)
                already.add(importer)

    return "\n\n".join(parts)


def gh_diff(owner: str, repo: str, pr_num: int) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.diff",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Extract owner, repo, PR number from a GitHub PR URL."""
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"Not a valid GitHub PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


# ── GrapeRoot graph helpers ────────────────────────────────────────────────────

def _mcp(tool: str, **kwargs: Any) -> dict:
    """Call a GrapeRoot MCP tool via HTTP."""
    payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
               "params": {"name": tool, "arguments": kwargs}}
    data    = json.dumps(payload).encode()
    req     = urllib.request.Request(
        f"http://127.0.0.1:{MCP_PORT}/mcp",
        data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        content = resp.get("result", {}).get("content", [])
        if content:
            return json.loads(content[0].get("text", "{}"))
        return {}
    except Exception as e:
        print(f"  [graph] {tool} failed: {e}", file=sys.stderr)
        return {}


def graph_impact(files: list[str]) -> dict:
    return _mcp("graph_impact", changed_files=files)


def graph_read_symbol(ref: str) -> str:
    result = _mcp("graph_read", file=ref, max_chars=3000, query="review")
    if isinstance(result, dict):
        return result.get("content", result.get("text", "")) or str(result)[:2000]
    return str(result)[:2000]


def mcp_available() -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{MCP_PORT}/prime", headers={"User-Agent": "graperoot-review"},
        )
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


# ── Parse diff → changed files + per-file hunks ───────────────────────────────

def parse_diff(diff: str) -> tuple[list[str], dict[str, str]]:
    """Returns (changed_file_paths, {path: hunk_text})."""
    files: list[str]        = []
    hunks: dict[str, str]   = {}
    current: str | None     = None
    current_lines: list[str]= []

    for line in diff.splitlines():
        if line.startswith("diff --git"):
            if current:
                hunks[current] = "\n".join(current_lines[-80:])  # last 80 lines
            # `diff --git a/<old> b/<new>` — always take the rightmost b/ segment.
            # For renamed/moved files the line has two paths; taking the last
            # b/<path> correctly gives the new (HEAD) file path.
            m = re.search(r" b/(.+)$", line)
            if m:
                current = m.group(1)
                files.append(current)
                current_lines = [line]
        elif current:
            current_lines.append(line)

    if current and current_lines:
        hunks[current] = "\n".join(current_lines[-80:])

    return files, hunks


# ── Master prompt (14-category structured output) ─────────────────────────────

SYSTEM_PROMPT = """You are an expert code reviewer with deep knowledge of security, distributed
systems, and production reliability. Analyse this PR and produce a structured JSON report.

ANTI-HALLUCINATION RULES — violating these is worse than finding nothing:
- NEVER invent code that is not in the provided context. Quote exact lines verbatim.
- Before reporting a bug, find the exact line in the diff or file content. If absent, skip it.
- Do not assume code exists outside what is shown.

Return ONLY valid JSON with this exact shape:

{
  "pr_summary": "2-3 sentence summary: what it does, production risk level, key concern",

  "inline_comments": [
    {
      "file": "path/to/file",
      "line": 42,
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "category": "logic|security|performance|reliability|test|contract|refactor",
      "title": "one-line summary",
      "comment": "detailed explanation with WHY this is a problem. Quote the EXACT line(s) from context.",
      "suggestion": "```suggestion\\n// corrected code\\n```",
      "graph_proven": true
    }
  ],

  "sast_findings": [
    {
      "severity": "CRITICAL|HIGH|MEDIUM",
      "rule": "rule-id or CWE or OWASP category",
      "file": "path", "line": 0,
      "detail": "specific explanation with exploit scenario"
    }
  ],

  "iac_findings": [
    {
      "severity": "HIGH",
      "file": "path to .tf / .yaml / Dockerfile",
      "rule": "e.g. missing resource limits, hardcoded secret",
      "detail": "specific risk and fix"
    }
  ],

  "secrets_found": [
    {"file": "path", "line": 0, "type": "API key / token", "value_preview": "sk-a..."}
  ],

  "dead_code": ["exported symbols with no importers — only if graph context confirms"],

  "duplicate_code": [
    {"files": ["file1", "file2"], "pattern": "description of duplicated logic"}
  ],

  "complex_functions": [
    {"file": "path", "function": "name", "cyclomatic_complexity": "high|very_high", "reason": "why"}
  ],

  "test_coverage_gaps": [
    {"file": "path", "function_or_class": "name", "risk": "why missing test is dangerous here"}
  ],

  "blast_radius": {
    "direct_callers": ["files that import the changed files — from graph context"],
    "cross_repo_risk": "description if this change breaks other repos or configs",
    "risk": "CRITICAL|HIGH|MEDIUM|LOW",
    "explanation": "specific production scenario if this PR ships with bugs"
  },

  "attack_surface_delta": {
    "increased": ["new endpoints, new data access paths, new auth flows added"],
    "decreased": ["removed attack surface"],
    "net_risk": "INCREASED|DECREASED|NEUTRAL"
  },

  "jira_intent_check": {
    "matches_description": true,
    "gaps": ["things the PR description promises but the diff does not implement"],
    "missing_files": ["files that MUST change for this PR to be complete but are absent"]
  },

  "missing_from_diff": [
    "files that should have been changed but were not, based on what the PR claims to do"
  ],

  "quality_gates": {
    "pass": false,
    "blocking_issues": ["CRITICAL/HIGH issues that must be fixed before merge"]
  },

  "security_grade": "A|B|C|D|F",
  "quality_score": 0,
  "review_confidence": "HIGH|MEDIUM|LOW"
}

Rules:
- inline_comments: no hard cap, sorted by severity (CRITICAL first), include suggestion when possible
- graph_proven=true only when blast_radius context was used to derive the finding
- iac_findings: check .tf, .yaml (k8s/helm), Dockerfile, .github/workflows in diff
- missing_from_diff: THE MOST IMPORTANT CHECK — what SHOULD be in diff but isn't
- For refactoring PRs: inventory every class/method in BASE, check if each is in HEAD or new module.
  For methods left behind: read the BODY (not just signature) — does it actually use framework types?
  Flag orphaned methods whose body only calls self.X() methods that were already moved.
- quality_score: 0–100 (100=perfect, 0=do not merge)
- Return ONLY the JSON, no markdown wrapper, no explanation outside JSON"""


# ── Specialized agent prompts ─────────────────────────────────────────────────

# ── Check pipeline ────────────────────────────────────────────────────────────
# Each check has ONE job. All receive the same assembled context (graph + diff +
# file content). Each returns a JSON array of findings (notes) or [].
# The aggregator merges all notes into the final structured report.
#
# Adding a new check = add one entry to CHECKS list below. No other changes needed.

def _check_prompt(tag: str, focus: str, rules: str) -> str:
    return f"""You are doing ONE specific code review check: {focus}

{rules}

IMPORTANT:
- Find ALL instances of the pattern — scan the entire diff and file content.
- Do not stop after the first finding. Report every distinct issue you see.
- Quote the exact problematic line(s) from the provided code in your comment.
- If "How this framework is correctly used in this codebase" section is present,
  USE IT to understand what correct code looks like, then compare against the diff.
  A discrepancy between the existing codebase pattern and the new code is a finding.
- Do not invent code that is not in the context — if the pattern is absent, return [].
- If you DO find the pattern, report it. Do not suppress real findings.

Return a JSON ARRAY of findings:
[{{
  "file": "path/to/file",
  "line": 0,
  "severity": "CRITICAL|HIGH|MEDIUM|LOW",
  "title": "[LLM-HEURISTIC: {tag}] one-line summary",
  "comment": "quote the exact line, then explain the problem and fix",
  "suggestion": "corrected code if applicable"
}}]

Return [] ONLY if you have genuinely searched and found nothing.
Return ONLY the JSON array. No prose outside the array."""


CHECKS = [
    ("arithmetic", _check_prompt("arithmetic",
        "arithmetic safety — division by zero, BigInt overflow, NaN propagation",
        """For every division, modulo, and numeric conversion:
- Division: read the 1-10 lines BEFORE the division. If there is an if(x===0){break/return/throw}
  guard on the same code path, the division is protected — DO NOT REPORT IT.
- BigInt←→float: Number(bigint) can be Infinity if bigint is large.
  BigInt(Math.round(Infinity)) throws RangeError. Report only if no guard exists.
- NaN propagation: where NaN silently replaces a numeric value.
DO NOT report guarded divisions. A guard 2 lines above is protection, not a bug.""")),

    ("mutation", _check_prompt("mutation",
        "mutation traps — Object.assign / spread / dict.update overwriting already-set keys",
        """For every Object.assign(target, source), {...target, ...source}, dict.update(extra):
- List keys already written to target (error, message, details, etc.)
- Check if source (user input, options bag, extra param) can contain those same keys
- If yes: source silently overwrites target — report as HIGH
- Include the exact line and which key(s) can be overwritten.""")),

    ("n_plus_one", _check_prompt("n-plus-one",
        "N+1 query pattern — ORM/DB calls inside loops",
        """For every loop (for, while, forEach, map) in the diff:
- Does the loop body call an ORM/DB query? (Model.objects.filter/get/exists/create,
  session.query, findMany, db.execute, executeQuery, SELECT inside loop)
- If yes: THIS IS N+1. Report as HIGH with the exact loop line AND inner query line.
  It does not matter if it's for validation, checking, or processing — any ORM call
  inside a for-loop is N+1 by definition. The fix is always to fetch the full set
  before the loop: filter(id__in=ids).exists() or filter(uuid__in=uuids).
Example of N+1 to catch:
  for uuid in submission_uuids:
      if Model.objects.filter(uuid=uuid).exists():  ← N+1: 1 query per iteration
Also check: custom list()/retrieve() that calls self.get_queryset() directly without
self.filter_queryset() wrapper — this silently bypasses all filter backends. HIGH.""")),

    ("falsy_traps", _check_prompt("falsy-trap",
        "boolean / falsy evaluation traps on collections",
        """Look for these patterns:
- `x or fallback` where x is a list, queryset, or paginator result:
  empty list [] is falsy in Python/JS/Ruby, so [] or fallback returns fallback
  even when the correct answer IS the empty result.
  Classic: `page = paginator.paginate_queryset(...); data = page or queryset`
  → if page=[], data becomes full queryset instead of empty page. Report CRITICAL.
- `if not collection` that should distinguish None (absent) from [] (empty).
- Any truthy check on a queryset object (QuerySets are always truthy even when empty).""")),

    ("arch", _check_prompt("arch-check",
        "refactoring completeness — missing moves, orphaned methods, behavioral defaults",
        """If 'existing sansio/shared module' examples are provided, USE THEM FIRST:
  Those show what the target module SHOULD contain. Compare against the diff to find
  methods present in changed files that are ABSENT from the sansio module examples
  despite having no framework-specific logic in their bodies.

Check 1 — REFACTOR INVENTORY:
List every class and public method in the BASE version of changed files.
For each method still in the original module (not moved to sansio/shared):
  a. Read its COMPLETE body (not just signature).
  b. Does the body reference framework types (Flask, Request, Response)?
     OR does it ONLY call self.X() where X is already in the sansio module?
  c. If ONLY calls already-moved helpers → ORPHAN → report HIGH.
     Quote the body: "body is `return self.null_session_class()` — calls only moved helpers"

Check 2 — SIBLING ASYMMETRY:
Methods A, B, C in same class. A moved to sansio, C did not.
If C's body has no framework dependency → report HIGH.

Check 3 — DEFAULT VALUE CHANGES:
Compare class-level attribute defaults: BASE `accessed = False`, HEAD `accessed = True`.
Report MEDIUM with exact old→new values. Do NOT report if the value is the same.""")),

    ("security", _check_prompt("security-check",
        "security vulnerabilities — auth bypass, unhandled errors, access control, exposed secrets",
        """Look for these patterns:
- Auth bypass: code that grants access based on a condition that can be bypassed.
  Check: does unauthenticated state lead to a 404/error instead of a login redirect?
  Check: does a default role or fallback value grant elevated privilege (e.g. default='admin')?
- Missing error handling: async/await call where the error return value is never checked.
  Pattern: `const result = await api.doSomething()` with no `if (result.error)` check.
- Privilege escalation: action that should be blocked for the actor's own account
  (e.g. admin can delete/demote themselves — missing `currentUserId !== targetId` guard).
- Hard-coded limits silently truncating: `limit: 500` with no pagination cursor → data loss.
- Exposed secrets or insecure defaults in the diff.""")),

    ("api_compat", _check_prompt("api-compat",
        "API/response shape changes that break existing callers",
        """Compare the BASE version of each changed file with its HEAD version.
Look for:
1. FIELD MOVED: a field that was at the top level of a response/object is now nested inside another field.
   Pattern: BASE has `{ error: "...", fieldName: value }`, HEAD has `{ error: "...", details: { fieldName: value } }`.
   This breaks any caller that reads `response.fieldName` directly.
2. FIELD RENAMED: a key was renamed without a compatibility alias.
3. FIELD REMOVED: a key present in BASE is absent in HEAD.
4. TYPE CHANGED: a field changed from string to array, from optional to required, etc.

For each finding, cite the BASE line showing the old shape AND the HEAD line showing the new shape.
Report as HIGH if existing callers will break silently (no error, wrong data).
Report as MEDIUM if callers will get an explicit error (missing field, type mismatch).""")),

    ("quality", _check_prompt("quality-check",
        "algorithmic complexity, test gaps, dead code, documentation",
        """Check 1 — ALGORITHMIC COMPLEXITY:
For every loop, check if the loop body calls a helper that iterates over a collection.
Pattern: for x in list → helper(x) where helper does list.find/filter/index/loop → O(n²).
Report as HIGH with both the outer loop line and the inner scan line.

Check 2 — TEST GAPS:
Risky code paths (error handling, edge cases, new business logic) with no visible test.

Check 3 — DEAD CODE:
Exported symbols with no importers according to the graph context.

Check 4 — DOCUMENTATION:
Docstring typos, missing spaces, run-on words. Cite the exact malformed text.""")),
]


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text)
    try:
        r = json.loads(text)
        return r if isinstance(r, dict) else {"inline_comments": r}
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


_CODE_RE = re.compile(r'[=\(\)\[\]{}<>:;,\.\+\-\*\/]|def |class |return |import |self\.')

def _snippet_in_context(snippet: str, context: str) -> bool:
    """Check snippet exists in context with word-boundary awareness.
    Prevents 'modified = T' from matching 'modified = True' as a substring."""
    if snippet not in context:
        return False
    # Find all occurrences and ensure none is followed immediately by an alphanumeric char
    # that would extend it to a longer token (e.g. 'T' followed by 'rue' → True)
    last_char = snippet[-1] if snippet else ""
    if last_char.isalnum() or last_char == "_":
        # Snippet ends with an identifier char — require word boundary after it
        pattern = re.escape(snippet) + r"(?![a-zA-Z0-9_])"
        return bool(re.search(pattern, context))
    return True


def _verify_findings(findings: list[dict], context: str) -> list[dict]:
    """Filter hallucinations: only check snippets that look like actual code, not prose."""
    verified = []
    for f in findings:
        comment = f.get("comment", f.get("body", ""))
        all_snippets = re.findall(r"`([^`\n]{4,120})`", comment)
        code_snippets = [s for s in all_snippets if _CODE_RE.search(s)]
        ok = True
        for snippet in code_snippets[:3]:
            if not _snippet_in_context(snippet, context):
                ok = False
                print(f"  [verify] filtered: '{snippet[:50]}' not in context")
                break
        if ok:
            verified.append(f)
    return verified


def claude_review(
    pr_title: str,
    pr_body: str,
    linked_issue: str,
    diff_text: str,
    impact_summary: str,
    symbol_excerpts: str,
    file_context: str = "",
) -> dict:
    """
    Pipeline: assemble context ONCE from graph, pass it through each focused check,
    collect notes from all checks, aggregate into one structured report.
    """
    import threading

    if not ANTHROPIC_KEY and not OPENAI_KEY:
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY", file=sys.stderr)
        return {}

    # ── Assemble context once ─────────────────────────────────────────────────
    context = f"""## PR: {pr_title}

### Description
{pr_body[:2000] or "(no description)"}

### Intent / Linked issue
{linked_issue or "(none)"}

### Diff
```diff
{diff_text[:MAX_DIFF_CHARS]}
{"... (truncated)" if len(diff_text) > MAX_DIFF_CHARS else ""}
```

### File content — BASE (before PR) and HEAD (after PR) of changed files
{file_context or "(not available)"}

### Blast radius — files that import the changed modules (graph-derived)
{impact_summary or "(not available)"}

### Key symbol excerpts
{symbol_excerpts or "(none)"}"""

    # ── Per-check context: diff + only the lines relevant to each check ────────
    # o1's max_completion_tokens covers BOTH reasoning and output.
    # Passing 40k chars leaves no room for output → every check returns [].
    # Solution: each check gets the diff (most important — shows what changed)
    # plus a filtered subset of file content containing lines relevant to it.

    # Patterns per check — extract only lines relevant to that check + surrounding context.
    # Tight patterns = less noise = gpt-4o finds the actual bug.
    # None = no pattern filter (use first N chars of file content as fallback).
    _CHECK_CFG: dict[str, tuple[re.Pattern | None, int]] = {
        # (pattern, context_window_lines)
        "arithmetic":  (re.compile(r'[*/%.]\s|Number\(|BigInt\(|Math\.\w|NaN\b|Infinity\b', re.I), 6),
        "mutation":    (re.compile(r'Object\.assign|\.update\s*\(|\.merge\s*\(|spread', re.I), 6),
        "n_plus_one":  (re.compile(r'\bfor\b|\bwhile\b|\bforEach\b|\.objects\.\w|session\.query|executemany|\.find\s*\(|\.filter\s*\(', re.I), 16),
        "falsy_traps": (re.compile(r'\bor\s+\w|\bif\s+not\b|\|\|\s|\?\?\s|page\s+or\b|\bnot\s+isinstance\b|is\s+None\b|is\s+not\s+None\b', re.I), 8),
        # arch: ONLY class-level declarations (no imports/from — too noisy)
        # arch: class/function defs + window=12 to capture full method bodies
        # Note: diff lines are indented (+    def foo), so match +\s*def not just +def
        "arch":        (re.compile(r'^(?:class |def |export (?:class|function|default)|async function )|\+\s*(?:class |def |export (?:class|function|default)|async function )', re.M), 12),
        # api_compat: response/request shapes, field names, return values
        "api_compat":  (re.compile(r'return\s*\{|response\.|\.json\(|body\s*=|\bdetails\b|\berror\b|\bmessage\b|\bdata\b|\bstatus\b|\btype\b.*:\s*\w', re.I), 8),
        # security: broad — auth, roles, errors, redirects, limits, secrets
        "security":    (re.compile(r'auth|role|permiss|admin\b|error\b|redirect|limit\b|session\b|token\b|secret|password|notFound|guard\b|login|logout|signup|register|removeUser|deleteUser|banUser|currentUser', re.I), 12),
        "quality":     (re.compile(r'\bfor\b|\bwhile\b|TODO|FIXME|"""|console\.\w|logger\.\w|\.log\(|\bprint\(', re.I), 6),
    }

    def _extract_relevant(content: str, pattern: re.Pattern, window: int) -> str:
        lines = content.splitlines()
        keep: set[int] = set()
        for i, line in enumerate(lines):
            if pattern.search(line):
                for j in range(max(0, i - window), min(len(lines), i + window + 1)):
                    keep.add(j)
        if not keep:
            return content[:2000]
        extracted = "\n".join(lines[i] for i in sorted(keep))
        return extracted

    def _filter_diff(raw_diff: str, pattern: re.Pattern, window: int) -> str:
        """Extract only diff lines (+/-) matching pattern plus surrounding context."""
        lines = raw_diff.splitlines()
        keep: set[int] = set()
        for i, line in enumerate(lines):
            # Only match added/removed lines (not context lines starting with space)
            if (line.startswith("+") or line.startswith("-") or line.startswith("@@") or line.startswith("diff")) and pattern.search(line):
                for j in range(max(0, i - window), min(len(lines), i + window + 1)):
                    keep.add(j)
        if not keep:
            return raw_diff[:4000]  # fallback: first 4k
        return "\n".join(lines[i] for i in sorted(keep))

    def _build_check_context(check_name: str) -> str:
        pat, win = _CHECK_CFG.get(check_name, (None, 5))
        header = (
            f"## PR: {pr_title}\n\n"
            f"### Description\n{pr_body[:300] or '(none)'}\n\n"
            f"### Intent\n{linked_issue or '(none)'}\n"
        )
        if pat:
            # Tight filter: extract ONLY pattern-matching lines + small window.
            # Target total context: 2-4k chars — same ballpark as the 251-char
            # snippet where gpt-4o reliably found the bug. More = noise = [].
            filtered_diff  = _filter_diff(diff_text, pat, window=min(win, 5))
            filtered_files = _extract_relevant(file_context, pat, min(win, 5))
            diff_part  = f"### Changed lines (filtered to this check's pattern)\n```diff\n{filtered_diff[:2500]}\n```\n"
            file_part  = f"### File sections (filtered)\n{filtered_files[:1500]}\n"
        else:
            diff_part  = f"### Diff\n```diff\n{diff_text[:4000]}\n```\n"
            file_part  = f"### File content\n{file_context[:2000]}\n"

        # Semantic pre-injection: correct pattern examples from the graph
        # This is the key to catching semantic bugs — the model sees how the
        # same framework/library is correctly used elsewhere in THIS codebase,
        # then compares against the new code. Same as graph_read in GrapeRoot Pro.
        if impact_summary and "existing file" in impact_summary:
            # impact_summary contains codebase pattern examples from _graph_semantic_examples
            semantic_part = f"### How this framework is correctly used in this codebase\n{impact_summary[:3000]}\n"
        elif impact_summary and "Blast Radius" in impact_summary:
            semantic_part = f"### Graph context\n{impact_summary[:1500]}\n"
        else:
            semantic_part = ""

        return header + diff_part + file_part + semantic_part

    # ── Run all checks in parallel, each writing notes on ONE thing ───────────
    notes: dict[str, list[dict]] = {}

    def run_check(name: str, system: str) -> None:
        check_ctx = _build_check_context(name)
        print(f"  [{name}] context={len(check_ctx)} chars")
        try:
            if USE_OPENAI:
                raw = _openai_review(check_ctx, system_override=system)
            else:
                raw = _anthropic_review(check_ctx, system_override=system)
            # Each check returns a flat JSON array
            raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
            raw = re.sub(r"\n?```$", "", raw)
            try:
                parsed = json.loads(raw)
                findings = parsed if isinstance(parsed, list) else parsed.get("inline_comments", [])
            except json.JSONDecodeError:
                m = re.search(r"\[.*\]", raw, re.DOTALL)
                findings = json.loads(m.group(0)) if m else []
            verified = _verify_findings(findings, context)
            print(f"  [{name}] {len(raw)} chars → {len(verified)} findings")
            notes[name] = verified
        except Exception as e:
            print(f"  [{name}] FAILED: {e}", file=sys.stderr)
            notes[name] = []

    threads = [
        threading.Thread(target=run_check, args=(name, prompt), daemon=True)
        for name, prompt in CHECKS
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=200)

    # ── Run static detectors (no LLM, deterministic) ─────────────────────────
    static = (
        detect_n_plus_one(diff_text) +
        detect_falsy_traps(diff_text) +
        detect_rust_index_panics(diff_text) +
        detect_rust_unwrap_panics(diff_text)
    )
    for f in static:
        print(f"  [static:{f['check']}] {f['title'][:60]}")

    # ── Aggregate: merge all notes, dedup by title, sort by severity ──────────
    all_findings: list[dict] = list(static)   # static findings first
    for name, findings in notes.items():
        for f in findings:
            f.setdefault("check", name)
        all_findings.extend(findings)

    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    all_findings.sort(key=lambda c: sev_order.get(c.get("severity", "LOW"), 4))

    seen: set[str] = set()
    deduped: list[dict] = []
    for f in all_findings:
        key = f.get("title", "").lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    print(f"  Total: {len(deduped)} findings after dedup ({len(all_findings)} raw across {len(CHECKS)} checks)")

    return {
        "pr_summary":      f"PR: {pr_title}",
        "inline_comments": deduped,
    }


def _anthropic_review(user_msg: str, system_override: str = "") -> str:
    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": system_override or SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(), method="POST",
        headers={
            "x-api-key":            ANTHROPIC_KEY,
            "anthropic-version":    "2023-06-01",
            "content-type":         "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        return resp["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Anthropic API error {e.code}: {body[:500]}", file=sys.stderr)
        raise


def _openai_review(user_msg: str, system_override: str = "") -> str:
    system = system_override or SYSTEM_PROMPT
    is_reasoning = MODEL.startswith(("o1", "o3", "o4"))
    if is_reasoning:
        payload = {
            "model": MODEL,
            "max_completion_tokens": 8000,
            "messages": [
                {"role": "user", "content": f"{system}\n\n---\n\n{user_msg}"},
            ],
        }
        if MODEL.startswith(("o3", "o4")):
            payload["reasoning_effort"] = "high"
    else:
        payload = {
            "model": MODEL,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
        }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(), method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_KEY}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read())
        return resp["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"OpenAI API error {e.code}: {body[:500]}", file=sys.stderr)
        raise


# ── Static detectors — deterministic [AST-HEURISTIC] without LLM ────────────

def _detect_file_from_diff(lines: list[str], i: int) -> str:
    for k in range(i, -1, -1):
        if lines[k].startswith("diff --git"):
            m = re.search(r" b/(.+)$", lines[k])
            return m.group(1) if m else "unknown"
    return "unknown"


def detect_n_plus_one(diff_text: str) -> list[dict]:
    """Detect for-loops that contain an ORM/DB query per iteration."""
    findings: list[dict] = []
    lines = diff_text.splitlines()
    for_re  = re.compile(r'^\+\s+for\s+(\w+)\s+in\s+', re.I)
    orm_re  = re.compile(r'\.objects\.\w|\.filter\s*\(|\.get\s*\(|\.exists\s*\(|session\.query\s*\(|\.find\s*\(|\.findOne\s*\(|\.execute\s*\(', re.I)

    for i, line in enumerate(lines):
        m = for_re.match(line)
        if not m:
            continue
        loop_var = m.group(1)
        body_lines = []
        for j in range(i + 1, min(i + 15, len(lines))):
            bline = lines[j]
            if not bline.startswith("+"):
                continue
            body_lines.append(bline)
            if orm_re.search(bline):
                excerpt = "\n".join(l for l in [line] + body_lines[:6])
                findings.append({
                    "file":     _detect_file_from_diff(lines, i),
                    "line":     i,
                    "severity": "HIGH",
                    "title":    f"[LLM-HEURISTIC: n-plus-one] N+1 query: '{loop_var}' loop calls DB per iteration",
                    "comment":  f"A database query is issued for every item in the `for {loop_var}` loop.\n"
                                f"Fix: fetch all needed records before the loop with a single query.\n\n"
                                f"```\n{excerpt}\n```",
                    "check": "n_plus_one",
                })
                break
    return findings


def detect_falsy_traps(diff_text: str) -> list[dict]:
    """Detect `page or queryset` and similar falsy-list traps in added lines.
    Matches both assignment form and argument form."""
    findings: list[dict] = []
    lines = diff_text.splitlines()
    # Match `a or b` anywhere on an added line where both look like collections
    or_re    = re.compile(r'\b(\w+)\s+or\s+(\w+)', re.I)
    coll_hint = re.compile(r'page|queryset|result|items|rows|data|records', re.I)

    for i, line in enumerate(lines):
        if not line.startswith("+"):
            continue
        for m in or_re.finditer(line):
            a, b = m.group(1), m.group(2)
            if coll_hint.search(a) and coll_hint.search(b):
                findings.append({
                    "file":     _detect_file_from_diff(lines, i),
                    "line":     i,
                    "severity": "CRITICAL",
                    "title":    f"[LLM-HEURISTIC: falsy-trap] `{a} or {b}` — empty list is falsy, bypasses correct empty result",
                    "comment":  f"If `{a}` is an empty list (e.g. paginator returned no results for this page), "
                                f"`{a} or {b}` evaluates to `{b}` — the full, unpaginated dataset — instead of the empty page.\n"
                                f"Fix: use `{a} if {a} is not None else {b}` or check `if {a} is not None:` explicitly.\n\n"
                                f"Code: `{line.strip()[1:].strip()}`",
                    "check": "falsy_traps",
                })
                break  # one finding per line
    return findings


# ── Rust-specific static detectors ────────────────────────────────────────────

def detect_rust_index_panics(diff_text: str) -> list[dict]:
    """Detect direct Vec/slice indexing in Rust that can panic at runtime.

    Rust panics when index >= len(). The safe alternative is .get(i) which
    returns Option<T>. We flag direct indexing where no bounds check precedes it.

    Catches Greptile-class findings like:
      plan.hashes[pos as usize]     ← panics if pos >= plan.hashes.len()
      plan.new_contents[i]          ← panics if i >= plan.new_contents.len()
    """
    findings: list[dict] = []
    lines = diff_text.splitlines()
    # Match direct indexing: anything[expr] on an added line
    index_re = re.compile(r'^\+.*\b(\w+)\[([^\]]+)\]', re.I)
    # Patterns that constitute a valid bounds check
    bounds_re = re.compile(r'\.get\(|\.len\(\)|< \w+\.len|>= \w+\.len|\bif\b.*\blen\b|\bpanic\b|\bassert\b', re.I)

    for i, line in enumerate(lines):
        if not line.startswith("+"):
            continue
        m = index_re.match(line)
        if not m:
            continue
        container, expr = m.group(1), m.group(2).strip()
        # Skip: integer literals, string keys, simple constants — only flag dynamic expressions
        if re.match(r'^["\'\d]', expr) or expr.isupper():
            continue
        # Skip if there's a bounds check in the preceding 12 added lines
        preceding = [lines[j] for j in range(max(0, i-12), i) if lines[j].startswith("+")]
        if any(bounds_re.search(l) for l in preceding):
            continue
        # Also skip if the line itself uses .get() pattern
        if ".get(" in line:
            continue
        findings.append({
            "file":     _detect_file_from_diff(lines, i),
            "line":     i,
            "severity": "HIGH",
            "title":    f"[LLM-HEURISTIC: rust-bounds] `{container}[{expr[:30]}]` panics if index out of bounds",
            "comment":  f"Direct indexing panics at runtime when `{expr}` >= `{container}.len()`.\n"
                        f"Fix: use `{container}.get({expr})` which returns `Option<_>`, then handle `None`.\n\n"
                        f"Code: `{line.strip()[1:].strip()}`",
            "check": "rust_bounds",
        })
    return findings


def detect_rust_unwrap_panics(diff_text: str) -> list[dict]:
    """Detect .unwrap() on Result/Option in production Rust code.

    .unwrap() panics on Err/None. In production paths this causes silent
    crashes. Use ? operator, expect("reason"), or proper error handling.
    Skips test files (tests are allowed to unwrap).
    """
    findings: list[dict] = []
    lines = diff_text.splitlines()
    unwrap_re = re.compile(r'^\+.*\b(unwrap|expect)\s*\(', re.I)

    for i, line in enumerate(lines):
        if not line.startswith("+"):
            continue
        m = unwrap_re.search(line)
        if not m:
            continue
        file_path = _detect_file_from_diff(lines, i)
        # Skip test files
        if any(x in file_path for x in ("test", "spec", "mock", "fixture")):
            continue
        # Skip if it's in a comment
        stripped = line.strip()
        if stripped.startswith("+//") or stripped.startswith("+#"):
            continue
        method = m.group(1)
        findings.append({
            "file":     file_path,
            "line":     i,
            "severity": "MEDIUM",
            "title":    f"[LLM-HEURISTIC: rust-unwrap] `.{method}()` panics on None/Err in production code",
            "comment":  f"`.{method}()` causes a runtime panic when the value is `None` or `Err`.\n"
                        f"In production paths, use `?` to propagate errors, or handle the failure explicitly.\n\n"
                        f"Code: `{line.strip()[1:].strip()}`",
            "check": "rust_unwrap",
        })
    return findings


# ── Post comments to GitHub ────────────────────────────────────────────────────

def _sev_emoji(s: str) -> str:
    return {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(s.upper(), "·")


def post_review(
    owner: str, repo: str, pr_num: int,
    report: dict,
    pr_head_sha: str,
    dry_run: bool = False,
) -> None:
    inline   = report.get("inline_comments", [])
    sast     = report.get("sast_findings", [])
    iac      = report.get("iac_findings", [])
    secrets  = report.get("secrets_found", [])
    missing  = report.get("missing_from_diff", [])
    blast    = report.get("blast_radius", {})
    gates    = report.get("quality_gates", {})
    summary  = report.get("pr_summary", "")
    score    = report.get("quality_score")
    grade    = report.get("security_grade", "")
    gaps     = report.get("test_coverage_gaps", [])
    dead     = report.get("dead_code", [])
    attack   = report.get("attack_surface_delta", {})

    total_findings = len(inline) + len(sast) + len(iac) + len(secrets) + len(missing)

    if total_findings == 0 and not gates.get("blocking_issues"):
        print("  No issues found — PR looks clean.")
        gh(f"/repos/{owner}/{repo}/issues/{pr_num}/comments",
           method="POST",
           body={"body": "**GrapeRoot Review** — No issues found. This PR looks clean.\n\n_Powered by [GrapeRoot](https://graperoot.dev) — graph-aware AI review_"})
        return

    lines: list[str] = ["**GrapeRoot Review**\n"]

    if summary:
        lines.append(f"> {summary}\n")

    # Score / grade
    score_parts = []
    if score is not None:
        score_parts.append(f"Quality: **{score}/100**")
    if grade:
        score_parts.append(f"Security: **{grade}**")
    if not gates.get("pass", True):
        score_parts.append("**❌ Do not merge**")
    if score_parts:
        lines.append(" · ".join(score_parts) + "\n")

    # Inline comment summary
    by_sev: dict[str, list] = {}
    for c in inline:
        by_sev.setdefault(c.get("severity", "LOW"), []).append(c)
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        items = by_sev.get(sev, [])
        if items:
            emoji = _sev_emoji(sev)
            lines.append(f"{emoji} **{sev}** ({len(items)}): " + ", ".join(c.get("title","") for c in items))

    # SAST
    if sast:
        lines.append(f"\n**SAST findings ({len(sast)})**")
        for f in sast[:5]:
            lines.append(f"- `{f.get('rule','')}` [{f.get('severity','')}] — {f.get('detail','')[:120]}")

    # IaC
    if iac:
        lines.append(f"\n**IaC findings ({len(iac)})**")
        for f in iac[:5]:
            lines.append(f"- `{f.get('file','')}` — {f.get('rule','')} — {f.get('detail','')[:100]}")

    # Secrets
    if secrets:
        lines.append(f"\n**⚠️ Secrets detected ({len(secrets)})**")
        for s in secrets:
            lines.append(f"- `{s.get('file','')}:{s.get('line','')}` — {s.get('type','')} (`{s.get('value_preview','')}`)")

    # Missing from diff
    if missing:
        lines.append(f"\n**Missing from diff** (files that should have changed)")
        for m in missing[:8]:
            lines.append(f"- `{m}`")

    # Blast radius
    if blast and blast.get("risk") in ("CRITICAL", "HIGH"):
        lines.append(f"\n**Blast radius: {blast.get('risk','')}**")
        lines.append(blast.get("explanation", "")[:200])
        callers = blast.get("direct_callers", [])
        if callers:
            lines.append("Affected files: " + ", ".join(f"`{c}`" for c in callers[:6]))

    # Test gaps
    if gaps:
        lines.append(f"\n**Test coverage gaps ({len(gaps)})**")
        for g in gaps[:4]:
            lines.append(f"- `{g.get('function_or_class','')}` in `{g.get('file','')}` — {g.get('risk','')[:80]}")

    # Dead code
    if dead:
        lines.append(f"\n**Dead exports** (graph-confirmed): " + ", ".join(f"`{d}`" for d in dead[:5]))

    # Attack surface
    if attack.get("net_risk") == "INCREASED" and attack.get("increased"):
        lines.append(f"\n**Attack surface increased:** " + ", ".join(attack["increased"][:3]))

    # Blocking issues
    if gates.get("blocking_issues"):
        lines.append("\n**Blocking issues**")
        for b in gates["blocking_issues"][:5]:
            lines.append(f"- {b}")

    lines.append("\n_Powered by [GrapeRoot](https://graperoot.dev) — graph-aware AI review_")
    body = "\n".join(lines)

    # Build inline comments from the inline_comments field
    gh_comments = []
    for c in inline[:MAX_COMMENTS]:
        sug = c.get("suggestion", "")
        comment_body = f"**[{c.get('severity','?')}] {c.get('title','')}**\n\n{c.get('comment', c.get('body',''))}"
        if sug:
            comment_body += f"\n\n{sug}"
        gh_comments.append({
            "path": c.get("file", c.get("path", "")),
            "line": c.get("line", 1),
            "side": "RIGHT",
            "body": comment_body,
        })

    review_payload = {
        "commit_id": pr_head_sha,
        "body":      body,
        "event":     "COMMENT",
        "comments":  gh_comments,
    }

    if dry_run:
        print("\n── DRY RUN — would post this review ──")
        print(f"Body:\n{body}\n")
        for c in gh_comments:
            print(f"  [{c['body'][:120]}]  → {c['path']}:{c['line']}")
        return

    try:
        result = gh(f"/repos/{owner}/{repo}/pulls/{pr_num}/reviews",
                    method="POST", body=review_payload)
        print(f"  Posted review: {result.get('html_url', result.get('id', '?'))}")
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
        print("  422 on inline comments — falling back to body-only review", file=sys.stderr)
        fallback_payload = {
            "commit_id": pr_head_sha,
            "body":      body,
            "event":     "COMMENT",
            "comments":  [],
        }
        result = gh(f"/repos/{owner}/{repo}/pulls/{pr_num}/reviews",
                    method="POST", body=fallback_payload)
        print(f"  Posted fallback review: {result.get('html_url', result.get('id', '?'))}")


# ── Side-by-side comparison ────────────────────────────────────────────────────

def compare_mode(
    pr_title: str, pr_body: str, diff_text: str,
    impact_summary: str, symbol_excerpts: str,
) -> None:
    """Run GrapeRoot Review vs plain Claude on same PR, print comparison."""
    print("\n" + "=" * 60)
    print("A: GrapeRoot Review  (graph context)")
    print("=" * 60)
    t0 = time.time()
    gr_comments = claude_review(pr_title, pr_body, "", diff_text,
                                impact_summary, symbol_excerpts)
    gr_time = time.time() - t0

    print("\n" + "=" * 60)
    print("B: Plain Claude  (diff only, no graph)")
    print("=" * 60)
    t0 = time.time()
    plain_comments = claude_review(pr_title, pr_body, "", diff_text, "", "")
    plain_time = time.time() - t0

    print("\n" + "─" * 60)
    print(f"GrapeRoot Review  — {len(gr_comments)} comments  ({gr_time:.1f}s)")
    for c in gr_comments:
        print(f"  [{c['severity']:8}] {c['path']}:{c.get('line','?')}  {c['title']}")

    print(f"\nPlain Claude      — {len(plain_comments)} comments  ({plain_time:.1f}s)")
    for c in plain_comments:
        print(f"  [{c['severity']:8}] {c['path']}:{c.get('line','?')}  {c['title']}")

    # Unique findings
    gr_titles   = {c["title"] for c in gr_comments}
    plain_titles= {c["title"] for c in plain_comments}
    only_gr     = gr_titles - plain_titles
    only_plain  = plain_titles - gr_titles

    print("\n── Only found by GrapeRoot (graph-aware):")
    for t in only_gr:   print(f"  + {t}")
    print("── Only found by Plain Claude:")
    for t in only_plain: print(f"  + {t}")
    print("─" * 60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="GrapeRoot Review")
    ap.add_argument("pr_url", help="GitHub PR URL")
    ap.add_argument("--dry",      action="store_true", help="Print review, don't post")
    ap.add_argument("--vs",       action="store_true", help="Side-by-side vs plain Claude")
    ap.add_argument("--port",     type=int, default=0, help="GrapeRoot MCP port")
    ap.add_argument("--json-out", default="",          help="Write findings + cost to this JSON file")
    args = ap.parse_args()

    global MCP_PORT
    if args.port:
        MCP_PORT = args.port

    if not GITHUB_TOKEN:
        print("Error: set GITHUB_TOKEN env var", file=sys.stderr); sys.exit(1)
    if not ANTHROPIC_KEY and not OPENAI_KEY:
        print("Error: set ANTHROPIC_API_KEY or OPENAI_API_KEY", file=sys.stderr); sys.exit(1)

    owner, repo, pr_num = parse_pr_url(args.pr_url)
    print(f"GrapeRoot Review: {owner}/{repo}#{pr_num}")

    # ── Fetch PR data ──────────────────────────────────────────────────────────
    print("  Fetching PR...")
    pr      = gh(f"/repos/{owner}/{repo}/pulls/{pr_num}")
    title   = pr.get("title", "")
    body    = pr.get("body", "") or ""
    head_sha= pr["head"]["sha"]

    # Extract linked issue from body (#123 or full URL)
    linked_issue = ""
    issue_m = re.search(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#(\d+)", body, re.I)
    if issue_m:
        try:
            iss = gh(f"/repos/{owner}/{repo}/issues/{issue_m.group(1)}")
            linked_issue = f"#{issue_m.group(1)}: {iss.get('title','')} — {iss.get('body','')[:400]}"
        except Exception:
            pass

    base_sha = pr["base"]["sha"]

    print("  Fetching diff...")
    diff    = gh_diff(owner, repo, pr_num)
    changed_files, hunks = parse_diff(diff)
    print(f"  Changed files: {len(changed_files)}")

    # ── Codebase context (full file content via GitHub API — no clone needed) ──
    print(f"  Fetching full file context for {len(changed_files)} file(s)...")
    file_context = build_file_context(owner, repo, changed_files, base_sha, head_sha)
    print(f"  File context: {len(file_context)} chars")

    # ── Graph context (MCP / pre-built — used when available) ─────────────────
    impact_summary  = ""
    symbol_excerpts = ""

    if GR_GRAPH_CONTEXT:
        impact_summary = GR_GRAPH_CONTEXT
        print(f"  Graph: using pre-built context ({len(GR_GRAPH_CONTEXT)} chars)")
    elif mcp_available():
        print(f"  Graph: running impact analysis on {len(changed_files)} files...")
        impact = graph_impact(changed_files)
        if impact:
            affected = impact.get("affected_files", [])[:8]
            impact_summary = (
                f"Changed files touch {len(affected)} downstream files:\n" +
                "\n".join(f"  - {f}" for f in affected)
            )
            recommended = impact.get("recommended_reads", changed_files[:3])
            excerpts = []
            for ref in recommended[:4]:
                text = graph_read_symbol(ref)
                if text:
                    excerpts.append(f"### {ref}\n{text[:800]}")
            symbol_excerpts = "\n\n".join(excerpts)
    else:
        print(f"  Graph: not available — reviewing diff only")
        print(f"  Tip: run  dgc {os.getcwd()}  first for graph-aware review")

    # ── Run review ─────────────────────────────────────────────────────────────
    diff_text = "\n\n".join(f"### {p}\n{h}" for p, h in list(hunks.items())[:10])

    if args.vs:
        compare_mode(title, body, diff_text, impact_summary, symbol_excerpts)
        return

    print("  Reviewing with AI...")
    report = claude_review(title, body, linked_issue, diff_text,
                           impact_summary, symbol_excerpts, file_context)

    inline = report.get("inline_comments", [])
    total = (len(inline) + len(report.get("sast_findings", [])) +
             len(report.get("iac_findings", [])) + len(report.get("missing_from_diff", [])))
    print(f"\n  Found {total} total findings ({len(inline)} inline):")
    for c in inline:
        sev_icon = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🔵"}.get(c.get("severity",""),"·")
        print(f"  {sev_icon} [{c.get('severity','?'):8}] {c.get('file',c.get('path','?'))}:{c.get('line','?')} {c.get('title','')}")

    t_review = time.time()
    post_review(owner, repo, pr_num, report, head_sha, dry_run=args.dry)

    if args.json_out:
        import pathlib
        pathlib.Path(args.json_out).write_text(json.dumps({
            "findings": inline,
            "report":   report,
            "cost_usd": 0.0,
            "elapsed_s": round(time.time() - t_review, 1),
        }))


if __name__ == "__main__":
    main()
