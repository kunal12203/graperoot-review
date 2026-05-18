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


def claude_review(
    pr_title: str,
    pr_body: str,
    linked_issue: str,
    diff_text: str,
    impact_summary: str,
    symbol_excerpts: str,
    file_context: str = "",
) -> dict:
    """Call the model and return the full 14-category structured report."""
    if not ANTHROPIC_KEY and not OPENAI_KEY:
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY", file=sys.stderr)
        return {}

    user_msg = f"""## PR: {pr_title}

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

    if USE_OPENAI:
        text = _openai_review(user_msg)
    else:
        text = _anthropic_review(user_msg)

    text = re.sub(r"^```(?:json)?\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text)
    print(f"  Model response ({len(text)} chars): {text[:300]}")
    try:
        result = json.loads(text)
        if isinstance(result, list):
            # Backwards-compat: model returned old flat array format
            return {"inline_comments": result}
        return result
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        print(f"  Model returned non-JSON:\n{text[:400]}", file=sys.stderr)
        return {}


def _anthropic_review(user_msg: str) -> str:
    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
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


def _openai_review(user_msg: str) -> str:
    # o1 / o3 reasoning models: no system role, max_completion_tokens, optional reasoning_effort
    is_reasoning = MODEL.startswith(("o1", "o3", "o4"))
    if is_reasoning:
        payload = {
            "model": MODEL,
            # Large budget: o1 uses some tokens for internal reasoning,
            # the rest goes to output. Don't use reasoning_effort on o1 —
            # it spends all budget on reasoning with 0 output tokens left.
            "max_completion_tokens": 16000,
            "messages": [
                {"role": "user", "content": f"{SYSTEM_PROMPT}\n\n---\n\n{user_msg}"},
            ],
        }
        # reasoning_effort is an o3/o4 parameter — NOT for o1
        if MODEL.startswith(("o3", "o4")):
            payload["reasoning_effort"] = "high"
    else:
        payload = {
            "model": MODEL,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
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

    # ── Deterministic pre-checks (graph-proven, no AI needed) ─────────────────
    det_missing: list[str] = []
    det_secrets: list[dict] = []

    # 1. Missing from diff: files that import changed files but aren't in the diff
    if impact_summary:
        import re as _re2
        # Extract file paths mentioned in blast radius that aren't in the PR diff
        mentioned = _re2.findall(r'[\w./\-]+\.(?:py|ts|js|go|java|rs|rb|cs)', impact_summary)
        for f in mentioned:
            if f not in changed_files and f not in det_missing:
                det_missing.append(f)

    # 2. Secrets scan: regex over the diff (deterministic, no AI)
    SECRET_PATTERNS = [
        (r'(?i)(api[_-]?key|secret|password|token|auth)["\s:=]+["\']?([A-Za-z0-9+/]{20,})["\']?', "Potential secret"),
        (r'sk-[A-Za-z0-9]{32,}', "OpenAI API key"),
        (r'ghp_[A-Za-z0-9]{36}', "GitHub PAT"),
        (r'AKIA[A-Z0-9]{16}', "AWS Access Key"),
        (r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', "Private key"),
    ]
    for i, line in enumerate(diff_text.splitlines(), 1):
        if not line.startswith("+"):
            continue
        for pat, label in SECRET_PATTERNS:
            if _re2.search(pat, line):
                det_secrets.append({"file": "diff", "line": i, "type": label,
                                    "value_preview": line[1:50].strip()})
                break

    if det_missing:
        print(f"  Deterministic: {len(det_missing)} files may be missing from diff")
    if det_secrets:
        print(f"  Deterministic: {len(det_secrets)} potential secret(s) detected")

    print("  Reviewing with AI...")
    report = claude_review(title, body, linked_issue, diff_text,
                           impact_summary, symbol_excerpts, file_context)

    # Merge deterministic findings into report
    if det_missing:
        existing_missing = report.get("missing_from_diff", [])
        report["missing_from_diff"] = list(dict.fromkeys(existing_missing + det_missing))
    if det_secrets:
        report.setdefault("secrets_found", []).extend(det_secrets)

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
