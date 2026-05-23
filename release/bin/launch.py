#!/usr/bin/env python3
"""GrapeRoot Pro — Python core. Called by launch_pro.{sh,ps1} after license check.

v1.0.15: graperoot-pro is now a stdio MCP server. Claude Code spawns it on
session start and kills it on exit — no port management, no orphan processes,
no stale .mcp.json entries. Same pattern as filesystem/git/everyone-else MCPs.

Responsibilities:
  * Build dual-graph index for the target project on first run (cached afterwards)
  * Merge .mcp.json with a stdio command entry for `graperoot-pro`
  * exec Claude Code (it owns the MCP lifecycle)
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
PRO_HOME = Path(os.environ.get("GRAPEROOT_PRO_HOME", Path.home() / ".graperoot-pro"))
SERVER_TAG = "mcp_graph_server_v7"  # substring that identifies our MCP server in ps output


def _kill_pid(pid: int) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=3)
        else:
            import signal as _sig
            os.kill(pid, _sig.SIGTERM)
            time.sleep(0.4)
            try:
                os.kill(pid, 0)
                os.kill(pid, _sig.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        pass


def cleanup_orphan_servers() -> int:
    """Kill MCP servers left behind by previous runs whose parent is dead.
    Runs on every dgc-pro invocation. Does nothing if there are no orphans.
    Safe: never touches another live dgc-pro session (checks parent PID).
    """
    killed = 0
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["wmic", "process", "get", "ProcessId,ParentProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=8,
            )
            for line in r.stdout.splitlines():
                if SERVER_TAG not in line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 4:
                    continue
                try:
                    ppid = int(parts[-2]); pid = int(parts[-1])
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                pr = subprocess.run(["tasklist", "/FI", f"PID eq {ppid}"], capture_output=True, text=True, timeout=3)
                if str(ppid) in pr.stdout:
                    continue  # parent alive, not orphan
                _kill_pid(pid); killed += 1
        else:
            r = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,command="],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if SERVER_TAG not in line:
                    continue
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                try:
                    pid = int(parts[0]); ppid = int(parts[1])
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                # ppid <= 1 means reparented to init (Linux) or launchd (Mac) — always orphan
                if ppid <= 1:
                    _kill_pid(pid); killed += 1
                    continue
                try:
                    os.kill(ppid, 0)  # parent alive → not orphan
                except ProcessLookupError:
                    _kill_pid(pid); killed += 1
                except PermissionError:
                    pass  # owned by another user, leave alone
    except Exception:
        pass
    if killed:
        print(f"[dgc-pro] cleaned up {killed} orphan MCP server(s) from previous runs", flush=True)
    return killed


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def build_graph(project: Path, data_dir: Path) -> None:
    """One-time graph scan. Cached in project's .dual-graph-pro/ folder."""
    graph_file = data_dir / "info_graph.json"
    if graph_file.exists():
        return
    print(f"[dgc-pro] scanning {project}…  (first-time index, ~2 min for 10k files)", flush=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    builder = PRO_HOME / "graph_builder.py"
    if not builder.exists():
        sys.exit(f"[dgc-pro] graph_builder.py missing from {PRO_HOME} — reinstall required")
    subprocess.run([sys.executable, str(builder),
                    "--root", str(project), "--out", str(graph_file)], check=True)


def write_mcp_config(project: Path, data_dir: Path) -> Path:
    """Merge graperoot-pro into project's .mcp.json as a stdio MCP entry.

    Claude Code spawns the server when the session starts, kills it on exit.
    Other MCP entries in the project are preserved.
    """
    cfg = project / ".mcp.json"
    existing = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    servers = existing.setdefault("mcpServers", {})
    server_py = PRO_HOME / "mcp_graph_server_v7.5.py"
    servers["graperoot-pro"] = {
        "command": sys.executable,  # the venv python that this launcher is running under
        "args": [str(server_py), "--stdio"],
        "env": {
            "DG_DATA_DIR": str(data_dir),
            "DUAL_GRAPH_PROJECT_ROOT": str(project),
            "GRAPEROOT_PRO_HOME": str(PRO_HOME),
        },
    }
    cfg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return cfg


CLAUDE_MD_POLICY = """<!-- dgc-policy-v1 -->
# Dual-Graph Context Policy

This project uses a local dual-graph MCP server (graperoot-pro) for efficient,
budget-aware context retrieval. Always prefer it over native file exploration.

## MANDATORY: Always follow this order

1. **Call `graph_continue` first** -- before any file exploration, grep, or code reading.

2. **If `graph_continue` returns `needs_project=true`**: call `graph_scan` with the
   current project directory (`pwd`). Do NOT ask the user.

3. **If `graph_continue` returns `skip=true`**: project is too small for the graph to
   help. Skip all graph tools and explore normally.

4. **Read `recommended_files`** using `graph_read` -- one call per file.
   - `recommended_files` may contain `file::symbol` entries (e.g. `src/auth.ts::handleLogin`).
     Pass them verbatim to `graph_read(file: "src/auth.ts::handleLogin")` -- it reads only
     that symbol's lines, not the full file.

5. **Check `confidence` and obey the caps strictly:**
   - `confidence=high` -> Stop. Do NOT grep or explore further.
   - `confidence=medium` -> If recommended files are insufficient, call `fallback_rg`
     at most `max_supplementary_greps` time(s) with specific terms, then `graph_read`
     at most `max_supplementary_files` additional file(s). Then stop.
   - `confidence=low` -> Call `fallback_rg` at most `max_supplementary_greps` time(s),
     then `graph_read` at most `max_supplementary_files` file(s). Then stop.

## Exhaustive enumeration tasks

Some tasks require scanning **every file** -- e.g. "find all dead exports", "list every
.find() without a limit", "audit all test files". Use these tools first:

- **`graph_dead_exports()`** -- pre-computed at scan time. Use for any dead-export task.
- **`graph_grep_all(pattern, file_glob?, max_hits?)`** -- exhaustive grep, no call cap.

## Rules

- Do NOT use `rg`, `grep`, or bash file exploration before calling `graph_continue`.
- Do NOT do broad/recursive exploration at any confidence level.
- After edits, call `graph_register_edit(files: ["path/to/file"])`. The parameter is
  `files` (plural, always an array). Use `file::symbol` notation when the edit targets
  a specific function, class, or hook.
<!-- /dgc-policy-v1 -->
"""


def write_claude_md(project: Path) -> tuple[Path, str]:
    """Ensure project's CLAUDE.md instructs Claude to use graperoot-pro tools.

    Idempotent + non-destructive:
      - No CLAUDE.md  -> create with policy
      - Has CLAUDE.md without our marker -> prepend policy + separator, preserve rest
      - Has CLAUDE.md with our marker (any version) -> leave it alone

    Returns (path, action) where action is "created" | "prepended" | "kept".
    """
    cfg = project / "CLAUDE.md"
    if not cfg.exists():
        cfg.write_text(CLAUDE_MD_POLICY, encoding="utf-8")
        return cfg, "created"
    existing = cfg.read_text(encoding="utf-8", errors="replace")
    # Any existing dgc-policy marker (current or older version) means user has the policy
    if "dgc-policy-v" in existing:
        return cfg, "kept"
    # Prepend, preserving user content unchanged
    cfg.write_text(CLAUDE_MD_POLICY + "\n---\n\n" + existing, encoding="utf-8")
    return cfg, "prepended"


def start_mcp_server(port: int, project: Path, data_dir: Path) -> subprocess.Popen:
    server_py = PRO_HOME / "mcp_graph_server_v7.5.py"
    if not server_py.exists():
        sys.exit(f"[dgc-pro] server binary missing from {PRO_HOME} — reinstall required")
    env = {
        **os.environ,
        "PORT": str(port),
        "DG_DATA_DIR": str(data_dir),
        "DUAL_GRAPH_PROJECT_ROOT": str(project),
    }
    log_file = PRO_HOME / "server.log"
    with open(log_file, "a") as lf:
        lf.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')}  port={port}  project={project}\n")
    proc = subprocess.Popen([sys.executable, str(server_py)], env=env,
                            stdout=open(log_file, "a"), stderr=subprocess.STDOUT)
    if not wait_port(port, timeout=30):
        proc.terminate()
        sys.exit("[dgc-pro] MCP server failed to start within 30s (check ~/.graperoot-pro/server.log)")
    return proc


def resolve_claude() -> str:
    for c in ("claude", "claude.cmd", "claude.exe"):
        p = shutil.which(c)
        if p:
            return p
    sys.exit("[dgc-pro] `claude` CLI not found. Install: npm install -g @anthropic-ai/claude-code")


def main() -> None:
    ap = argparse.ArgumentParser(prog="dgc-pro",
        description="GrapeRoot Pro — Claude Code + dual-graph context engine (Pro v1.0).")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--version", action="store_true")
    # Everything else passes through to claude
    args, passthrough = ap.parse_known_args()

    # Wipe any MCP servers left behind by a previous run that was hard-killed
    # (Force Quit, kill -9, crash). Runs before anything else — cheap no-op when clean.
    cleanup_orphan_servers()

    if args.version:
        ver = (PRO_HOME / "bin" / "version.txt").read_text().strip() if (PRO_HOME/"bin"/"version.txt").exists() else "1.0.15"
        print(f"dgc-pro v{ver}")
        return

    project = Path(args.project).resolve()
    if not project.is_dir():
        sys.exit(f"[dgc-pro] {project} is not a directory")
    data_dir = project / ".dual-graph-pro"

    build_graph(project, data_dir)
    mcp_cfg = write_mcp_config(project, data_dir)
    print(f"[dgc-pro] graperoot-pro registered (stdio) in {mcp_cfg}", flush=True)
    claude_md, action = write_claude_md(project)
    if action == "created":
        print(f"[dgc-pro] CLAUDE.md created with dual-graph policy ({claude_md})", flush=True)
    elif action == "prepended":
        print(f"[dgc-pro] dual-graph policy prepended to existing CLAUDE.md ({claude_md})", flush=True)
    # action == "kept": stay silent, user already has the policy
    claude = resolve_claude()
    sys.exit(subprocess.call([claude] + passthrough, cwd=str(project)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[dgc-pro] interrupted", file=sys.stderr)
        sys.exit(130)
