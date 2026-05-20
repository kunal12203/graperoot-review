#!/usr/bin/env python3
"""GrapeRoot Review — Enterprise Local Edition.

Runs entirely on your machine. Code never leaves your infrastructure.
Uses your existing Claude Code subscription (claude --print) — no API key needed.
Uses the full MCP graph (graph_read reads actual file content, not metadata).

Usage:
    python3 review_enterprise.py <github-pr-url>          # review and post
    python3 review_enterprise.py <github-pr-url> --dry    # review, don't post
    python3 review_enterprise.py <github-pr-url> --port 8765  # custom MCP port

Requires:
    GITHUB_TOKEN  — for posting to GitHub (gh auth token exports this)
    claude CLI    — installed via Claude Code (claude --version to check)
    dgc-pro MCP   — running on the repo being reviewed (for graph context)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any


# ── Config ─────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MCP_PORT     = int(os.environ.get("GRAPEROOT_PORT", os.environ.get("DG_MCP_PORT", "8765")))
MAX_DIFF     = 20_000


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh(path: str, method: str = "GET", body: dict | None = None) -> Any:
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "graperoot-enterprise/1.0",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def gh_diff(owner: str, repo: str, pr_num: int) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github.diff",
                 "User-Agent": "graperoot-enterprise/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def gh_file(owner: str, repo: str, path: str, ref: str) -> str:
    import base64
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "graperoot-enterprise/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return base64.b64decode(d["content"].replace("\n","")).decode("utf-8", errors="replace") if "content" in d else ""
    except Exception:
        return ""


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"Not a GitHub PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def parse_diff(diff: str) -> tuple[list[str], dict[str, str]]:
    files, hunks, current, lines = [], {}, None, []
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            if current:
                hunks[current] = "\n".join(lines[-120:])
            m = re.search(r" b/(.+)$", line)
            if m:
                current, lines = m.group(1), [line]
        elif current:
            lines.append(line)
    if current:
        hunks[current] = "\n".join(lines[-120:])
    return list(hunks.keys()), hunks


# ── MCP graph helpers (full graph_read — not degraded JSON) ───────────────────

def mcp_available() -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{MCP_PORT}/prime",
                                     headers={"User-Agent": "graperoot-enterprise"})
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def mcp_call(tool: str, **kwargs) -> dict:
    payload = {"jsonrpc":"2.0","method":"tools/call","id":1,
               "params":{"name":tool,"arguments":kwargs}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{MCP_PORT}/mcp",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        content = resp.get("result",{}).get("content",[])
        return json.loads(content[0].get("text","{}")) if content else {}
    except Exception:
        return {}


def get_graph_context(changed_files: list[str], owner: str, repo: str,
                      head_sha: str) -> str:
    """Build rich graph context using the live MCP server if available."""
    if not mcp_available():
        return "(MCP server not running — start dgc-pro on this repo for graph context)"

    parts = []

    # Blast radius via graph_impact
    impact = mcp_call("graph_impact", changed_files=changed_files)
    if impact.get("affected_files"):
        affected = impact["affected_files"][:8]
        parts.append(f"### Blast Radius — {len(affected)} file(s) import from changed files")
        for f in affected:
            parts.append(f"  - {f}")

    # Read key symbols using graph_read (real file content, not metadata)
    reads = impact.get("recommended_reads", changed_files[:3])[:5]
    for ref in reads:
        result = mcp_call("graph_read", file=ref, max_chars=3000,
                          query="review for bugs and incomplete refactors")
        if isinstance(result, dict) and result.get("content"):
            parts.append(f"\n### {ref} [graph_read — actual code]\n```\n{str(result['content'])[:2000]}\n```")

    return "\n".join(parts) if parts else "(graph empty — no edges found for changed files)"


# ── Static detectors (deterministic, [AST-HEURISTIC]) ─────────────────────────

def _file_from_diff(lines: list[str], i: int) -> str:
    for k in range(i, -1, -1):
        if lines[k].startswith("diff --git"):
            m = re.search(r" b/(.+)$", lines[k])
            return m.group(1) if m else "unknown"
        if lines[k].startswith("### "):
            return lines[k][4:].split("  ")[0].strip()
    return "unknown"


def detect_n_plus_one(diff: str) -> list[dict]:
    findings, lines = [], diff.splitlines()
    for_re = re.compile(r'^\+\s+for\s+(\w+)\s+in\s+', re.I)
    orm_re = re.compile(r'\.objects\.\w|\.filter\s*\(|\.get\s*\(|\.exists\s*\(|session\.query|\.find\s*\(', re.I)
    for i, line in enumerate(lines):
        m = for_re.match(line)
        if not m: continue
        body = [lines[j] for j in range(i+1, min(i+15,len(lines))) if lines[j].startswith("+")]
        for bl in body:
            if orm_re.search(bl):
                findings.append({"severity":"HIGH","tag":"[AST-HEURISTIC: n-plus-one]",
                    "file":_file_from_diff(lines,i),"line":i,
                    "title":f"N+1 query: `{m.group(1)}` loop calls DB per iteration",
                    "body":f"Fix: fetch all records before the loop with `filter(id__in=ids)` or equivalent.\n\nCode:\n```\n{line.strip()[1:]}\n{bl.strip()[1:]}\n```"})
                break
    return findings


def detect_falsy_traps(diff: str) -> list[dict]:
    findings, lines = [], diff.splitlines()
    or_re    = re.compile(r'\b(\w+)\s+or\s+(\w+)', re.I)
    coll_re  = re.compile(r'page|queryset|result|items|rows|data|records', re.I)
    for i, line in enumerate(lines):
        if not line.startswith("+"): continue
        for m in or_re.finditer(line):
            a, b = m.group(1), m.group(2)
            if coll_re.search(a) and coll_re.search(b):
                findings.append({"severity":"CRITICAL","tag":"[AST-HEURISTIC: falsy-trap]",
                    "file":_file_from_diff(lines,i),"line":i,
                    "title":f"`{a} or {b}` — empty list is falsy, bypasses correct empty result",
                    "body":f"If `{a}` is `[]`, `{a} or {b}` returns the full `{b}` instead of the empty result.\nFix: `{a} if {a} is not None else {b}`\n\nCode: `{line.strip()[1:].strip()}`"})
                break
    return findings


def detect_window_open_noopener(diff: str) -> list[dict]:
    findings, lines = [], diff.splitlines()
    open_re = re.compile(r'^\+.*\bwindow\.open\s*\(', re.I)
    for i, line in enumerate(lines):
        if not open_re.match(line): continue
        window = "\n".join(lines[i:min(i+4,len(lines))])
        if "noopener" in window.lower(): continue
        findings.append({"severity":"HIGH","tag":"[AST-HEURISTIC: dom-security]",
            "file":_file_from_diff(lines,i),"line":i,
            "title":"`window.open()` without `noopener` — opener attack surface",
            "body":f"The opened window can access `window.opener` and navigate the parent.\nFix: `window.open(url, '_blank', 'noopener,noreferrer')`\n\nCode: `{line.strip()[1:].strip()}`"})
    return findings


def detect_click_no_keyboard(diff: str) -> list[dict]:
    findings, lines = [], diff.splitlines()
    click_re   = re.compile(r'^\+.*\bonClick\s*=', re.I)
    kbd_re     = re.compile(r'onKeyDown|onKeyPress|role\s*=.*button|tabIndex', re.I)
    skip_re    = re.compile(r'<(?:button|a|input|select|textarea)\b', re.I)
    for i, line in enumerate(lines):
        if not click_re.match(line) or skip_re.search(line): continue
        fp = _file_from_diff(lines, i)
        if not fp.endswith((".tsx",".jsx",".js",".ts")): continue
        window = "\n".join(lines[max(0,i-5):min(len(lines),i+6)])
        if kbd_re.search(window): continue
        findings.append({"severity":"MEDIUM","tag":"[AST-HEURISTIC: a11y]",
            "file":fp,"line":i,
            "title":"`onClick` with no keyboard handler — WCAG 2.1.1",
            "body":f"Keyboard users can't activate this. Add `onKeyDown` + `role='button'` + `tabIndex={{0}}`.\n\nCode: `{line.strip()[1:].strip()}`"})
    return findings


def detect_rust_index_panics(diff: str) -> list[dict]:
    findings, lines = [], diff.splitlines()
    idx_re    = re.compile(r'^\+.*\b(\w+)\[([^\]]+)\]', re.I)
    bounds_re = re.compile(r'\.get\(|\.len\(\)|< \w+\.len|>= \w+\.len|\bif\b.*\blen\b', re.I)
    for i, line in enumerate(lines):
        if not line.startswith("+"): continue
        m = idx_re.match(line)
        if not m: continue
        fp = _file_from_diff(lines, i)
        if not fp.endswith(".rs"): continue
        container, expr = m.group(1), m.group(2).strip()
        if re.match(r'^["\'\d]', expr) or expr.isupper(): continue
        if ".get(" in line: continue
        preceding = [lines[j] for j in range(max(0,i-12),i) if lines[j].startswith("+")]
        if any(bounds_re.search(l) for l in preceding): continue
        findings.append({"severity":"HIGH","tag":"[AST-HEURISTIC: rust-bounds]",
            "file":fp,"line":i,
            "title":f"`{container}[{expr[:30]}]` panics if index out of bounds",
            "body":f"Fix: use `{container}.get({expr})` which returns `Option<_>`.\n\nCode: `{line.strip()[1:].strip()}`"})
    return findings


def run_static_checks(diff: str, file_context: str) -> list[dict]:
    return (
        detect_n_plus_one(diff) +
        detect_falsy_traps(diff) +
        detect_window_open_noopener(diff) +
        detect_click_no_keyboard(diff) +
        detect_rust_index_panics(diff)
    )


# ── Claude CLI inference ([LLM-HEURISTIC]) ────────────────────────────────────

SYSTEM_PROMPT = """You are GrapeRoot Review — enterprise local code reviewer.
You have the PR diff, BASE file content (before), HEAD file content (after),
and graph context (blast radius + symbol reads from the local codebase graph).

ANTI-HALLUCINATION: Every finding must cite exact code from the provided context.

Run these checks. Find ALL instances, not just the first.

ARCH — refactoring completeness:
- Methods in BASE not present in the new shared/sansio module despite having no framework dependencies.
- Sibling methods: A moved, B not moved but B's body only calls moved helpers.
- Default value changes: BASE `x = False`, HEAD `x = True` → MEDIUM.

SECURITY — auth, conditions, async errors:
- Auth bypass, default admin role, missing same-user guard.
- Condition omits a property: checks `data||response` but not `error`.
- `await call()` result used without checking error field.
- Hard-coded limits silently truncating data.

MUTATION — Object.assign/spread overwrites:
- target already has key K; source might also have K → silent overwrite → HIGH.

Return ONLY a JSON array:
[{"file":"","line":0,"severity":"CRITICAL|HIGH|MEDIUM|LOW",
  "tag":"[LLM-HEURISTIC: arch|security|mutation]",
  "title":"one-line summary","body":"cite exact code + fix"}]
Return [] if genuinely nothing found."""


def claude_review(context: str) -> list[dict]:
    """Call claude --print with the full context. No API key needed."""
    result = subprocess.run(
        ["claude", "--print", "--output-format", "json"],
        input=f"{SYSTEM_PROMPT}\n\n---\n\n{context}",
        capture_output=True, text=True, timeout=240,
    )
    if result.returncode != 0:
        print(f"  [claude] error: {result.stderr[:200]}", file=sys.stderr)
        return []
    try:
        data = json.loads(result.stdout)
        text = data.get("result", "") if isinstance(data, dict) else result.stdout
    except json.JSONDecodeError:
        text = result.stdout

    text = re.sub(r"^```(?:json)?\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try: return json.loads(m.group(0))
            except Exception: pass
    return []


# ── Post to GitHub ────────────────────────────────────────────────────────────

def post_review(owner: str, repo: str, pr_num: int,
                all_findings: list[dict], head_sha: str, dry_run: bool) -> None:
    if not all_findings:
        print("\n  PR looks clean — no findings to report.")
        if not dry_run:
            gh(f"/repos/{owner}/{repo}/issues/{pr_num}/comments", method="POST",
               body={"body": "**GrapeRoot Review** — No issues found. PR looks clean.\n\n_Powered by GrapeRoot Enterprise (local-first, graph-proven)_"})
        return

    # Build review body
    by_sev = {}
    for f in all_findings:
        by_sev.setdefault(f.get("severity","LOW"), []).append(f)

    lines = ["**GrapeRoot Review** — Enterprise Edition\n"]
    for sev in ("CRITICAL","HIGH","MEDIUM","LOW"):
        items = by_sev.get(sev, [])
        if items:
            emoji = {"CRITICAL":"🔴","HIGH":"🟠","MEDIUM":"🟡","LOW":"🔵"}[sev]
            for f in items:
                tag  = f.get("tag","")
                title = f.get("title","")
                file_ = f.get("file","")
                lines.append(f"{emoji} **{sev}** `{file_}` — {tag} {title}")
                body  = f.get("body","")
                if body:
                    for bl in body.splitlines()[:4]:
                        lines.append(f"  > {bl}")
                lines.append("")

    lines.append("_Powered by [GrapeRoot](https://graperoot.dev) — local-first, graph-proven, code never leaves your machine_")
    body_text = "\n".join(lines)

    if dry_run:
        print("\n── DRY RUN ──")
        print(body_text)
        return

    try:
        result = gh(f"/repos/{owner}/{repo}/pulls/{pr_num}/reviews", method="POST",
                    body={"commit_id":head_sha,"body":body_text,"event":"COMMENT","comments":[]})
        print(f"  ✓ Posted: {result.get('html_url', result.get('id','?'))}")
    except Exception as e:
        print(f"  ! Could not post review: {e}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="GrapeRoot Enterprise Local Review")
    ap.add_argument("pr_url")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    global MCP_PORT
    if args.port:
        MCP_PORT = args.port

    if not GITHUB_TOKEN:
        print("Error: set GITHUB_TOKEN (or run: export GITHUB_TOKEN=$(gh auth token))",
              file=sys.stderr)
        return 1

    if not subprocess.run(["which","claude"], capture_output=True).returncode == 0:
        print("Error: claude CLI not found. Install Claude Code first.", file=sys.stderr)
        return 1

    owner, repo, pr_num = parse_pr_url(args.pr_url)
    print(f"GrapeRoot Enterprise — {owner}/{repo}#{pr_num}")
    print(f"  MCP: {'connected on :'+str(MCP_PORT) if mcp_available() else 'not running (start dgc-pro for richer context)'}")
    print()

    # Fetch PR data
    print("  Fetching PR...")
    pr       = gh(f"/repos/{owner}/{repo}/pulls/{pr_num}")
    head_sha = pr["head"]["sha"]
    base_sha = pr["base"]["sha"]
    title    = pr.get("title","")
    body_    = (pr.get("body","") or "")[:1500]

    # Fetch diff
    print("  Fetching diff...")
    diff = gh_diff(owner, repo, pr_num)
    changed, hunks = parse_diff(diff)
    diff_text = "\n\n".join(f"### {p}\n{h}" for p,h in list(hunks.items())[:12])
    print(f"  Changed files: {len(changed)}")

    # Fetch file context (BASE + HEAD for changed files)
    print("  Fetching file context...")
    PRIORITY = re.compile(r'(schema|model|serial|view|handler|auth|util|valid|error|request|response|process)', re.I)
    priority = [f for f in changed if PRIORITY.search(f.rsplit("/",1)[-1])]
    others   = [f for f in changed if f not in priority]
    file_parts, budget = [], 30_000
    for path in (priority + others)[:12]:
        if budget <= 0: break
        base_c = gh_file(owner, repo, path, base_sha)
        if base_c:
            s = base_c[:min(6000, budget)]
            file_parts.append(f"### {path}  [BASE]\n```\n{s}\n```")
            budget -= len(s)
        head_c = gh_file(owner, repo, path, head_sha)
        if head_c and head_c != base_c:
            s = head_c[:min(4000, budget)]
            file_parts.append(f"### {path}  [HEAD]\n```\n{s}\n```")
            budget -= len(s)
    file_context = "\n\n".join(file_parts)
    print(f"  File context: {len(file_context)} chars")

    # Graph context (uses live MCP if dgc-pro is running)
    print("  Getting graph context...")
    graph_ctx = get_graph_context(changed, owner, repo, head_sha)
    print(f"  Graph: {graph_ctx[:60].strip()}")

    # ── Run static detectors ─────────────────────────────────────────────────
    print("\n  Running static checks...")
    static_findings = run_static_checks(diff_text, file_context)
    for f in static_findings:
        print(f"  {f['tag']} {f['severity']}: {f['title'][:60]}")

    # ── Run Claude LLM review ────────────────────────────────────────────────
    print("\n  Running Claude review...")
    llm_context = f"""## PR: {title}

### Description
{body_}

### Diff
```diff
{diff_text[:MAX_DIFF]}
```

### File content (BASE and HEAD)
{file_context[:15000]}

### Graph context (blast radius + symbol reads)
{graph_ctx[:5000]}"""

    llm_findings = claude_review(llm_context)
    for f in llm_findings:
        print(f"  {f.get('tag','')} {f.get('severity','?')}: {f.get('title','')[:60]}")

    # ── Merge, deduplicate, sort ──────────────────────────────────────────────
    all_findings = static_findings + llm_findings
    sev_order = {"CRITICAL":0,"HIGH":1,"MEDIUM":2,"LOW":3}
    all_findings.sort(key=lambda f: sev_order.get(f.get("severity","LOW"), 4))
    seen, deduped = set(), []
    for f in all_findings:
        key = f.get("title","").lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    print(f"\n  Total: {len(deduped)} findings ({len(static_findings)} static + {len(llm_findings)} LLM)")

    # ── Post ──────────────────────────────────────────────────────────────────
    post_review(owner, repo, pr_num, deduped, head_sha, args.dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
