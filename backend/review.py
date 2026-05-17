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
MAX_DIFF_CHARS   = 12_000
MAX_COMMENTS     = 8

USE_OPENAI = bool(OPENAI_KEY and not ANTHROPIC_KEY)
MODEL          = "gpt-4o" if USE_OPENAI else os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")


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


# ── Claude review ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior software engineer doing a code review.
You have access to the full codebase context via the graph — use it.

Return a JSON array of review comments. Each comment:
{
  "path": "src/file.ts",       // file path relative to repo root
  "line": 42,                  // line number in the NEW file (right side of diff)
  "severity": "CRITICAL|HIGH|MEDIUM|LOW|STYLE",
  "title": "one-line summary",
  "body": "detailed explanation with suggested fix if applicable"
}

Rules:
- CRITICAL: bug that will cause incorrect behavior, data loss, security issue
- HIGH: logic error, missing error handling on external call, race condition
- MEDIUM: performance issue, missing test coverage for risky change
- LOW: code smell, unclear naming
- STYLE: formatting, whitespace — OMIT these unless specifically asked
- Maximum 8 comments total. Rank by severity, omit STYLE entirely.
- If the PR is clean, return [] (empty array).
- Return ONLY the JSON array, no markdown wrapper."""


def claude_review(
    pr_title: str,
    pr_body: str,
    linked_issue: str,
    diff_text: str,
    impact_summary: str,
    symbol_excerpts: str,
) -> list[dict]:
    """Call Claude or GPT-4o with all context and return structured review comments."""
    if not ANTHROPIC_KEY and not OPENAI_KEY:
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY", file=sys.stderr)
        return []

    user_msg = f"""## PR: {pr_title}

### Description
{pr_body[:2000] or "(no description)"}

### Linked Issue / Intent
{linked_issue or "(none)"}

### Blast Radius (files affected beyond the diff)
{impact_summary or "(graph not available — reviewing diff only)"}

### Key Symbol Excerpts (from changed files)
{symbol_excerpts or "(none)"}

### Diff
```diff
{diff_text[:MAX_DIFF_CHARS]}
{"... (truncated)" if len(diff_text) > MAX_DIFF_CHARS else ""}
```

Review this PR. Return a JSON array of comments as instructed."""

    if USE_OPENAI:
        text = _openai_review(user_msg)
    else:
        text = _anthropic_review(user_msg)

    text = re.sub(r"^```(?:json)?\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"Model returned non-JSON:\n{text[:400]}", file=sys.stderr)
        return []


def _anthropic_review(user_msg: str) -> str:
    payload = {
        "model": MODEL,
        "max_tokens": 2048,
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
    payload = {
        "model": MODEL,
        "max_tokens": 2048,
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
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return resp["choices"][0]["message"]["content"]


# ── Post comments to GitHub ────────────────────────────────────────────────────

def post_review(
    owner: str, repo: str, pr_num: int,
    comments: list[dict],
    pr_head_sha: str,
    dry_run: bool = False,
) -> None:
    if not comments:
        print("  No issues found — PR looks clean.")
        gh(f"/repos/{owner}/{repo}/issues/{pr_num}/comments",
           method="POST",
           body={"body": "**GrapeRoot Review** — No issues found. This PR looks clean.\n\n_Powered by [GrapeRoot](https://graperoot.dev) — graph-aware AI review_"})
        return

    # Build GitHub review body (summary at top)
    by_sev: dict[str, list[dict]] = {}
    for c in comments:
        by_sev.setdefault(c["severity"], []).append(c)

    summary_lines = ["**GrapeRoot Review**\n"]
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        items = by_sev.get(sev, [])
        if items:
            emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}[sev]
            summary_lines.append(f"{emoji} **{sev}** ({len(items)}): " +
                                  ", ".join(c["title"] for c in items))

    summary_lines += [
        "",
        "_Powered by [GrapeRoot](https://graperoot.dev) — graph-aware AI review_",
    ]
    body = "\n".join(summary_lines)

    # Build inline comments (GitHub requires position in diff or line + side)
    gh_comments = []
    for c in comments[:MAX_COMMENTS]:
        gh_comments.append({
            "path":     c["path"],
            "line":     c.get("line", 1),
            "side":     "RIGHT",
            "body":     f"**[{c['severity']}] {c['title']}**\n\n{c['body']}",
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
        # Inline comment line numbers didn't match diff positions — fall back to
        # a single body-only review that lists all findings inline as text.
        print("  422 on inline comments — falling back to body-only review", file=sys.stderr)
        fallback_lines = ["**GrapeRoot Review**\n"]
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            items = by_sev.get(sev, [])
            for c in items:
                emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(sev, "·")
                fallback_lines.append(
                    f"{emoji} **[{sev}]** `{c['path']}` — **{c['title']}**\n\n{c['body']}\n"
                )
        fallback_lines.append("\n_Powered by [GrapeRoot](https://graperoot.dev) — graph-aware AI review_")
        fallback_payload = {
            "commit_id": pr_head_sha,
            "body":      "\n".join(fallback_lines),
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

    print("  Fetching diff...")
    diff    = gh_diff(owner, repo, pr_num)
    changed_files, hunks = parse_diff(diff)
    print(f"  Changed files: {len(changed_files)}")

    # ── Graph context ──────────────────────────────────────────────────────────
    impact_summary  = ""
    symbol_excerpts = ""

    if GR_GRAPH_CONTEXT:
        # Injected by webhook server (graph already built + queried there)
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

    print("  Reviewing with Claude...")
    comments = claude_review(title, body, linked_issue, diff_text,
                             impact_summary, symbol_excerpts)

    # Print summary
    print(f"\n  Found {len(comments)} issue(s):\n")
    for c in comments:
        sev_icon = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🔵","STYLE":"⚪"}.get(c["severity"],"·")
        print(f"  {sev_icon} [{c['severity']:8}] {c['path']}:{c.get('line','?')}")
        print(f"             {c['title']}")
        print(f"             {c['body'][:120]}...\n" if len(c['body']) > 120
              else f"             {c['body']}\n")

    t_review = time.time()
    post_review(owner, repo, pr_num, comments, head_sha, dry_run=args.dry)

    if args.json_out:
        cost_usd = _last_cost if hasattr(sys.modules[__name__], "_last_cost") else 0.0
        import pathlib
        pathlib.Path(args.json_out).write_text(json.dumps({
            "findings": comments,
            "cost_usd": cost_usd,
            "elapsed_s": round(time.time() - t_review, 1),
        }))


if __name__ == "__main__":
    main()
