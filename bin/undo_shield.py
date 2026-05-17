#!/usr/bin/env python3
"""GrapeRoot Undo Shield — PreToolUse hook.

Intercepts destructive Claude Code tool calls and warns before touching
files that have received high session attention.

Set DG_UNDO_SHIELD=0 to disable.

Exit codes (Claude Code hook protocol):
  0  — allow silently
  1  — warn Claude; Claude should ask user to confirm before proceeding
  2  — hard block shown directly to user (reserved for rm -rf of high-attention files)
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

# ── Bypass ────────────────────────────────────────────────────────────────────
if os.environ.get("DG_UNDO_SHIELD", "1") == "0":
    sys.exit(0)

# ── Parse hook payload ────────────────────────────────────────────────────────
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool       = payload.get("tool_name", "")
tool_input = payload.get("tool_input") or {}

# ── Destructive tool detection ────────────────────────────────────────────────
WRITE_TOOLS = {"Write", "NotebookEdit"}
EDIT_TOOL   = "Edit"   # partial overwrite — lower risk, still worth checking
BASH_TOOL   = "Bash"

# Patterns that are truly destructive (hard block candidates)
HARD_BASH = re.compile(
    r"(?:^|[;&|\n]\s*)rm\s+(?:-[rRf]+\s+){1,3}\S+"   # rm -rf / rm -r
    r"|(?:^|[;&|\n]\s*)find\s+.+?-delete"             # find . -delete
    r"|(?:^|[;&|\n]\s*):\s*>\s*\S+"                   # : > file  (truncate)
    r"|(?:^|[;&|\n]\s*)dd\s+if=/dev/zero"             # zero-fill
    r"|(?:^|[;&|\n]\s*)mkfs\b"                        # format
    r"|(?:^|[;&|\n]\s*)shred\s",
    re.IGNORECASE | re.MULTILINE,
)
# Patterns that overwrite but aren't rm-level (soft warn)
SOFT_BASH = re.compile(
    r"(?<![>])>\s*(?!>)\S+"                            # single > redirect (overwrite)
    r"|(?:^|[;&|\n]\s*)rm\s+(?!-[rRf])\S+",           # rm without -r/-f
    re.IGNORECASE | re.MULTILINE,
)

is_hard  = False
is_soft  = False
op_label = ""

if tool in WRITE_TOOLS:
    is_soft  = True
    op_label = f"{tool}"
elif tool == EDIT_TOOL:
    is_soft  = True
    op_label = "Edit"
elif tool == BASH_TOOL:
    cmd = tool_input.get("command", "")
    if HARD_BASH.search(cmd):
        is_hard  = True
        op_label = f"bash: {cmd[:120].strip()}"
    elif SOFT_BASH.search(cmd):
        is_soft  = True
        op_label = f"bash: {cmd[:120].strip()}"

if not (is_hard or is_soft):
    sys.exit(0)

# ── Collect target paths ──────────────────────────────────────────────────────
def target_paths() -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            paths.append(v.strip())
    if tool == BASH_TOOL:
        cmd = tool_input.get("command", "")
        # extract explicit file args after rm / > redirection
        for m in re.finditer(
            r"(?:rm\s+(?:-\S+\s+)*|(?<![>])>(?!>)\s*)([^\s;&|><\"'`\\]+)", cmd
        ):
            p = m.group(1).strip()
            if p and not p.startswith("-") and "/" in p:
                paths.append(p)
    return paths

paths = target_paths()
if not paths and not is_hard:
    sys.exit(0)

# ── Find action graph ─────────────────────────────────────────────────────────
def find_data_dir() -> Path | None:
    # 1. Env var set by launcher (most reliable)
    env = os.environ.get("DG_DATA_DIR", "")
    if env:
        return Path(env)
    # 2. Walk up from each target path
    search_roots = [Path(p).expanduser().resolve() for p in paths]
    search_roots.append(Path.cwd())
    for root in search_roots:
        for parent in [root, *root.parents]:
            candidate = parent / ".dual-graph"
            if (candidate / "chat_action_graph.json").exists():
                return candidate
    return None

data_dir = find_data_dir()
if not data_dir:
    sys.exit(0)

project_root = data_dir.parent

# ── Load action graph ─────────────────────────────────────────────────────────
ag_path = data_dir / "chat_action_graph.json"
try:
    ag = json.loads(ag_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)

# ── Build attention map with recency weighting ────────────────────────────────
# Score = sum of (kind_weight × recency_decay) per file
KIND_WEIGHT = {"read": 2, "read_cache_hit": 1, "register_edit": 8, "edit_observation": 5}
now_ts = int(datetime.now(timezone.utc).timestamp())

attention: dict[str, dict] = {}  # relative_path → {score, reads, edits, last_ts}

for action in ag.get("actions", []):
    kind    = action.get("kind", "")
    weight  = KIND_WEIGHT.get(kind, 0)
    if weight == 0:
        continue
    f = (action.get("payload") or {}).get("file", "")
    if not f:
        continue
    age_sec = max(0, now_ts - int(action.get("ts", 0) or 0))
    decay   = 2.0 ** (-age_sec / 3600.0)  # half-life 1 hour
    rec = attention.setdefault(f, {"score": 0.0, "reads": 0, "edits": 0, "last_ts": 0})
    rec["score"]  += weight * decay
    if kind in {"read", "read_cache_hit"}:
        rec["reads"] += 1
    elif kind in {"register_edit", "edit_observation"}:
        rec["edits"] += 1
    rec["last_ts"] = max(rec["last_ts"], int(action.get("ts", 0) or 0))

# ── Match target paths against attention map ──────────────────────────────────
def normalize(raw: str) -> Path:
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return Path(raw)

at_risk: list[dict] = []
for raw_path in paths:
    abs_target = normalize(raw_path)
    for rel_path, rec in attention.items():
        # Try absolute match via project root
        try:
            abs_graph = (project_root / rel_path).resolve()
        except Exception:
            abs_graph = Path(rel_path)
        if abs_target == abs_graph:
            if rec["score"] >= 2.0:
                at_risk.append({"path": rel_path, **rec})
            break

if not at_risk:
    sys.exit(0)

# Sort by score descending
at_risk.sort(key=lambda x: -x["score"])
top = at_risk[:4]

# ── Determine exit severity ───────────────────────────────────────────────────
max_edits = max(r["edits"] for r in top)
exit_code = 2 if (is_hard and max_edits >= 2) else 1

# ── Build warning message ─────────────────────────────────────────────────────
sev = "HARD BLOCK" if exit_code == 2 else "WARNING"
lines = [
    f"GrapeRoot Undo Shield [{sev}]",
    f"Operation: {op_label}",
    "",
    "The following high-attention files will be affected:",
    "",
]
for r in top:
    parts = []
    if r["reads"]:  parts.append(f"read {r['reads']}×")
    if r["edits"]:  parts.append(f"edited {r['edits']}×")
    age_min = round((now_ts - r["last_ts"]) / 60) if r["last_ts"] else "?"
    lines.append(f"  {r['path']}  [{', '.join(parts)}, last touched {age_min} min ago]")

lines += [
    "",
    "This operation cannot be undone.",
]

if exit_code == 2:
    lines.append("Please type 'yes, proceed' to confirm, or rephrase your request.")
else:
    lines.append("Please confirm with the user before proceeding.")

print("\n".join(lines))
sys.exit(exit_code)
