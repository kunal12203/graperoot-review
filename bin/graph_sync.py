#!/usr/bin/env python3
"""GrapeRoot Graph Sync — PostToolUse hook.

Automatically records edits in the chat_action_graph.json after Write/Edit
so the graph stays fresh without Claude needing to call graph_register_edit.

Runs fire-and-forget — never blocks, never fails loudly.
Set DG_GRAPH_GATE=0 to disable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if os.environ.get("DG_GRAPH_GATE", "1") == "0":
    sys.exit(0)

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = payload.get("tool_name", "")
tool_input = payload.get("tool_input") or {}

file_path = tool_input.get("file_path", "")
if not file_path:
    sys.exit(0)


def _find_data_dir() -> Path | None:
    """Locate .dual-graph or .dual-graph-pro data directory."""
    env = os.environ.get("DG_DATA_DIR")
    if env:
        p = Path(env)
        if p.is_dir():
            return p
        return None
    cwd = Path.cwd()
    for candidate in [cwd / ".dual-graph-pro", cwd / ".dual-graph"]:
        if candidate.is_dir():
            return candidate
    for parent in cwd.parents:
        for name in (".dual-graph-pro", ".dual-graph"):
            candidate = parent / name
            if candidate.is_dir():
                return candidate
    return None


def _register_edit(file_path: str, data_dir: Path) -> None:
    """Directly append to chat_action_graph.json — no server needed."""
    graph_file = data_dir / "chat_action_graph.json"

    try:
        if graph_file.exists():
            g = json.loads(graph_file.read_text(encoding="utf-8"))
        else:
            g = {"actions": [], "files": {}}
    except (json.JSONDecodeError, OSError):
        g = {"actions": [], "files": {}}

    # Make path relative to project root
    project_root = data_dir.parent
    try:
        rel_path = str(Path(file_path).resolve().relative_to(project_root.resolve()))
    except ValueError:
        rel_path = file_path

    # Append action
    actions = g.setdefault("actions", [])
    actions.append({
        "type": "edit_auto",
        "ts": time.time(),
        "files": [rel_path],
        "source": "graph_sync_hook",
    })

    # Keep last 200 actions
    if len(actions) > 200:
        g["actions"] = actions[-200:]

    # Update file metadata
    files_meta = g.setdefault("files", {})
    meta = files_meta.setdefault(rel_path, {})
    meta["last_edited"] = time.time()
    meta["edit_count"] = int(meta.get("edit_count", 0)) + 1

    # Mark symbol index stale for this file
    sym_index_file = data_dir / "symbol_index.json"
    if sym_index_file.exists():
        try:
            si = json.loads(sym_index_file.read_text(encoding="utf-8"))
            stale_keys = [k for k in si if k.startswith(rel_path + "::")]
            if stale_keys:
                for k in stale_keys:
                    si[k]["stale"] = True
                sym_index_file.write_text(json.dumps(si), encoding="utf-8")
        except Exception:
            pass

    try:
        graph_file.write_text(json.dumps(g, indent=2), encoding="utf-8")
    except OSError:
        pass


data_dir = _find_data_dir()
if data_dir:
    _register_edit(file_path, data_dir)

sys.exit(0)
