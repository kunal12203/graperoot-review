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
    parts: list[str] = []
    budget = max_chars

    for path in changed_files[:6]:
        if budget <= 0:
            break

        # Base (before PR) — FULL content, critical for spotting omissions
        base = gh_file(owner, repo, path, base_sha)
        if base:
            snippet = base[:min(15000, budget)]
            parts.append(f"### {path}  [BASE — before this PR]\n```\n{snippet}\n{'...(truncated)' if len(base) > len(snippet) else ''}\n```")
            budget -= len(snippet)

        # Head (after PR) for new files or heavily modified ones
        head = gh_file(owner, repo, path, head_sha)
        if head and head != base:
            snippet = head[:min(10000, budget)]
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
            m = re.search(r"b/(.+)$", line)
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

ARCH_PROMPT = """You are a senior architect. Your ONLY job: find what is MISSING, INCOMPLETE, or ASYMMETRIC in this PR.

ANTI-HALLUCINATION: Quote exact lines from the provided context. Do not invent code.

You MUST do these checks in order and report findings for EACH:

STEP 1 — REFACTOR INVENTORY: List every class and public method in the BASE version of changed files.
Then for each method NOT moved to the new module:
  - Read its BODY (not just signature). Does the body actually call framework-specific APIs?
  - If the body only calls self.X() where X was already moved, this is an ORPHAN — report it as HIGH.
  Example: `def make_null_session(self, app: Flask): return self.null_session_class()`
  — body never uses `app`, only calls `self.null_session_class()` which moved to sansio → flag it.

STEP 2 — SIBLING ASYMMETRY: Find methods moved to the new module. Find their sibling methods.
If sibling A moved but sibling B did not, and B has no framework dependency in its body → flag B.

STEP 3 — DEFAULT VALUE CHANGES: Compare class-level attribute defaults between BASE and HEAD.
Any change (False→True, None→value) is a behavioral change → report as MEDIUM.

STEP 4 — MISSING FILES: Based on blast radius, which files that import changed modules are NOT in diff?

Every inline comment title MUST begin with `[LLM-HEURISTIC: arch-check]`. Example:
  "title": "[LLM-HEURISTIC: arch-check] make_null_session not moved to sansio"

Return ONLY JSON:
{
  "inline_comments": [{"file":"","line":0,"severity":"CRITICAL|HIGH|MEDIUM|LOW",
    "category":"refactor|contract","title":"[LLM-HEURISTIC: arch-check] ...","comment":"cite exact line","suggestion":"","graph_proven":false}],
  "missing_from_diff": ["file paths that should have been in this PR"],
  "jira_intent_check": {"matches_description":true,"gaps":[],"missing_files":[]}
}"""

SEC_PROMPT = """You are a security engineer reviewing a PR for bugs, vulnerabilities, and data integrity issues.
Your ONLY job: find things that will crash, corrupt data, or behave incorrectly at runtime.

ANTI-HALLUCINATION: Every finding must cite the exact line(s) from the provided context. Do not invent code.

Run these checks IN ORDER. Do not skip any.

STEP 1 — ARITHMETIC SAFETY (highest priority):
For every division, modulo, and numeric conversion in the diff:
  a. Division/modulo: can the denominator ever be zero? Check every call site and data source.
     Look for: `x / y`, `x % y`, `Number(bigint) / Number(other)` where other could be 0n.
  b. BigInt↔float traps: `Number(bigint)` where bigint is large → Infinity.
     Then `BigInt(Math.round(Infinity))` or `BigInt(Math.ceil(Infinity))` throws RangeError.
     Pattern to find: any chain of `Number(bigint)` → arithmetic → `BigInt(result)`.
  c. NaN propagation: any place where NaN can silently replace a numeric value.
Report each as HIGH or CRITICAL with the exact expression and the input value that causes failure.

STEP 2 — MUTATION TRAPS (Object.assign, spread, merge):
For every `Object.assign(target, source)` or `{...target, ...source}`:
  a. List the keys already written to target before the assign.
  b. Check if source can contain any of those same keys (user input, `extra` param, options bag).
  c. If yes: source silently overwrites target's values — report as HIGH.
     Exact pattern: `body = { error: code, message: msg }; Object.assign(body, extra)` →
     if extra has `error` or `message`, they are overwritten.

STEP 3 — REDUNDANT / RACY OPERATIONS:
  a. Same data fetched from network/DB twice in the same request or render cycle.
  b. A loop where an inner call re-fetches data already available in an outer scope.
  c. Report double fetches as MEDIUM (race window + wasted latency).

STEP 4 — SECURITY (original focus):
  - Injection, auth bypass, privilege escalation, insecure defaults, exposed secrets.
  - Behavioral changes that affect callers (key ordering, default values, field semantics).

Every inline comment title MUST begin with `[LLM-HEURISTIC: security-check]`.

Return ONLY JSON:
{
  "inline_comments": [{"file":"","line":0,"severity":"CRITICAL|HIGH|MEDIUM|LOW",
    "category":"security|logic|performance|reliability","title":"[LLM-HEURISTIC: security-check] ...",
    "comment":"cite exact line","suggestion":"","graph_proven":false}],
  "sast_findings": [{"severity":"","rule":"","file":"","line":0,"detail":""}],
  "secrets_found": [{"file":"","line":0,"type":"","value_preview":""}],
  "attack_surface_delta": {"increased":[],"decreased":[],"net_risk":"NEUTRAL"}
}"""

QUAL_PROMPT = """You are a quality engineer reviewing a PR for reliability, performance, testability, and maintainability.
Your ONLY job: find quality gaps.

ANTI-HALLUCINATION: Every finding must cite exact code from the provided context.

Run these checks IN ORDER:

STEP 1 — ALGORITHMIC COMPLEXITY (often overlooked, high impact):
For every loop in the diff:
  a. Does the loop body call a function that itself iterates over a collection?
     Pattern: `for x of list { list2.find(y => y.id === x.id) }` → O(n²).
  b. Is there a helper function called on EVERY iteration that does a linear scan internally?
     Look at the helper's body — does it contain `.find()`, `.filter()`, `.indexOf()`, or a for-loop?
  c. If yes → report as HIGH with the outer loop + the inner scan, plus the O(n²) impact at scale.
     Suggest: build a Map/Set once before the loop, look up in O(1) per iteration.

STEP 2 — REDUNDANT COMPUTATIONS:
  a. Is the same value computed or fetched more than once within a loop or function?
  b. Could it be cached/memoized before the loop?
  Report as MEDIUM.

STEP 3 — TEST GAPS — risky code paths with no test coverage
STEP 4 — DEAD CODE — exported symbols with no importers (use graph context)
STEP 5 — COMPLEXITY — functions that are too long or deeply nested
STEP 6 — DOCUMENTATION — docstring typos, missing spaces, run-on words
STEP 7 — DUPLICATION — similar logic in multiple places

Every inline comment title MUST begin with `[LLM-HEURISTIC: quality-check]`. Example:
  "title": "[LLM-HEURISTIC: quality-check] Docstring missing space before backtick"

Return ONLY JSON:
{
  "inline_comments": [{"file":"","line":0,"severity":"CRITICAL|HIGH|MEDIUM|LOW",
    "category":"test|reliability|documentation","title":"[LLM-HEURISTIC: quality-check] ...",
    "comment":"cite exact line","suggestion":"","graph_proven":false}],
  "test_coverage_gaps": [{"file":"","function_or_class":"","risk":""}],
  "dead_code": ["exported symbols with zero importers"],
  "complex_functions": [{"file":"","function":"","cyclomatic_complexity":"high|very_high","reason":""}],
  "duplicate_code": [{"files":[],"pattern":""}],
  "quality_score": 0,
  "security_grade": "A"
}"""


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

def _verify_findings(findings: list[dict], context: str) -> list[dict]:
    """Filter hallucinations: only check snippets that look like actual code, not prose."""
    verified = []
    for f in findings:
        comment = f.get("comment", f.get("body", ""))
        # Only extract snippets that contain programming syntax (not prose explanation)
        all_snippets = re.findall(r"`([^`\n]{4,120})`", comment)
        code_snippets = [s for s in all_snippets if _CODE_RE.search(s)]
        ok = True
        for snippet in code_snippets[:3]:
            if snippet not in context:
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
    """Run 3 specialized AI agents in parallel, verify findings, merge into one report."""
    import threading

    if not ANTHROPIC_KEY and not OPENAI_KEY:
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY", file=sys.stderr)
        return {}

    full_context = f"""## PR: {pr_title}

### Description
{pr_body[:2000] or "(no description)"}

### Linked Issue / Intent
{linked_issue or "(none)"}

### Diff (what changed)
```diff
{diff_text[:MAX_DIFF_CHARS]}
{"... (truncated)" if len(diff_text) > MAX_DIFF_CHARS else ""}
```

### Full file content — BASE and HEAD versions + affected files
{file_context or "(not available)"}

### Blast radius — files connected to changed modules (graph)
{impact_summary or "(not available)"}

### Key symbol excerpts
{symbol_excerpts or "(none)"}"""

    results: dict[str, dict] = {}
    errors:  dict[str, str]  = {}

    def run_agent(name: str, system: str) -> None:
        try:
            if USE_OPENAI:
                text = _openai_review(full_context, system_override=system)
            else:
                text = _anthropic_review(full_context, system_override=system)
            parsed = _parse_json(text)
            print(f"  [{name}] {len(text)} chars → {len(parsed.get('inline_comments', []))} inline")
            results[name] = parsed
        except Exception as e:
            errors[name] = str(e)
            print(f"  [{name}] FAILED: {e}", file=sys.stderr)
            results[name] = {}

    agents = [
        ("arch", ARCH_PROMPT),
        ("sec",  SEC_PROMPT),
        ("qual", QUAL_PROMPT),
    ]
    threads = [threading.Thread(target=run_agent, args=(n, s), daemon=True) for n, s in agents]
    for t in threads: t.start()
    for t in threads: t.join(timeout=200)

    # Merge all results
    report: dict = {"pr_summary": f"PR: {pr_title}"}
    all_inline: list[dict] = []
    for name, result in results.items():
        inline = result.get("inline_comments", [])
        # Verify: only keep findings grounded in actual context
        verified = _verify_findings(inline, full_context + diff_text)
        all_inline.extend(verified)
        # Merge list fields
        for key in ("sast_findings","iac_findings","secrets_found","dead_code",
                    "duplicate_code","complex_functions","test_coverage_gaps","missing_from_diff"):
            if result.get(key):
                report.setdefault(key, [])
                report[key].extend(result[key])
        # Merge dict fields
        for key in ("blast_radius","attack_surface_delta","jira_intent_check","quality_gates"):
            if result.get(key) and key not in report:
                report[key] = result[key]
        # Scalars
        if result.get("quality_score") and "quality_score" not in report:
            report["quality_score"] = result["quality_score"]
        if result.get("security_grade") and "security_grade" not in report:
            report["security_grade"] = result["security_grade"]

    # Deduplicate inline comments by title
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    all_inline.sort(key=lambda c: sev_order.get(c.get("severity", "LOW"), 4))
    for c in all_inline:
        t = c.get("title", "").lower().strip()
        if t not in seen_titles:
            seen_titles.add(t)
            deduped.append(c)

    report["inline_comments"] = deduped
    print(f"  Total: {len(deduped)} inline after dedup+verify ({len(all_inline)} raw)")
    return report


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
            "max_completion_tokens": 5000,
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
