#!/usr/bin/env python3
"""GrapeRoot subagent bridge — read-only graph access without MCP.

Usage (from any path, including worktrees):
    python3 <project>/.claude/../bin/graph_query.py read "src/auth.ts::handleLogin"
    python3 <project>/bin/graph_query.py read "src/db.ts"
    python3 <project>/bin/graph_query.py symbols "src/auth.ts"
    python3 <project>/bin/graph_query.py neighbors "src/auth.ts"
    python3 <project>/bin/graph_query.py grep "handleLogin"

Auto-discovers the graph store (.dual-graph-pro/) by walking up from CWD or
from this script's location. Zero dependencies beyond Python 3.8+ stdlib.

This is READ-ONLY. It never modifies the graph store.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _find_store() -> Path | None:
    """Walk up from CWD, script location, and git main worktree to find .dual-graph-pro/."""
    candidates = [Path.cwd()]
    script_dir = Path(__file__).resolve().parent.parent
    candidates.append(script_dir)

    env_root = os.environ.get("GRAPEROOT_PROJECT_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root))

    # If in a git worktree, find the main worktree (original project root)
    import subprocess
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5, cwd=str(Path.cwd()),
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    wt_path = Path(line[len("worktree "):])
                    if wt_path.is_dir():
                        candidates.append(wt_path)
                    break  # first entry is always the main worktree
    except Exception:
        pass

    for start in candidates:
        p = start.resolve()
        for _ in range(20):
            store = p / ".dual-graph-pro"
            if store.is_dir() and (store / "info_graph.json").exists():
                return store
            parent = p.parent
            if parent == p:
                break
            p = parent
    return None


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_graph(store: Path) -> dict:
    return _load_json(store / "info_graph.json")


def _load_symbols(store: Path) -> dict:
    return _load_json(store / "symbol_index.json")


def _project_root(store: Path) -> Path:
    """Derive project root from graph's stored root or store parent."""
    graph = _load_graph(store)
    stored = graph.get("root", "")
    if stored:
        root_path = Path(stored.split("#")[0]) if "#" in stored else Path(stored)
        if root_path.is_dir():
            return root_path
    return store.parent


def cmd_read(store: Path, file_arg: str, max_chars: int = 20000) -> None:
    """Read a file or file::symbol from the project, guided by the graph."""
    project_root = _project_root(store)
    sym_index = _load_symbols(store)

    file_path = file_arg
    symbol_name = None
    if "::" in file_arg:
        file_path, symbol_name = file_arg.split("::", 1)

    target = (project_root / file_path).resolve()
    if not target.exists():
        graph = _load_graph(store)
        content = None
        for node in graph.get("nodes", []):
            if node.get("path") == file_path and node.get("content"):
                content = node["content"]
                break
        if not content:
            print(json.dumps({"ok": False, "error": f"file not found: {file_path}"}))
            sys.exit(1)
        text = content
    else:
        text = target.read_text(encoding="utf-8", errors="ignore")

    if symbol_name:
        sym_key = file_arg
        meta = sym_index.get(sym_key)
        if meta:
            lines = text.splitlines()
            start = max(0, int(meta.get("line_start", 1)) - 1)
            end = int(meta.get("line_end", len(lines)))
            excerpt = "\n".join(lines[start:end])
            result = {
                "ok": True,
                "file": file_arg,
                "mode": "symbol_excerpt",
                "line_start": start + 1,
                "line_end": end,
                "content": excerpt[:max_chars],
                "chars": len(excerpt),
            }
            print(json.dumps(result))
            return

    text = text[:max_chars]
    result = {
        "ok": True,
        "file": file_arg,
        "mode": "full" if len(text) < max_chars else "truncated",
        "content": text,
        "chars": len(text),
    }
    print(json.dumps(result))


def cmd_symbols(store: Path, file_arg: str) -> None:
    """List all symbols in a file from the symbol index."""
    sym_index = _load_symbols(store)
    matches = []
    for sym_id, meta in sym_index.items():
        if sym_id.startswith(file_arg + "::") or meta.get("path") == file_arg:
            matches.append({
                "id": sym_id,
                "line_start": meta.get("line_start"),
                "line_end": meta.get("line_end"),
                "confidence": meta.get("confidence", ""),
            })
    matches.sort(key=lambda x: x.get("line_start", 0))
    print(json.dumps({"ok": True, "file": file_arg, "symbols": matches, "count": len(matches)}))


def cmd_neighbors(store: Path, file_arg: str) -> None:
    """Find files connected to this file via graph edges."""
    graph = _load_graph(store)
    edges = graph.get("edges", [])
    neighbors = set()
    for edge in edges:
        src = edge.get("source", edge.get("from", ""))
        tgt = edge.get("target", edge.get("to", ""))
        if src == file_arg:
            neighbors.add(tgt)
        elif tgt == file_arg:
            neighbors.add(src)
    file_base = file_arg.split("::")[0] if "::" in file_arg else file_arg
    for edge in edges:
        src = edge.get("source", edge.get("from", ""))
        tgt = edge.get("target", edge.get("to", ""))
        if src.startswith(file_base + "::"):
            neighbors.add(tgt)
        elif tgt.startswith(file_base + "::"):
            neighbors.add(src)
    neighbors.discard(file_arg)
    result = sorted(neighbors)
    print(json.dumps({"ok": True, "file": file_arg, "neighbors": result[:30], "count": len(result)}))


def cmd_grep(store: Path, pattern: str, max_hits: int = 30) -> None:
    """Grep across project files using the graph's file index for paths."""
    import re
    import subprocess

    project_root = _project_root(store)

    try:
        proc = subprocess.run(
            ["rg", "-n", "-S", "--max-count", str(max_hits), pattern, "."],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        proc = subprocess.run(
            ["grep", "-rn", pattern, "."],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=15,
        )

    hits = []
    for line in (proc.stdout or "").splitlines():
        if line.startswith("--"):
            continue
        parts = line.split(":", 2)
        if len(parts) == 3:
            hits.append({"file": parts[0], "line": parts[1], "text": parts[2]})
        if len(hits) >= max_hits:
            break

    print(json.dumps({"ok": True, "pattern": pattern, "hits": hits, "count": len(hits)}))


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: graph_query.py <command> <arg> [options]", file=sys.stderr)
        print("Commands: read, symbols, neighbors, grep", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    arg = sys.argv[2]

    store = _find_store()
    if not store:
        print(json.dumps({
            "ok": False,
            "error": "Could not find .dual-graph-pro/ graph store. Walk-up search from CWD and script location failed.",
            "hint": "Set GRAPEROOT_PROJECT_ROOT env var to the project root.",
        }))
        sys.exit(1)

    max_chars = 20000
    if len(sys.argv) > 3:
        try:
            max_chars = int(sys.argv[3])
        except ValueError:
            pass

    if command == "read":
        cmd_read(store, arg, max_chars)
    elif command == "symbols":
        cmd_symbols(store, arg)
    elif command == "neighbors":
        cmd_neighbors(store, arg)
    elif command == "grep":
        max_hits = max_chars if command == "grep" else 30
        cmd_grep(store, arg, max_hits=30)
    else:
        print(json.dumps({"ok": False, "error": f"Unknown command: {command}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
