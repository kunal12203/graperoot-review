#!/usr/bin/env python3
"""GrapeRoot Pro — Python core. Called by launch_pro.{sh,ps1} after license check.

v1.0.45: real token savings telemetry — stop_hook reads /savings breakdown
         (tokens_avoided_tar, cross_turn, shadow) and sends to Railway;
         webhook stores breakdown columns + /api/usage/savings-chart endpoint

v1.0.44: shims call launch.py directly (not launch_pro.sh), version.txt sync fixed,
         cost calculation moved server-side (Railway webhook)

v1.0.43: per-prompt rows in DB (not cumulative), project_hash from cwd (not hardcoded),
         task_type='prompt' for per-turn granularity

v1.0.42: fix zero token counts — read transcript JSONL instead of Stop hook payload
         (Claude Code Stop hook sends session metadata only, not usage)

v1.0.41: separate Pro telemetry DB (PRO_DATABASE_URL) from leaderboard DB,
         stop_hook.py as file (fixes zero token counts), silent stop hook errors,
         exhaustive free-tier dual-graph cleanup on launch (_is_free_tier_hook)

v1.0.40: auto-remove free-tier dual-graph when Pro is active (Pro is a superset)

v1.0.39: fix Stop hook to POST directly to Railway webhook (not local /session/end)

v1.0.38: usage telemetry pipeline — NeonDB ingest + dashboard API for savings visualization

v1.0.37: gap-aware query intent routing — detects content/control-flow/absence/dynamic queries
         and adjusts confidence so the right exploration tool is used (no more blind file reads)

v1.0.36: gate exempts infrastructure paths (.dual-graph-pro, .claude, .mcp.json) from blocking

v1.0.35: fix scan timeout deadlock — gate allows native tools when graph is sparse/empty,
         build_graph has 120s timeout, graph_scan has 90s timeout with graceful skip

v1.0.34: fix auto-update blocked by Cloudflare (add User-Agent header to urllib requests)

v1.0.33: gate allows native tools when graph is stale OR missing (fixes token bleed on project switch)

v1.0.32: gate allows native tools when graph was never built (large repo scan timeout)

v1.0.31: fix stale graph index when switching projects + remove global hook (teammate token bleed)

v1.0.30: fix SSH/docker/kubectl blocked by gate + fix multi-session project root confusion

v1.0.29: self-update also re-creates ~/.local/bin symlinks
shims in install.sh/ps1 and self-update lists for Mac/Linux/Windows.

v1.0.24: fix Windows self-update missing dg-pro.cmd/.ps1 shims.

v1.0.23: fix MCP server crash when cached graph has wrong root directory.

v1.0.22: fix Codex log message (config.yaml → config.toml).

v1.0.21: multi-platform support — --claude (default), --codex, --gemini, --opencode,
--cursor. dg-pro shorthand for --codex. All platforms get full graph context.

v1.0.20: cross_search tool, gate external-ref tracking, graph hooks auto-registration.

v1.0.19: fix graph gate blocking CLI tools (vercel, aws, etc.), fix --resume flag,
fix action graph read persistence.

v1.0.18: graperoot-pro is now a stdio MCP server. Claude Code spawns it on
session start and kills it on exit — no port management, no orphan processes,
no stale .mcp.json entries. Same pattern as filesystem/git/everyone-else MCPs.

Responsibilities:
  * Build dual-graph index for the target project on first run (cached afterwards)
  * Merge .mcp.json / config with graperoot-pro MCP entry for chosen tool
  * exec chosen AI tool (it owns the MCP lifecycle)
"""
import argparse
import hashlib
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
        # Validate the graph was built for this project, not a different root.
        # A mismatched root means the graph was copied or built incorrectly —
        # it will return irrelevant files and may be enormous (causing server crashes).
        try:
            with open(graph_file, encoding="utf-8") as _f:
                _head = _f.read(512)
            import re as _re
            m = _re.search(r'"root"\s*:\s*"([^"]+)"', _head)
            if m:
                cached_root = Path(m.group(1)).resolve()
                if cached_root != project.resolve():
                    print(
                        f"[dgc-pro] cached graph has wrong root ({cached_root}), "
                        f"expected {project.resolve()} — rebuilding…",
                        flush=True,
                    )
                    graph_file.unlink()
                else:
                    return  # root matches, cache is valid
            else:
                return  # no root field, trust the cache
        except Exception:
            return  # unreadable header, trust the cache
    print(f"[dgc-pro] scanning {project}…  (first-time index, ~2 min for 10k files)", flush=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    builder = PRO_HOME / "graph_builder.py"
    if not builder.exists():
        sys.exit(f"[dgc-pro] graph_builder.py missing from {PRO_HOME} — reinstall required")
    try:
        subprocess.run([sys.executable, str(builder),
                        "--root", str(project), "--out", str(graph_file)],
                       check=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"[dgc-pro] scan timed out (project too large) — starting without graph", flush=True)
        print(f"[dgc-pro] native tools (Read, grep) will work normally", flush=True)
    except subprocess.CalledProcessError:
        print(f"[dgc-pro] scan failed — starting without graph", flush=True)


def _is_free_tier_hook(cmd: str) -> bool:
    """Return True if cmd belongs to the free-tier dual-graph (not Pro)."""
    if ".dual-graph-pro" in cmd or "graperoot-pro" in cmd:
        return False
    if "/session/end" in cmd or "undo_shield" in cmd:
        return True
    # Match .dual-graph followed by path separator or quote (not .dual-graph-pro)
    import re
    return bool(re.search(r'\.dual-graph[/\\"\'\s]', cmd))


def _remove_free_tier_mcp() -> None:
    """Remove dual-graph (free tier) from all MCP configs and kill its process.

    Pro is a superset — running both causes duplicate tools and confusion.
    Searches: top-level mcpServers, per-project mcpServers, and settings.local.json.
    """
    removed = False

    # 1. Remove from ~/.claude.json — both top-level AND per-project entries
    claude_cfg = Path.home() / ".claude.json"
    if claude_cfg.exists():
        try:
            data = json.loads(claude_cfg.read_text(encoding="utf-8"))
            # Top-level mcpServers
            servers = data.get("mcpServers", {})
            if "dual-graph" in servers:
                del servers["dual-graph"]
                data["mcpServers"] = servers
                removed = True
            # Per-project mcpServers (projects dict keyed by path)
            projects = data.get("projects", {})
            for proj_path, proj_data in projects.items():
                if isinstance(proj_data, dict):
                    proj_servers = proj_data.get("mcpServers", {})
                    if "dual-graph" in proj_servers:
                        del proj_servers["dual-graph"]
                        proj_data["mcpServers"] = proj_servers
                        removed = True
            if removed:
                claude_cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    # 2. Remove from ~/.claude/settings.local.json
    claude_local = Path.home() / ".claude" / "settings.local.json"
    if claude_local.exists():
        try:
            data = json.loads(claude_local.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            if "dual-graph" in servers:
                del servers["dual-graph"]
                data["mcpServers"] = servers
                claude_local.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                removed = True
        except Exception:
            pass

    # 3. Clean stale dual-graph hooks from project settings.local.json
    project = Path.cwd()
    proj_settings = project / ".claude" / "settings.local.json"
    if proj_settings.exists():
        try:
            sdata = json.loads(proj_settings.read_text(encoding="utf-8"))
            hooks = sdata.get("hooks", {})
            dirty = False
            for event in list(hooks.keys()):
                entries = hooks[event]
                if not isinstance(entries, list):
                    continue
                cleaned = []
                for entry in entries:
                    hook_list = entry.get("hooks", [])
                    filtered = [h for h in hook_list
                                if not (_is_free_tier_hook(h.get("command", "")))]
                    if filtered:
                        entry["hooks"] = filtered
                        cleaned.append(entry)
                    elif hook_list:
                        dirty = True
                if cleaned != entries:
                    hooks[event] = cleaned
                    dirty = True
                if not hooks[event]:
                    del hooks[event]
                    dirty = True
            if dirty:
                sdata["hooks"] = hooks
                proj_settings.write_text(json.dumps(sdata, indent=2) + "\n", encoding="utf-8")
                removed = True
        except Exception:
            pass

    # 4. Kill any running dual-graph MCP server process
    try:
        if IS_WINDOWS:
            r = subprocess.run(["wmic", "process", "where",
                                "commandline like '%mcp_graph_server%' and commandline like '%dual-graph%'",
                                "get", "processid"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", line], capture_output=True, timeout=3)
        else:
            r = subprocess.run(["ps", "-axo", "pid=,command="],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "mcp_graph_server" in line and "dual-graph" in line and "graperoot-pro" not in line:
                    parts = line.split(None, 1)
                    if parts and parts[0].isdigit():
                        os.kill(int(parts[0]), signal.SIGTERM)
    except Exception:
        pass

    if removed:
        print("[dgc-pro] Removed free-tier dual-graph (Pro replaces it)", flush=True)


def write_mcp_config(project: Path, data_dir: Path, port: int) -> Path:
    """Merge graperoot-pro into project's .mcp.json as an HTTP MCP entry.

    The server runs as a background HTTP process — Claude Code / opencode / cursor
    connect via HTTP transport. Other MCP entries in the project are preserved.
    """
    cfg = project / ".mcp.json"
    existing = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    servers = existing.setdefault("mcpServers", {})
    servers["graperoot-pro"] = {
        "type": "http",
        "url": f"http://localhost:{port}/mcp",
    }
    cfg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return cfg


def write_opencode_config(project: Path, port: int) -> Path:
    """Merge graperoot-pro into project's opencode.json.

    Also injects CLAUDE.md into `instructions` so the graph policy lands in
    the system prompt — strongest available enforcement for opencode (no hooks).
    """
    cfg = project / "opencode.json"
    existing = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.setdefault("mcp", {})["graperoot-pro"] = {
        "type": "remote",
        "url": f"http://127.0.0.1:{port}/mcp",
    }
    # Inject policy file into system prompt via instructions[]
    instructions = existing.get("instructions", [])
    if "CLAUDE.md" not in instructions:
        instructions.append("CLAUDE.md")
        existing["instructions"] = instructions
    cfg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return cfg


def write_codex_config(port: int) -> None:
    """Inject graperoot-pro into Codex global MCP config (~/.codex/config.toml).

    Codex uses TOML. Format:
        [mcp_servers.graperoot-pro]
        url = "http://127.0.0.1:PORT/mcp"
    """
    import re
    codex_cfg = Path.home() / ".codex" / "config.toml"
    codex_cfg.parent.mkdir(exist_ok=True)
    new_block = (
        f'\n[mcp_servers.graperoot-pro]\n'
        f'url = "http://127.0.0.1:{port}/mcp"\n'
    )
    if codex_cfg.exists():
        content = codex_cfg.read_text(encoding="utf-8")
        # Remove any stale graperoot-pro block (any port)
        content = re.sub(
            r'\n\[mcp_servers\.graperoot-pro\]\nurl = "http://127\.0\.0\.1:\d+/mcp"\n',
            "", content,
        )
        content = content.rstrip("\n") + new_block
    else:
        content = new_block.lstrip("\n")
    codex_cfg.write_text(content, encoding="utf-8")


def write_gemini_config(project: Path, port: int) -> Path:
    """Inject graperoot-pro into project's .gemini/settings.json.

    Gemini reads MCP servers from <project>/.gemini/settings.json:
        {"mcpServers": {"name": {"url": "...", "type": "http"}}}
    """
    gemini_dir = project / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    cfg = gemini_dir / "settings.json"
    existing = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.setdefault("mcpServers", {})["graperoot-pro"] = {
        "url": f"http://127.0.0.1:{port}/mcp",
        "type": "http",
    }
    cfg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return cfg


CODEX_MD_POLICY = """<!-- dg-policy-v1 -->
# Dual-Graph Context Policy

This project uses a local dual-graph MCP server (graperoot-pro) for efficient,
budget-aware context retrieval. Always prefer it over native file exploration.

## MANDATORY: Always follow this order

1. **Call `graph_continue` first** -- before any file exploration, grep, or code reading.

2. **If `graph_continue` returns `needs_project=true`**: call `graph_scan` with the
   current project directory. Do NOT ask the user.

3. **If `graph_continue` returns `skip=true`**: project is too small. Explore normally.

4. **Read `recommended_files`** using `graph_read` -- one call per file.
   Pass `file::symbol` entries verbatim (reads only that symbol's lines).

5. **Check `confidence` and obey the caps strictly:**
   - `confidence=high` -> Stop. Do NOT grep or explore further.
   - `confidence=medium` -> `fallback_rg` at most `max_supplementary_greps` time(s),
     then `graph_read` at most `max_supplementary_files` file(s). Then stop.
   - `confidence=low` -> same as medium.

## Rules

- Do NOT use native file exploration before calling `graph_continue`.
- After edits, call `graph_register_edit(files: ["path/to/file"])`.
<!-- /dg-policy-v1 -->
"""

GEMINI_MD_POLICY = """<!-- grp-policy-v1 -->
# Dual-Graph Context Policy

This project uses a local dual-graph MCP server (graperoot-pro) for efficient context retrieval.

## MANDATORY: Always follow this order

1. **Call `graph_continue` first** -- before any file exploration, grep, or code reading.
2. **If `needs_project=true`**: call `graph_scan` with the current project directory.
3. **If `skip=true`**: project is too small. Explore normally.
4. **Read `recommended_files`** using `graph_read` -- one call per file.
5. **Obey confidence caps**: high=stop, medium/low=limited fallback_rg + graph_read.

## Rules

- Do NOT use grep/bash/file exploration before `graph_continue`.
- After edits, call `graph_register_edit(files: ["path/to/file"])`.
<!-- /grp-policy-v1 -->
"""

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


def write_codex_md(project: Path) -> tuple[Path, str]:
    """Ensure CODEX.md instructs Codex to use graperoot-pro tools."""
    cfg = project / "CODEX.md"
    if not cfg.exists():
        cfg.write_text(CODEX_MD_POLICY, encoding="utf-8")
        return cfg, "created"
    existing = cfg.read_text(encoding="utf-8", errors="replace")
    if "dg-policy-v" in existing:
        return cfg, "kept"
    cfg.write_text(CODEX_MD_POLICY + "\n---\n\n" + existing, encoding="utf-8")
    return cfg, "prepended"


def write_gemini_md(project: Path) -> tuple[Path, str]:
    """Ensure GEMINI.md instructs Gemini CLI to use graperoot-pro tools."""
    cfg = project / "GEMINI.md"
    if not cfg.exists():
        cfg.write_text(GEMINI_MD_POLICY, encoding="utf-8")
        return cfg, "created"
    existing = cfg.read_text(encoding="utf-8", errors="replace")
    if "grp-policy-v" in existing:
        return cfg, "kept"
    cfg.write_text(GEMINI_MD_POLICY + "\n---\n\n" + existing, encoding="utf-8")
    return cfg, "prepended"


def write_hooks(project: Path, data_dir: Path) -> None:
    """Register PreToolUse (graph gate) and PostToolUse (graph sync) hooks."""
    gate_script = PRO_HOME / "graph_gate.py"
    sync_script = PRO_HOME / "graph_sync.py"
    if not gate_script.exists():
        return
    settings_dir = project / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings_file = settings_dir / "settings.local.json"
    existing = {}
    if settings_file.exists():
        try:
            existing = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    python = sys.executable
    gate_cmd = f'DG_DATA_DIR="{data_dir}" {python} "{gate_script}"'
    sync_cmd = f'DG_DATA_DIR="{data_dir}" {python} "{sync_script}"'
    gate_entry = {"matcher": "Bash|Read", "hooks": [{"type": "command", "command": gate_cmd}]}
    sync_entry = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": sync_cmd}]}
    hooks = existing.setdefault("hooks", {})
    # Clean stale entries (Pro's own graph_gate/sync + free-tier dual-graph hooks)
    for hook_type in ("PreToolUse", "PostToolUse"):
        old = hooks.get(hook_type, [])
        hooks[hook_type] = [e for e in old if not any(
            "graph_gate" in h.get("command", "")
            or "graph_sync" in h.get("command", "")
            or _is_free_tier_hook(h.get("command", ""))
            for h in e.get("hooks", [])
        )]
    # Clean stale SessionStart/PreCompact (dual-graph prime.sh)
    for hook_type in ("SessionStart", "PreCompact"):
        old = hooks.get(hook_type, [])
        hooks[hook_type] = [e for e in old if not any(
            _is_free_tier_hook(h.get("command", ""))
            for h in e.get("hooks", [])
        )]
        if not hooks[hook_type]:
            del hooks[hook_type]
    hooks.setdefault("PreToolUse", []).insert(0, gate_entry)
    hooks.setdefault("PostToolUse", []).insert(0, sync_entry)
    existing["hooks"] = hooks
    settings_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    # Clear gate file for fresh session
    gc_active = data_dir / ".gc_active"
    if gc_active.exists():
        gc_active.unlink()
    print(f"[dgc-pro] Graph Gate + Sync hooks registered", flush=True)


def write_stop_hook(project: Path, port: int) -> None:
    """Register a Stop hook that pushes session usage to the Railway webhook (NeonDB).

    Posts directly to the remote webhook — no local /session/end intermediary needed.
    The MCP server port is used to fetch /savings data locally before posting.
    """
    settings_dir = project / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings_file = settings_dir / "settings.local.json"
    existing = {}
    if settings_file.exists():
        try:
            existing = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    license_file = PRO_HOME / "license.key"
    if not license_file.exists():
        return

    python = sys.executable
    project_hash = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    # Write stop hook as a proper script file — avoids shell quoting / newline issues with -c
    stop_py = PRO_HOME / "stop_hook.py"
    stop_py.write_text(
        "import sys, json, urllib.request, hashlib\n"
        "try:\n"
        "    d = json.loads(sys.stdin.read())\n"
        "    session_id = d.get('session_id', '')\n"
        "    transcript_path = d.get('transcript_path', '')\n"
        "    cwd = d.get('cwd', '')\n"
        "    project_hash = hashlib.sha256(cwd.encode()).hexdigest()[:16] if cwd else 'unknown'\n"
        "    model = 'claude-sonnet-4-6'\n"
        "    inp = out = cr = cw = 0\n"
        "    if transcript_path:\n"
        "        try:\n"
        "            last_u = None\n"
        "            with open(transcript_path, 'r', encoding='utf-8') as f:\n"
        "                for line in f:\n"
        "                    line = line.strip()\n"
        "                    if not line: continue\n"
        "                    try: entry = json.loads(line)\n"
        "                    except Exception: continue\n"
        "                    u = entry.get('usage')\n"
        "                    if u: last_u = u\n"
        "                    msg = entry.get('message', {})\n"
        "                    if isinstance(msg, dict):\n"
        "                        mu = msg.get('usage')\n"
        "                        if mu: last_u = mu\n"
        "                        if msg.get('model'): model = msg['model']\n"
        "            if last_u:\n"
        "                inp = last_u.get('input_tokens', 0)\n"
        "                out = last_u.get('output_tokens', 0)\n"
        "                cr  = last_u.get('cache_read_input_tokens', 0)\n"
        "                cw  = last_u.get('cache_creation_input_tokens', 0)\n"
        "        except Exception: pass\n"
        "    tokens_avoided = 0\n"
        "    tokens_avoided_tar = 0\n"
        "    tokens_avoided_cross_turn = 0\n"
        "    shadow_tokens_avoided = 0\n"
        "    graperoot_overhead_tokens = 0\n"
        "    tool_hits_str = '{}'\n"
        "    try:\n"
        f"        resp = urllib.request.urlopen('http://127.0.0.1:{port}/savings', timeout=3)\n"
        "        savings = json.loads(resp.read())\n"
        "        if savings.get('ok'):\n"
        "            tokens_avoided = savings.get('tokens_avoided', 0)\n"
        "            tokens_avoided_tar = savings.get('tokens_avoided_tar', 0)\n"
        "            tokens_avoided_cross_turn = savings.get('tokens_avoided_cross_turn', 0)\n"
        "            shadow_tokens_avoided = savings.get('shadow_savings', {}).get('total_tokens', 0)\n"
        "            graperoot_overhead_tokens = savings.get('graperoot_overhead_tokens', 0)\n"
        "            tool_hits_str = json.dumps(savings.get('tool_hits', {}))\n"
        "    except Exception: pass\n"
        f"    lk  = open('{license_file}').read().strip()\n"
        "    p   = json.dumps({\n"
        "        'license_key': lk, 'session_id': session_id, 'model': model,\n"
        "        'input_tokens': inp, 'output_tokens': out,\n"
        "        'cache_read_tokens': cr, 'cache_write_tokens': cw,\n"
        "        'tokens_avoided': tokens_avoided,\n"
        "        'tokens_avoided_tar': tokens_avoided_tar,\n"
        "        'tokens_avoided_cross_turn': tokens_avoided_cross_turn,\n"
        "        'shadow_tokens_avoided': shadow_tokens_avoided,\n"
        "        'graperoot_overhead_tokens': graperoot_overhead_tokens,\n"
        "        'tool_hits': tool_hits_str,\n"
        "        'task_type': 'prompt', 'confidence': 'none',\n"
        "        'project_hash': project_hash,\n"
        "        'device_host': __import__('socket').gethostname(),\n"
        "    }).encode()\n"
        "    urllib.request.urlopen(urllib.request.Request(\n"
        "        'https://graperoot-review-production.up.railway.app/api/usage',\n"
        "        data=p, headers={'Content-Type': 'application/json'}, method='POST'\n"
        "    ), timeout=5)\n"
        "except Exception:\n"
        "    pass\n",
        encoding="utf-8",
    )
    stop_script = f'{python} "{stop_py}"'
    stop_entry = {"matcher": "", "hooks": [{"type": "command", "command": stop_script}]}
    hooks = existing.setdefault("hooks", {})
    old_stop = hooks.get("Stop", [])
    hooks["Stop"] = [e for e in old_stop
                     if "/api/usage" not in json.dumps(e)
                     and "/session/end" not in json.dumps(e)
                     and "dual-graph/stop.sh" not in json.dumps(e)]
    hooks["Stop"].append(stop_entry)
    existing["hooks"] = hooks
    settings_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def write_gemini_hooks(project: Path, data_dir: Path) -> None:
    """Register BeforeTool (graph gate) hook in .gemini/settings.json.

    Gemini's equivalent of Claude's PreToolUse hook. Tool names differ:
      Bash → run_shell_command,  Read → read_file
    """
    gate_script = PRO_HOME / "graph_gate.py"
    if not gate_script.exists():
        return
    gemini_dir = project / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    cfg = gemini_dir / "settings.json"
    existing = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            pass
    python = sys.executable
    gate_cmd = f'DG_DATA_DIR="{data_dir}" GEMINI_PROJECT_DIR="{project}" {python} "{gate_script}"'
    gate_entry = {"matcher": "run_shell_command|read_file|glob|grep|ls", "hooks": [{"type": "command", "command": gate_cmd}]}
    hooks = existing.setdefault("hooks", {})
    # Remove stale gate entries
    old = hooks.get("BeforeTool", [])
    hooks["BeforeTool"] = [e for e in old if not any(
        "graph_gate" in h.get("command", "") for h in e.get("hooks", [])
    )]
    hooks["BeforeTool"].insert(0, gate_entry)
    existing["hooks"] = hooks
    cfg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    gc_active = data_dir / ".gc_active"
    if gc_active.exists():
        gc_active.unlink()
    print(f"[graperoot-pro] Graph Gate hook registered for Gemini", flush=True)


def start_mcp_server(port: int, project: Path, data_dir: Path) -> subprocess.Popen:
    server_py = PRO_HOME / "mcp_graph_server_v7.5.py"
    if not server_py.exists():
        sys.exit(f"[dgc-pro] server binary missing from {PRO_HOME} — reinstall required")
    env = {
        **os.environ,
        "PORT": str(port),
        "HOST": "127.0.0.1",
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


def resolve_codex() -> str:
    for c in ("codex", "codex.cmd", "codex.exe"):
        p = shutil.which(c)
        if p:
            return p
    sys.exit("[dg-pro] `codex` CLI not found. Install: npm install -g @openai/codex")


def resolve_gemini() -> str:
    for c in ("gemini", "gemini.cmd", "gemini.exe"):
        p = shutil.which(c)
        if p:
            return p
    sys.exit("[graperoot-pro] `gemini` CLI not found. Install: npm install -g @google/generative-ai")


def resolve_opencode() -> str:
    for c in ("opencode", "opencode.cmd", "opencode.exe"):
        p = shutil.which(c)
        if p:
            return p
    sys.exit("[graperoot-pro] `opencode` CLI not found. Install: curl -fsSL https://opencode.ai/install.sh | bash")


def resolve_cursor() -> str:
    # Cursor on Mac/Linux uses `cursor` CLI; on Windows it may be `cursor.cmd`
    for c in ("cursor", "cursor.cmd", "cursor.exe"):
        p = shutil.which(c)
        if p:
            return p
    # macOS app bundle fallback
    mac_cursor = "/Applications/Cursor.app/Contents/MacOS/Cursor"
    if Path(mac_cursor).exists():
        return mac_cursor
    sys.exit("[graperoot-pro] `cursor` not found. Install from https://www.cursor.com")


def _local_version() -> str:
    vf = PRO_HOME / "bin" / "version.txt"
    if vf.exists():
        return vf.read_text().strip()
    vf2 = PRO_HOME / "VERSION"
    if vf2.exists():
        return vf2.read_text().strip()
    return "0.0.0"


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0, 0, 0)


def auto_update() -> None:
    """Check for newer version and self-update if available."""
    import urllib.request
    import tarfile
    import tempfile

    local_ver = _local_version()
    license_file = PRO_HOME / "license.key"
    if not license_file.exists():
        return  # Can't verify without license

    license_key = license_file.read_text().strip()
    api_url = "https://api.graperoot.dev/v1/license/verify"

    # Check remote version (fast — just a JSON POST)
    try:
        body = json.dumps({"license_key": license_key, "host": socket.gethostname(), "os": sys.platform}).encode()
        req = urllib.request.Request(api_url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": f"GrapeRoot-Pro/{local_ver}",
        }, method="POST")
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
    except Exception:
        return  # Network issue — skip silently

    if not data.get("valid"):
        return

    remote_ver = data.get("version", "").lstrip("v")
    if not remote_ver:
        return

    if _version_tuple(remote_ver) <= _version_tuple(local_ver):
        return  # Already up to date

    download_url = data.get("download_url", "")
    if not download_url:
        return

    print(f"[dgc-pro] Update available: v{local_ver} → v{remote_ver}", flush=True)
    print(f"[dgc-pro] Downloading...", flush=True)

    try:
        tmp = tempfile.mkdtemp(prefix="grp-pro-update-")
        tarball = os.path.join(tmp, "graperoot-pro.tar.gz")
        dl_req = urllib.request.Request(download_url, headers={"User-Agent": f"GrapeRoot-Pro/{local_ver}"})
        with urllib.request.urlopen(dl_req, timeout=60) as dl_resp, open(tarball, "wb") as f:
            f.write(dl_resp.read())

        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(tmp, filter="data" if hasattr(tarfile, "data_filter") else None)

        extracted = Path(tmp) / "graperoot-pro"
        if not extracted.is_dir():
            return

        # Copy new files over existing install (preserve license.key, venv, data)
        preserve = {"license.key", "venv", "server.log", "bin"}
        for item in extracted.iterdir():
            dest = PRO_HOME / item.name
            if item.name in preserve:
                continue
            if item.is_file():
                shutil.copy2(str(item), str(dest))
            elif item.is_dir():
                if dest.exists():
                    shutil.rmtree(str(dest))
                shutil.copytree(str(item), str(dest))

        # Update version file
        ver_src = extracted / "VERSION"
        if ver_src.exists():
            bin_dir = PRO_HOME / "bin"
            bin_dir.mkdir(exist_ok=True)
            shutil.copy2(str(ver_src), str(bin_dir / "version.txt"))

        # Copy bin/ contents (doctor, etc.)
        src_bin = extracted / "bin"
        if src_bin.is_dir():
            dest_bin = PRO_HOME / "bin"
            dest_bin.mkdir(exist_ok=True)
            for f in src_bin.iterdir():
                shutil.copy2(str(f), str(dest_bin / f.name))

        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[dgc-pro] Updated to v{remote_ver}", flush=True)

    except Exception as e:
        print(f"[dgc-pro] Update failed ({e}) — continuing with current version", flush=True)


def _detect_tool_from_argv(argv: list[str]) -> str:
    """Return tool name if a --tool flag is in argv, else 'claude'."""
    for a in argv:
        if a in ("--codex", "codex", "--dg-pro", "dg-pro"):
            return "codex"
        if a in ("--gemini", "gemini"):
            return "gemini"
        if a in ("--opencode", "opencode"):
            return "opencode"
        if a in ("--cursor", "cursor"):
            return "cursor"
        if a in ("--claude", "claude"):
            return "claude"
    return "claude"


def main() -> None:
    # Detect tool early so we can set prog name for help text
    tool = _detect_tool_from_argv(sys.argv[1:])
    prog = {"claude": "dgc-pro", "codex": "dg-pro"}.get(tool, "graperoot-pro")

    ap = argparse.ArgumentParser(prog=prog,
        description=f"GrapeRoot Pro — dual-graph context engine (v1.0). Tool: {tool}.")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--skip-update", action="store_true", help="Skip auto-update check")
    # Tool selection (stripped before passthrough)
    ap.add_argument("--claude", dest="_tool", action="store_const", const="claude")
    ap.add_argument("--codex", "--dg-pro", dest="_tool", action="store_const", const="codex")
    ap.add_argument("--gemini", dest="_tool", action="store_const", const="gemini")
    ap.add_argument("--opencode", dest="_tool", action="store_const", const="opencode")
    ap.add_argument("--cursor", dest="_tool", action="store_const", const="cursor")
    # Claude-specific passthrough flags (prevent positional confusion)
    ap.add_argument("--resume", "-r", dest="_resume", default=None)
    ap.add_argument("--model", dest="_model", default=None)
    ap.add_argument("--prompt", "-p", dest="_prompt", default=None)
    ap.add_argument("--max-turns", dest="_max_turns", default=None)
    ap.add_argument("--system-prompt", dest="_system_prompt", default=None)
    args, passthrough = ap.parse_known_args()

    tool = args._tool or tool  # honour explicit flag if present

    # Re-inject Claude-specific flags into passthrough (only used when tool=claude)
    if tool == "claude":
        if args._resume:
            passthrough = ["--resume", args._resume] + passthrough
        if args._model:
            passthrough = ["--model", args._model] + passthrough
        if args._prompt:
            passthrough = ["--prompt", args._prompt] + passthrough
        if args._max_turns:
            passthrough = ["--max-turns", args._max_turns] + passthrough
        if args._system_prompt:
            passthrough = ["--system-prompt", args._system_prompt] + passthrough

    label = {"claude": "dgc-pro", "codex": "dg-pro"}.get(tool, "graperoot-pro")

    cleanup_orphan_servers()

    if not args.skip_update:
        auto_update()

    if args.version:
        ver = (PRO_HOME / "bin" / "version.txt").read_text().strip() if (PRO_HOME/"bin"/"version.txt").exists() else "1.0.41"
        print(f"{label} v{ver}  (platform: {tool})")
        return

    project = Path(args.project).resolve()
    if not project.is_dir():
        sys.exit(f"[{label}] {project} is not a directory")
    data_dir = project / ".dual-graph-pro"

    build_graph(project, data_dir)

    port = find_free_port()
    proc = start_mcp_server(port, project, data_dir)
    print(f"[{label}] MCP server started on port {port} (pid {proc.pid})", flush=True)

    def _cleanup(*_args):
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    import atexit
    atexit.register(_cleanup)
    if not IS_WINDOWS:
        signal.signal(signal.SIGHUP, lambda *_: (_cleanup(), sys.exit(1)))
        signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(1)))

    # ── Remove free-tier dual-graph if present (Pro is a superset) ─────────
    _remove_free_tier_mcp()

    # ── Per-tool MCP wiring + policy file ──────────────────────────────────
    if tool == "codex":
        write_codex_config(port)
        print(f"[{label}] graperoot-pro injected into ~/.codex/config.toml (port {port})", flush=True)
        doc, action = write_codex_md(project)
    elif tool == "gemini":
        mcp_cfg = write_gemini_config(project, port)
        print(f"[{label}] graperoot-pro registered in {mcp_cfg}", flush=True)
        doc, action = write_gemini_md(project)
        write_gemini_hooks(project, data_dir)
    elif tool == "opencode":
        mcp_cfg = write_opencode_config(project, port)
        print(f"[{label}] graperoot-pro registered in {mcp_cfg}", flush=True)
        doc, action = write_claude_md(project)  # opencode reads CLAUDE.md
    elif tool == "cursor":
        mcp_cfg = write_mcp_config(project, data_dir, port)
        print(f"[{label}] graperoot-pro registered in {mcp_cfg}", flush=True)
        doc, action = write_claude_md(project)
    else:  # claude (default)
        mcp_cfg = write_mcp_config(project, data_dir, port)
        print(f"[{label}] graperoot-pro registered (http://localhost:{port}/mcp) in {mcp_cfg}", flush=True)
        doc, action = write_claude_md(project)

    if action == "created":
        print(f"[{label}] {doc.name} created with dual-graph policy", flush=True)
    elif action == "prepended":
        print(f"[{label}] dual-graph policy prepended to existing {doc.name}", flush=True)

    # Hooks: Claude uses write_hooks(), Gemini already called write_gemini_hooks() above.
    # Codex and opencode have no hook mechanism — policy doc is the only enforcement.
    if tool == "claude":
        write_hooks(project, data_dir)
        write_stop_hook(project, port)

    # ── Launch the tool ─────────────────────────────────────────────────────
    if tool == "codex":
        cli = resolve_codex()
        exit_code = subprocess.call([cli] + passthrough, cwd=str(project))
    elif tool == "gemini":
        cli = resolve_gemini()
        exit_code = subprocess.call([cli] + passthrough, cwd=str(project))
    elif tool == "opencode":
        cli = resolve_opencode()
        exit_code = subprocess.call([cli] + passthrough, cwd=str(project))
    elif tool == "cursor":
        cli = resolve_cursor()
        exit_code = subprocess.call([cli, str(project)] + passthrough)
    else:  # claude
        cli = resolve_claude()
        exit_code = subprocess.call([cli] + passthrough, cwd=str(project))

    _cleanup()
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[dgc-pro] interrupted", file=sys.stderr)
        sys.exit(130)
