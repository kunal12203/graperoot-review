#!/usr/bin/env python3
"""Graph service for GrapeRoot Review hosted deployment.

Clones repos, builds AST graphs, caches them, and provides
blast-radius + symbol-read without needing the MCP server.

Cache layout:
    /app/graphs/{owner}/{repo}/
        info_graph.json     — the full graph
        symbol_index.json   — symbol → line range index
        last_sha            — HEAD sha when graph was last built
        built_at            — ISO timestamp of last build
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

GRAPH_CACHE_ROOT = Path(os.environ.get("GRAPH_CACHE_ROOT", "/app/graphs"))
GRAPH_MAX_AGE_S  = int(os.environ.get("GRAPH_MAX_AGE_S", 3600 * 6))  # rebuild if > 6h old
GRAPH_BUILDER    = Path(__file__).parent / "graph_builder_v6.2.py"


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_dir(owner: str, repo: str) -> Path:
    d = GRAPH_CACHE_ROOT / owner / repo
    d.mkdir(parents=True, exist_ok=True)
    return d


def _graph_path(owner: str, repo: str) -> Path:
    return _cache_dir(owner, repo) / "info_graph.json"


def _symbol_index_path(owner: str, repo: str) -> Path:
    return _cache_dir(owner, repo) / "symbol_index.json"


def _is_fresh(owner: str, repo: str, head_sha: str) -> bool:
    """Return True if cached graph exists, matches head_sha, and isn't stale."""
    gp = _graph_path(owner, repo)
    if not gp.exists():
        return False
    cd  = _cache_dir(owner, repo)
    sha_file = cd / "last_sha"
    built_at = cd / "built_at"
    if not sha_file.exists():
        return False
    if sha_file.read_text().strip() != head_sha:
        return False  # repo has new commits
    if built_at.exists():
        age = time.time() - float(built_at.read_text().strip())
        if age > GRAPH_MAX_AGE_S:
            return False
    return True


def _save_meta(owner: str, repo: str, head_sha: str) -> None:
    cd = _cache_dir(owner, repo)
    (cd / "last_sha").write_text(head_sha)
    (cd / "built_at").write_text(str(time.time()))


# ── Clone + build ──────────────────────────────────────────────────────────────

def build_graph(owner: str, repo: str, github_token: str, head_sha: str = "") -> bool:
    """Clone repo and build the graph. Returns True on success."""
    clone_dir = Path(f"/tmp/gr-clone-{owner}-{repo}")

    try:
        # Shallow clone using installation token
        clone_url = f"https://x-access-token:{github_token}@github.com/{owner}/{repo}.git"
        print(f"[graph] cloning {owner}/{repo}...", flush=True)
        t0 = time.time()

        result = subprocess.run(
            ["git", "clone", "--depth=1", "--single-branch", clone_url, str(clone_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"[graph] clone failed: {result.stderr[:200]}", flush=True)
            return False

        print(f"[graph] cloned in {time.time()-t0:.1f}s, building graph...", flush=True)
        t0 = time.time()

        graph_out = _graph_path(owner, repo)
        result = subprocess.run(
            [sys.executable, str(GRAPH_BUILDER),
             "--root", str(clone_dir),
             "--out",  str(graph_out)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            print(f"[graph] build failed: {result.stderr[:200]}", flush=True)
            return False

        # Copy symbol index to cache dir
        src_sym = clone_dir / ".dual-graph" / "symbol_index.json"
        if src_sym.exists():
            import shutil
            shutil.copy(src_sym, _symbol_index_path(owner, repo))

        _save_meta(owner, repo, head_sha)
        print(f"[graph] built in {time.time()-t0:.1f}s → {graph_out}", flush=True)
        print(f"[graph] {result.stdout.strip()}", flush=True)
        return True

    except subprocess.TimeoutExpired:
        print(f"[graph] timeout cloning/building {owner}/{repo}", flush=True)
        return False
    except Exception as e:
        print(f"[graph] error: {e}", flush=True)
        return False
    finally:
        # Clean up clone
        try:
            import shutil
            if clone_dir.exists():
                shutil.rmtree(clone_dir, ignore_errors=True)
        except Exception:
            pass


def ensure_graph(owner: str, repo: str, github_token: str, head_sha: str = "") -> bool:
    """Ensure a fresh graph exists for owner/repo. Returns True if available."""
    if _is_fresh(owner, repo, head_sha):
        print(f"[graph] cache hit for {owner}/{repo} @ {head_sha[:7]}", flush=True)
        return True
    return build_graph(owner, repo, github_token, head_sha)


# ── Graph queries (no MCP needed) ─────────────────────────────────────────────

def _load_graph(owner: str, repo: str) -> Optional[dict]:
    gp = _graph_path(owner, repo)
    if not gp.exists():
        return None
    try:
        return json.loads(gp.read_text())
    except Exception:
        return None


def _load_symbol_index(owner: str, repo: str) -> dict:
    sp = _symbol_index_path(owner, repo)
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text())
    except Exception:
        return {}


def graph_impact(owner: str, repo: str, changed_files: list[str]) -> dict:
    """Compute blast-radius for changed files using cached graph."""
    g = _load_graph(owner, repo)
    if not g:
        return {"ok": False, "reason": "no_graph"}

    edges   = g.get("edges", [])
    changed = set(changed_files)
    connected: set[str] = set()

    for edge in edges:
        frm = str(edge.get("from", ""))
        to  = str(edge.get("to", ""))
        if frm in changed:
            connected.add(to)
        if to in changed:
            connected.add(frm)

    connected -= changed  # only files NOT in the diff

    # Build a human-readable blast-radius summary
    if connected:
        summary = (
            f"**{len(connected)} file(s) import or are imported by the changed files:**\n" +
            "\n".join(f"  - {f}" for f in sorted(connected)[:10])
        )
    else:
        summary = "No other files directly import the changed files."

    # Recommend the most-connected changed files to read at symbol level
    file_degree: dict[str, int] = {}
    for edge in edges:
        for f in (edge.get("from", ""), edge.get("to", "")):
            if f in changed:
                file_degree[f] = file_degree.get(f, 0) + 1

    recommended = sorted(changed, key=lambda f: -file_degree.get(f, 0))[:4]

    return {
        "ok": True,
        "affected_files": sorted(connected),
        "recommended_reads": recommended,
        "summary": summary,
    }


def graph_read_symbol(owner: str, repo: str, file_ref: str, max_chars: int = 2000) -> str:
    """Read a file or file::symbol from the cached graph's source repo.

    Since we don't keep the clone, we reconstruct content from the graph node's
    line numbers and the graph's stored source snippets (if any), or fall back
    to returning the symbol metadata.
    """
    g = _load_graph(owner, repo)
    if not g:
        return ""

    # Parse file::symbol notation
    if "::" in file_ref:
        file_path, symbol = file_ref.split("::", 1)
    else:
        file_path, symbol = file_ref, None

    nodes = g.get("nodes", [])

    if symbol:
        # Find the symbol node
        for node in nodes:
            if node.get("kind") == "symbol" and node.get("path") == file_path:
                name = node.get("name", "")
                if name == symbol or name.endswith(f".{symbol}"):
                    lines = node.get("line_start", 0)
                    linee = node.get("line_end", 0)
                    sig   = node.get("signature", node.get("body_hash", ""))[:200]
                    return f"// {file_path}::{name} (lines {lines}-{linee})\n{sig}"

    # Fall back to file-level summary
    file_imports = [e.get("to", "") for e in g.get("edges", []) if e.get("from") == file_path]
    file_symbols = [n.get("name", "") for n in nodes
                    if n.get("kind") == "symbol" and n.get("path") == file_path]

    parts = [f"// {file_path}"]
    if file_imports:
        parts.append(f"// imports: {', '.join(file_imports[:8])}")
    if file_symbols:
        parts.append(f"// exports: {', '.join(file_symbols[:12])}")

    return "\n".join(parts)[:max_chars]


def has_graph(owner: str, repo: str) -> bool:
    return _graph_path(owner, repo).exists()


def graph_summary(owner: str, repo: str) -> dict:
    """Return basic stats about the cached graph."""
    gp = _graph_path(owner, repo)
    if not gp.exists():
        return {"exists": False}
    try:
        g    = json.loads(gp.read_text())
        cd   = _cache_dir(owner, repo)
        sha  = (cd / "last_sha").read_text().strip() if (cd / "last_sha").exists() else ""
        built = (cd / "built_at").read_text().strip() if (cd / "built_at").exists() else ""
        return {
            "exists":       True,
            "file_count":   g.get("file_count", 0),
            "symbol_count": g.get("symbol_count", 0),
            "edge_count":   g.get("edge_count", 0),
            "last_sha":     sha,
            "built_at":     built,
        }
    except Exception as e:
        return {"exists": True, "error": str(e)}
