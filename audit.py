#!/usr/bin/env python3
"""GrapeRoot Vibe Code Auditor.

Runs 7 health checks against info_graph.json + source files:
  dead exports · test coverage · circular deps · copy-paste ·
  DB calls in routes · orphaned TODOs · missing error handling

Usage:
    dgc audit /path/to/project            # text report + saves JSON
    dgc audit /path/to/project --fix      # text report + launches dgc with audit context
    python3 audit.py --root /path [--json] [--no-color]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# ── ANSI colours ──────────────────────────────────────────────────────────────
def _c(code: str, text: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    codes = {
        "red": "\033[91m", "yellow": "\033[93m", "green": "\033[92m",
        "cyan": "\033[96m", "bold": "\033[1m", "dim": "\033[2m",
        "orange": "\033[33m", "reset": "\033[0m",
    }
    return f"{codes.get(code, '')}{text}{codes['reset']}"

COLOR = sys.stdout.isatty()

CODE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java",
             ".kt", ".cs", ".rb", ".php", ".c", ".cpp", ".cc", ".swift",
             ".dart", ".ex", ".exs", ".scala", ".lua", ".jl", ".hs", ".zig"}

# ── Graph loading ─────────────────────────────────────────────────────────────
def _load_graph(root: Path) -> dict:
    p = root / ".dual-graph" / "info_graph.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}

def _read(root: Path, file_id: str) -> str:
    p = root / file_id
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

# ── Check 1: Dead exports ─────────────────────────────────────────────────────
def check_dead_exports(graph: dict) -> list[dict]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    imported: set[str] = set()
    for e in edges:
        if e.get("rel") in {"imports", "references", "contains"}:
            imported.add(e.get("to", ""))

    dead = []
    for n in nodes:
        if n.get("kind") != "symbol" or not n.get("exported"):
            continue
        if n["id"] not in imported:
            ext = Path(n.get("file", "")).suffix
            if ext in CODE_EXTS:
                dead.append({
                    "file":   n.get("file", ""),
                    "symbol": n.get("name", ""),
                    "line":   n.get("line_start", 0),
                    "type":   n.get("symbol_type", ""),
                })
    dead.sort(key=lambda x: x["file"])
    return dead[:50]

# ── Check 2: Test coverage ────────────────────────────────────────────────────
TEST_PAT = re.compile(
    r"(?:\.test\.|\.spec\.|_test\.|test_|spec_|__tests__/|/tests?/)", re.IGNORECASE
)

def check_test_coverage(graph: dict, root: Path) -> dict:
    nodes = graph.get("nodes", [])
    file_nodes = [n for n in nodes if n.get("kind") == "file"]

    code_files  = [n["id"] for n in file_nodes
                   if Path(n["id"]).suffix in CODE_EXTS and not TEST_PAT.search(n["id"])]
    test_files  = [n["id"] for n in file_nodes if TEST_PAT.search(n["id"])]

    # build stem → True for tested modules
    tested: set[str] = set()
    for t in test_files:
        stem = Path(t).stem
        stem = re.sub(r"(?:\.test|\.spec|_test|_spec)$", "", stem, flags=re.IGNORECASE)
        stem = re.sub(r"^(?:test_|spec_)", "", stem, flags=re.IGNORECASE)
        tested.add(stem.lower())

    untested = [f for f in code_files
                if Path(f).stem.lower() not in tested
                and Path(f).stem.lower() not in {"index", "main", "init", "app"}]

    total   = len(code_files)
    covered = total - len(untested)
    pct     = round(covered / total * 100) if total else 100

    # Sort untested by file size desc (bigger = more important to cover)
    untested_with_size = []
    for f in untested[:20]:
        size = 0
        try:
            size = (root / f).stat().st_size
        except Exception:
            pass
        untested_with_size.append((f, size))
    untested_with_size.sort(key=lambda x: -x[1])

    return {
        "total_code_files": total,
        "tested_files":     covered,
        "coverage_pct":     pct,
        "untested":         [f for f, _ in untested_with_size[:15]],
    }

# ── Check 3: Circular deps ────────────────────────────────────────────────────
def check_circular_deps(graph: dict) -> list[list[str]]:
    edges = graph.get("edges", [])
    adj: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e.get("rel") in {"imports", "references"}:
            s, d = e.get("from", ""), e.get("to", "")
            if s and d and s != d:
                adj[s].add(d)

    cycles: list[list[str]] = []
    seen: set[str] = set()

    def dfs(node: str, path: list[str], on_stack: set[str]) -> None:
        if len(cycles) >= 10:
            return
        for nb in adj.get(node, set()):
            if nb in on_stack:
                idx  = path.index(nb)
                cyc  = path[idx:]
                fkey = frozenset(cyc)
                if not any(frozenset(c) == fkey for c in cycles):
                    cycles.append(cyc)
            elif nb not in seen:
                on_stack.add(nb)
                dfs(nb, path + [nb], on_stack)
                on_stack.discard(nb)

    for n in list(adj)[:300]:
        if n not in seen:
            dfs(n, [n], {n})
            seen.add(n)

    return cycles

# ── Check 4: Copy-paste ───────────────────────────────────────────────────────
def check_copy_paste(graph: dict) -> list[dict]:
    nodes    = graph.get("nodes", [])
    by_hash: dict[str, list[dict]] = defaultdict(list)

    for n in nodes:
        if n.get("kind") != "symbol":
            continue
        bh   = n.get("body_hash", "")
        span = (n.get("line_end") or 0) - (n.get("line_start") or 0)
        if bh and span >= 10 and Path(n.get("file", "")).suffix in CODE_EXTS:
            by_hash[bh].append(n)

    dups = []
    for _, group in by_hash.items():
        if len(group) >= 2:
            dups.append({
                "copies":  len(group),
                "lines":   (group[0].get("line_end") or 0) - (group[0].get("line_start") or 0),
                "symbols": [{"file": n.get("file",""), "name": n.get("name",""),
                              "line": n.get("line_start", 0)} for n in group[:5]],
            })

    dups.sort(key=lambda x: -(x["copies"] * x["lines"]))
    return dups[:12]

# ── Check 5: DB calls in route handlers ──────────────────────────────────────
DB_RE    = re.compile(
    r"(?:db\.|prisma\.|mongoose\.|sqlalchemy\.|session\.query"
    r"|\.query\s*\(|\.execute\s*\(|\.find(?:One|All|Many)?\s*\("
    r"|\.save\s*\(|\.create\s*\(|cursor\.execute)",
    re.IGNORECASE,
)
ROUTE_RE = re.compile(r"(?:routes?|controllers?|handlers?|views?|api)", re.IGNORECASE)

def check_db_in_routes(graph: dict, root: Path) -> list[dict]:
    issues = []
    for n in graph.get("nodes", []):
        if n.get("kind") != "file":
            continue
        fid = n["id"]
        if not ROUTE_RE.search(fid) or Path(fid).suffix not in CODE_EXTS:
            continue
        content = _read(root, fid)
        hits    = DB_RE.findall(content)
        if hits:
            lines = [i + 1 for i, l in enumerate(content.splitlines())
                     if DB_RE.search(l)]
            issues.append({"file": fid, "count": len(hits),
                           "lines": lines[:5], "patterns": list(set(hits))[:3]})
    issues.sort(key=lambda x: -x["count"])
    return issues[:10]

# ── Check 6: Orphaned TODOs ───────────────────────────────────────────────────
TODO_RE = re.compile(r"(?:TODO|FIXME|HACK|XXX)\s*(?:\([^)]*\))?[:：]?\s*(.{3,})", re.IGNORECASE)

def check_todos(graph: dict, root: Path) -> list[dict]:
    results = []
    for n in graph.get("nodes", []):
        if n.get("kind") != "file" or Path(n["id"]).suffix not in CODE_EXTS:
            continue
        content = _read(root, n["id"])
        for i, line in enumerate(content.splitlines(), 1):
            m = TODO_RE.search(line)
            if m:
                results.append({"file": n["id"], "line": i,
                                 "text": m.group(0)[:90].strip()})
    return results[:40]

# ── Check 7: Missing error handling ──────────────────────────────────────────
EXTERNAL_RE = re.compile(
    r"(?:fetch\s*\(|axios\.[a-z]+\s*\(|http(?:s)?\.request\s*\("
    r"|requests\.[a-z]+\s*\(|urllib\.request\.|grpc\.|\.rpc\s*\()",
    re.IGNORECASE,
)
ERR_RE = re.compile(r"\btry\b|\bcatch\b|\bexcept\b|\brescue\b", re.IGNORECASE)

def check_missing_error_handling(graph: dict, root: Path) -> list[dict]:
    issues  = []
    cache: dict[str, list[str]] = {}
    for n in graph.get("nodes", []):
        if n.get("kind") != "symbol" or Path(n.get("file","")).suffix not in CODE_EXTS:
            continue
        fid = n.get("file", "")
        if fid not in cache:
            cache[fid] = _read(root, fid).splitlines()
        ls = (n.get("line_start") or 1) - 1
        le = min(n.get("line_end") or ls + 1, len(cache[fid]))
        body = "\n".join(cache[fid][ls:le])
        if EXTERNAL_RE.search(body) and not ERR_RE.search(body):
            issues.append({"file": fid, "symbol": n.get("name",""), "line": ls + 1})
        if len(issues) >= 25:
            break
    return issues

# ── Scoring ───────────────────────────────────────────────────────────────────
def debt_score(r: dict) -> int:
    score  = 0
    cov    = r.get("test_coverage", {}).get("coverage_pct", 100)
    score += max(0, (60 - cov))                          # up to 60 pts
    score += min(15, len(r.get("dead_exports",   [])) // 3)
    score += min(10, len(r.get("circular_deps",  [])) * 2)
    score += min(10, len(r.get("copy_paste",     [])) * 2)
    score += min(5,  len(r.get("db_in_routes",   [])))
    score += min(5,  len(r.get("todos",          [])) // 4)
    return min(100, score)

# ── Fix roadmap ───────────────────────────────────────────────────────────────
def fix_roadmap(r: dict) -> list[dict]:
    tasks = []
    cov = r.get("test_coverage", {}).get("coverage_pct", 100)
    if cov < 50:
        biggest = (r.get("test_coverage") or {}).get("untested", [])[:3]
        tasks.append({"week": 1, "label": "Add tests to largest untested modules",
                      "files": biggest, "why": f"Only {cov}% test coverage"})
    db = r.get("db_in_routes", [])
    if db:
        tasks.append({"week": 1, "label": "Move DB calls out of route handlers",
                      "files": [x["file"] for x in db[:3]],
                      "why": "Direct DB in routes = no separation of concerns, untestable"})
    cp = r.get("copy_paste", [])
    if cp:
        files = list({s["file"] for d in cp[:3] for s in d["symbols"]})[:4]
        tasks.append({"week": 2, "label": "Extract duplicated functions to shared util",
                      "files": files,
                      "why": f"{len(cp)} duplicate function group(s) — single bug fix = fix everywhere"})
    cycles = r.get("circular_deps", [])
    if cycles:
        tasks.append({"week": 2, "label": "Break circular imports",
                      "files": list({f for c in cycles[:2] for f in c})[:4],
                      "why": "Circular deps prevent tree-shaking and cause init-order bugs"})
    dead = r.get("dead_exports", [])
    if len(dead) >= 5:
        files = list({d["file"] for d in dead[:6]})[:4]
        tasks.append({"week": 3, "label": f"Remove {len(dead)} dead exports",
                      "files": files, "why": "Dead code increases bundle size and confuses readers"})
    return tasks

# ── Terminal report ───────────────────────────────────────────────────────────
def _debt_color(score: int) -> str:
    if score < 30:  return "green"
    if score < 60:  return "yellow"
    return "red"

def format_report(root: Path, r: dict, color: bool = True) -> str:
    graph = r.get("_graph", {})
    nodes = graph.get("nodes", [])
    fc = sum(1 for n in nodes if n.get("kind") == "file"
             and Path(n["id"]).suffix in CODE_EXTS)
    sc = sum(1 for n in nodes if n.get("kind") == "symbol")
    ds = r["debt_score"]

    label = "LOW" if ds < 30 else ("MEDIUM" if ds < 60 else "HIGH")
    sep   = "━" * 52

    def h(text: str) -> str:
        return _c("bold", text, color)
    def dim(text: str) -> str:
        return _c("dim", text, color)

    out = [
        "",
        h(f"GrapeRoot Audit — {root.name}"),
        sep,
        "",
        h("OVERVIEW"),
        f"  Code files: {fc:,}   Symbols: {sc:,}",
        f"  Vibe debt:  {_c(_debt_color(ds), f'{ds}/100  ({label})', color)}",
        "",
    ]

    # Test coverage
    cov = r.get("test_coverage", {})
    pct = cov.get("coverage_pct", "?")
    col = "green" if (isinstance(pct, int) and pct >= 70) else "yellow" if (isinstance(pct, int) and pct >= 40) else "red"
    out.append(h("TEST COVERAGE"))
    out.append(f"  {_c(col, f'{pct}%', color)}  ({cov.get('tested_files',0)}/{cov.get('total_code_files',0)} source files covered)")
    untested = cov.get("untested", [])
    if untested:
        out.append(dim("  Largest untested:"))
        for f in untested[:4]:
            out.append(dim(f"    · {f}"))
    out.append("")

    # Dead exports
    dead = r.get("dead_exports", [])
    if dead:
        out.append(h(f"DEAD EXPORTS  ({len(dead)} symbols)"))
        for d in dead[:5]:
            out.append(f"  · {d['file']}:{d['line']}  {_c('dim', d['symbol'], color)}")
        if len(dead) > 5:
            out.append(dim(f"  ... and {len(dead)-5} more"))
        out.append("")

    # Circular deps
    cycs = r.get("circular_deps", [])
    if cycs:
        out.append(h(f"CIRCULAR DEPS  ({len(cycs)} cycle(s))"))
        for c in cycs[:3]:
            out.append(f"  · {_c('orange', ' → '.join(c[:4]), color)}")
        out.append("")

    # Copy-paste
    dups = r.get("copy_paste", [])
    if dups:
        out.append(h(f"COPY-PASTE  ({len(dups)} group(s))"))
        for d in dups[:4]:
            names = "  /  ".join(f"{x['file'].split('/')[-1]}::{x['name']}" for x in d["symbols"][:2])
            out.append(f"  · {d['copies']}× duplicate, {d['lines']} lines — {_c('dim', names, color)}")
        out.append("")

    # DB in routes
    db = r.get("db_in_routes", [])
    if db:
        out.append(h(f"DB IN ROUTES  ({len(db)} file(s))"))
        for x in db[:4]:
            lines_str = ", ".join(str(l) for l in x["lines"][:3])
            out.append(f"  · {x['file']}  ({x['count']} calls, lines {lines_str})")
        out.append("")

    # TODOs
    todos = r.get("todos", [])
    if todos:
        out.append(h(f"TODO / FIXME  ({len(todos)})"))
        for t in todos[:4]:
            out.append(f"  · {t['file']}:{t['line']}  {_c('dim', t['text'][:70], color)}")
        if len(todos) > 4:
            out.append(dim(f"  ... and {len(todos)-4} more"))
        out.append("")

    # Missing error handling
    noerr = r.get("missing_error_handling", [])
    if noerr:
        out.append(h(f"NO ERROR HANDLING  ({len(noerr)} function(s))"))
        for e in noerr[:4]:
            out.append(f"  · {e['file']}:{e['line']}  {_c('dim', e['symbol']+'()', color)}")
        out.append("")

    # Fix roadmap
    tasks = fix_roadmap(r)
    if tasks:
        out += [sep, h("FIX ROADMAP"), ""]
        for t in tasks:
            out.append(_c("cyan", f"  Week {t['week']}:  {t['label']}", color))
            out.append(dim(f"           Why: {t['why']}"))
            for f in t["files"][:2]:
                out.append(dim(f"           → {f}"))
            out.append("")

    out += [sep,
            dim("  Run  dgc audit /path --fix  to launch Claude with this context pre-loaded.")]
    return "\n".join(out)

# ── Write audit context for dgc injection ────────────────────────────────────
def write_audit_context(root: Path, r: dict) -> Path:
    ds     = r["debt_score"]
    label  = "LOW" if ds < 30 else ("MEDIUM" if ds < 60 else "HIGH")
    cov    = r.get("test_coverage", {}).get("coverage_pct", "?")
    tasks  = fix_roadmap(r)

    lines  = [
        "# Audit Context (generated by dgc audit)",
        "",
        f"Vibe debt score: {ds}/100 ({label})",
        f"Test coverage: {cov}%",
        "",
    ]
    dead = r.get("dead_exports", [])
    if dead:
        lines.append(f"Dead exports: {len(dead)} — run graph_dead_exports() for full list")
    cycs = r.get("circular_deps", [])
    if cycs:
        lines.append(f"Circular deps: {len(cycs)} cycles")
    db = r.get("db_in_routes", [])
    if db:
        files_str = ", ".join(x["file"] for x in db[:3])
        lines.append(f"DB in route handlers: {files_str}")

    if tasks:
        lines += ["", "## Suggested fix order", ""]
        for t in tasks:
            lines.append(f"**Week {t['week']}:** {t['label']}")
            for f in t["files"][:2]:
                lines.append(f"  - {f}")
        lines.append("")

    lines += [
        "## Instructions",
        "Work through the fix roadmap above in order.",
        "Call graph_continue() before reading any file.",
        "Call graph_register_edit() after every change.",
    ]

    out_path = root / ".dual-graph" / "AUDIT_CONTEXT.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path

# ── Main ──────────────────────────────────────────────────────────────────────
def run_audit(root: Path) -> dict:
    graph = _load_graph(root)
    if not graph:
        print(f"No info_graph.json found in {root}/.dual-graph/", file=sys.stderr)
        print("Run `dgc .` first to build the graph, then re-run the audit.", file=sys.stderr)
        sys.exit(1)

    r: dict = {"_graph": graph}
    checks = [
        ("dead_exports",          lambda: check_dead_exports(graph)),
        ("test_coverage",         lambda: check_test_coverage(graph, root)),
        ("circular_deps",         lambda: check_circular_deps(graph)),
        ("copy_paste",            lambda: check_copy_paste(graph)),
        ("db_in_routes",          lambda: check_db_in_routes(graph, root)),
        ("todos",                 lambda: check_todos(graph, root)),
        ("missing_error_handling",lambda: check_missing_error_handling(graph, root)),
    ]
    labels = {
        "dead_exports": "dead exports",
        "test_coverage": "test coverage",
        "circular_deps": "circular deps",
        "copy_paste": "copy-paste",
        "db_in_routes": "DB in routes",
        "todos": "TODOs",
        "missing_error_handling": "error handling",
    }
    for key, fn in checks:
        print(f"  {labels[key]}...", end="  ", flush=True, file=sys.stderr)
        r[key] = fn()
        print("done", file=sys.stderr)

    r["debt_score"] = debt_score(r)
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description="GrapeRoot Vibe Code Auditor")
    ap.add_argument("--root",     default=".",   help="Project root")
    ap.add_argument("--out",      default=None,  help="JSON report output path")
    ap.add_argument("--json",     action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--fix",      action="store_true",
                    help="After report, launch dgc with audit context pre-loaded")
    args = ap.parse_args()

    root  = Path(args.root).expanduser().resolve()
    color = COLOR and not args.no_color

    print(f"\nGrapeRoot Auditor — {root.name}", file=sys.stderr)
    r = run_audit(root)

    # Save JSON
    out_path = Path(args.out) if args.out else (root / ".dual-graph" / "audit_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export = {k: v for k, v in r.items() if k != "_graph"}
    out_path.write_text(json.dumps(export, indent=2), encoding="utf-8")

    # Write AUDIT_CONTEXT.md for dgc injection
    ctx_path = write_audit_context(root, r)

    if args.json:
        print(json.dumps(export, indent=2))
    else:
        print(format_report(root, r, color=color))
        print(f"\n  JSON:    {out_path}", file=sys.stderr)
        print(f"  Context: {ctx_path}", file=sys.stderr)

    if args.fix:
        dgc_bin = Path(__file__).parent / "bin" / "dgc"
        if not dgc_bin.exists():
            dgc_bin = Path("dgc")
        print(f"\n  Launching dgc with audit context...", file=sys.stderr)
        os.execv(str(dgc_bin), [str(dgc_bin), str(root)])


if __name__ == "__main__":
    main()
