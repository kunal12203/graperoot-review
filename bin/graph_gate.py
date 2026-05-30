#!/usr/bin/env python3
"""GrapeRoot Graph Gate — PreToolUse hook.

Blocks native file EXPLORATION (cat, grep, find, ls) — Claude must use
graph_read and fallback_rg instead.

DOES NOT BLOCK:
  - Read tool (Claude Code requires Read before Edit/Write — blocking creates deadlock)
  - Edit commands (sed -i, awk -i inplace)
  - Non-exploration Bash (git, npm, python, curl, etc.)
  - Files outside the project directory

Set DG_GRAPH_GATE=0 to disable entirely.
Set DG_GRAPH_GATE=warn to allow but print warnings.

Exit codes (Claude Code hook protocol):
  0  — allow silently
  2  — hard block
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ── Bypass ────────────────────────────────────────────────────────────────────
_mode = os.environ.get("DG_GRAPH_GATE", "1")
if _mode == "0":
    sys.exit(0)

# ── Parse hook payload ────────────────────────────────────────────────────────
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = payload.get("tool_name", "")
tool_input = payload.get("tool_input") or {}

# ── Read: warn but don't block (required for Edit/Write workflow) ────────────
if tool == "Read":
    file_path = tool_input.get("file_path", "")
    # Don't warn on config/meta files
    _EXEMPT_BASENAMES = {
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
        "pyproject.toml", "setup.py", "requirements.txt",
        "tsconfig.json", ".eslintrc.json", ".env", ".gitignore",
        "CLAUDE.md", "CONTEXT.md", "MEMORY.md", "README.md",
        "Dockerfile", "docker-compose.yml",
    }
    _EXEMPT_PATTERNS = re.compile(
        r"(?:README|CHANGELOG|LICENSE|CONTRIBUTING)(?:\.\w+)?$"
        r"|\.(?:lock|toml|yml|yaml|json|md)$"
        r"|/\.claude/|/\.dual-graph",
        re.IGNORECASE,
    )
    basename = Path(file_path).name if file_path else ""
    if basename not in _EXEMPT_BASENAMES and not _EXEMPT_PATTERNS.search(file_path):
        print(
            f"HINT: graph_read(file=\"{file_path}\") is more token-efficient than Read.\n"
            f"graph_read returns only the relevant symbol/section, not the full file.\n"
            f"Use Read only when you need the full file for editing.",
            file=sys.stderr,
        )
    sys.exit(0)  # Always allow — never block Read

if tool != "Bash":
    sys.exit(0)

# ── Exploration detection for Bash ────────────────────────────────────────────
_EXPLORATION = re.compile(
    r"^\s*(?:cat|head|tail|less|more|bat)\b"
    r"|^\s*(?:grep|rg|ag|ack|ripgrep)\b"
    r"|^\s*(?:find|fd|locate|mdfind)\b"
    r"|^\s*(?:ls|tree|exa|eza)\b"
    r"|^\s*(?:wc)\b"
)

_ALWAYS_ALLOW = re.compile(
    r"^\s*(?:git|npm|yarn|pnpm|pip|cargo|go|make|gradle|mvn|cmake)\b"
    r"|^\s*(?:python3?|node|ruby|java|javac|rustc|gcc|g\+\+|clang)\b"
    r"|^\s*(?:docker|kubectl|terraform|helm)\b"
    r"|^\s*(?:curl|wget|ssh|scp|rsync)\b"
    r"|^\s*(?:kill|ps|top|htop|lsof|pgrep|pkill)\b"
    r"|^\s*(?:echo|printf|date|env|export|set|source|eval)\b"
    r"|^\s*(?:cd|pwd|mkdir|touch|rm|mv|cp|chmod|chown|ln)\b"
    r"|^\s*(?:test|true|false|\[|sleep|wait|nohup|xargs)\b"
    r"|^\s*(?:gh|brew|apt|yum|dnf|pacman)\b"
    r"|^\s*(?:vercel|npx|bunx|wrangler|netlify|fly|railway|supabase)\b"
    r"|^\s*(?:aws|gcloud|az|firebase|heroku|cf)\b"
    r"|^\s*(?:sed|awk)\b"
    r"|^\s*(?:tee|sort|uniq|cut|tr|column|jq|yq)\b"
)

_SPLIT_SEQ = re.compile(r"\s*(?:&&|\|\||;)\s*")
_SPLIT_PIPE = re.compile(r"\s*\|\s*")


def _is_exploration_bash(cmd: str) -> bool:
    """Return True if any segment of a compound command is exploration.

    Only the first command in a pipeline is checked — piped grep/head/tail
    filters CLI output, not the filesystem.
    """
    segments = _SPLIT_SEQ.split(cmd)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        first_in_pipe = _SPLIT_PIPE.split(seg)[0].strip()
        if not first_in_pipe:
            continue
        if _ALWAYS_ALLOW.search(first_in_pipe):
            continue
        if _EXPLORATION.search(first_in_pipe):
            return True
    return False


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


def _mcp_server_alive(data_dir: Path) -> bool:
    """Check if the MCP server is likely running.

    Since the server uses stdio transport (managed by Claude Code),
    there's no PID file. We check if a graph server process exists
    or if the tool log was recently written (last 10 min).
    """
    import subprocess

    # Check 1: any mcp_graph_server process running?
    try:
        r = subprocess.run(
            ["pgrep", "-f", "mcp_graph_server"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
    except Exception:
        pass

    # Check 2: tool log recently written?
    log_file = data_dir / "mcp_tool_calls.jsonl"
    if log_file.exists():
        try:
            mtime = log_file.stat().st_mtime
            import time
            if time.time() - mtime < 600:  # 10 minutes
                return True
        except OSError:
            pass

    return False


# ── Path extraction from Bash commands ───────────────────────────────────────
_QUOTED_PATH = re.compile(r'["\']([~/][^"\']+)["\']')
_BARE_ABS_PATH = re.compile(r'(?:^|\s)((?:/|~/)[^\s;|&><]+)')

def _extract_paths(cmd: str) -> list[str]:
    """Extract file/directory paths mentioned in a command."""
    paths = []
    for m in _QUOTED_PATH.finditer(cmd):
        paths.append(m.group(1))
    for m in _BARE_ABS_PATH.finditer(cmd):
        p = m.group(1).rstrip("'\")")
        if p not in paths:
            paths.append(p)
    return paths


def _load_external_refs(data_dir: Path) -> dict:
    """Read external_refs from action graph, skipping stale entries (>24h)."""
    import time as _t
    f = data_dir / "chat_action_graph.json"
    if not f.exists():
        return {}
    try:
        g = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = g.get("external_refs", {})
    if not isinstance(raw, dict):
        return {}
    now = _t.time()
    return {k: v for k, v in raw.items()
            if isinstance(v, dict) and now - float(v.get("ts", 0)) < 86400}


def _path_in_external_refs(path_str: str, ext_refs: dict) -> bool:
    """True if path or any registered parent dir is in ext_refs."""
    if not ext_refs:
        return False
    try:
        p = Path(path_str.replace("\\ ", " ").replace("~", str(Path.home()))).resolve()
    except Exception:
        return False
    if str(p) in ext_refs:
        return True
    for key in ext_refs:
        key_norm = key.rstrip("/")
        if not key_norm:
            continue
        try:
            p.relative_to(key_norm)
            return True  # p is inside a registered dir
        except ValueError:
            continue
    return False


def _classify_paths(cmd: str, project_root: Path) -> tuple[list, list]:
    """Return (inside_paths, outside_paths) from explicit absolute paths in cmd."""
    paths = _extract_paths(cmd)
    inside, outside = [], []
    for raw in paths:
        expanded = raw.replace("\\ ", " ").replace("~", str(Path.home()))
        try:
            Path(expanded).resolve().relative_to(project_root.resolve())
            inside.append(raw)
        except ValueError:
            outside.append(raw)
    return inside, outside


# ── Remote execution (never touches local filesystem) ────────────────────────
_REMOTE_EXEC = re.compile(
    r"^\s*(?:ssh|scp|rsync)\b"
    r"|^\s*(?:docker|podman)\s+(?:exec|run)\b"
    r"|^\s*kubectl\s+exec\b"
)


# ── Main logic ────────────────────────────────────────────────────────────────
def main() -> int:
    cmd = tool_input.get("command", "")

    if _REMOTE_EXEC.search(cmd):
        return 0  # Remote execution — always allow

    if not _is_exploration_bash(cmd):
        return 0  # Not exploration — allow

    data_dir = _find_data_dir()
    if data_dir is None:
        return 0  # Not a GrapeRoot project

    project_root = data_dir.parent
    inside_paths, outside_paths = _classify_paths(cmd, project_root)

    if not inside_paths and outside_paths:
        # All paths are outside the project
        ext_refs = _load_external_refs(data_dir)
        if all(_path_in_external_refs(p, ext_refs) for p in outside_paths):
            return 0  # All registered via cross_search — silent allow
        # Not all registered — soft hint, still allow (we don't own external dirs)
        print(
            "HINT: Use cross_search(pattern=\"...\", path=\"<dir>\") to register "
            "external paths before exploring them. You can still proceed.",
            file=sys.stderr,
        )
        return 0

    # Command targets inside project (or no explicit path → assume cwd)
    graph_file = data_dir / "info_graph.json"
    if not graph_file.exists() or graph_file.stat().st_size < 100:
        return 0  # No graph built yet — allow native tools (blocking is pointless)

    # Check if graph was built for a different project (stale from another session)
    # Also check if graph has enough content to be useful (scan may have timed out)
    try:
        import json as _json
        _gdata = _json.loads(graph_file.read_bytes())
        _stored_root = _gdata.get("root", "")
        if _stored_root:
            _current = str(Path(str(project_root)).resolve())
            _stored = str(Path(_stored_root).resolve())
            if _current != _stored:
                return 0  # Stale graph from different project — allow native tools
        # If graph has fewer than 10 files indexed, it's not useful enough to block
        _file_count = _gdata.get("file_count", _gdata.get("node_count", 0))
        if _file_count < 10:
            return 0  # Graph too sparse (scan likely timed out) — allow native tools
    except Exception:
        return 0  # Unreadable graph — allow native tools

    if not _mcp_server_alive(data_dir):
        print(
            "WARNING: GrapeRoot MCP server not running — graph tools unavailable.\n"
            "Native file tools are allowed for now, but context quality is degraded.\n"
            "To restore full context: exit this session and run `dgc-pro` again.",
            file=sys.stderr,
        )
        return 0

    msg = (
        "BLOCKED: Native file exploration is disabled.\n"
        "Instead of this command, use the MCP tool:\n"
        "  • For grep/rg/find → call fallback_rg(pattern=\"<your pattern>\")\n"
        "  • For cat/head/ls → call graph_read(file=\"<path>\")\n"
        "Retry now with the correct graph tool."
    )
    if _mode == "warn":
        print(msg, file=sys.stderr)
        return 0
    print(msg, file=sys.stderr)
    return 2


sys.exit(main())
