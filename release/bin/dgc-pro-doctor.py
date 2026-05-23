#!/usr/bin/env python3
"""GrapeRoot Pro Health Check & Auto-Fix

Diagnoses and fixes common MCP connection issues:
  • Version mismatches (v7.4 vs v7.5)
  • Missing Python dependencies
  • Orphan server processes
  • Hook errors blocking stdio
  • Port conflicts

Usage:
  python3 dgc-pro-doctor.py              # Run diagnostics
  python3 dgc-pro-doctor.py --fix        # Auto-fix issues
  python3 dgc-pro-doctor.py --check-mcp  # Just check MCP connectivity
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PRO_HOME = Path.home() / ".graperoot-pro"
MCP_CONFIG = Path.home() / ".mcp.json"
CLAUDE_HOOKS = Path.home() / ".claude" / "hooks"

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def color(text, col):
    return f"{col}{text}{Colors.RESET}"

def check(name):
    return f"[{color('✓', Colors.GREEN)}] {name}"

def fail(name):
    return f"[{color('✗', Colors.RED)}] {name}"

def warn(name):
    return f"[{color('⚠', Colors.YELLOW)}] {name}"

def header(text):
    print(f"\n{color('═' * 60, Colors.BLUE)}")
    print(f"{color(text.center(60), Colors.BOLD)}")
    print(f"{color('═' * 60, Colors.BLUE)}\n")

def run_cmd(cmd, timeout=5):
    """Run command, return (success, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)

# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics
# ─────────────────────────────────────────────────────────────────────────────

def check_installation():
    """Check if GrapeRoot Pro is installed"""
    header("Installation Check")

    issues = []

    if not PRO_HOME.exists():
        print(fail(f"GrapeRoot Pro not found at {PRO_HOME}"))
        issues.append("missing_install")
        return issues

    print(check(f"Installation found at {PRO_HOME}"))

    # Check for v7.5 server file
    v75 = PRO_HOME / "mcp_graph_server_v7.5.py"
    v74 = PRO_HOME / "mcp_graph_server_v7.4.py"

    if v75.exists():
        print(check("MCP server v7.5 present"))
    elif v74.exists():
        print(warn("Only v7.4 found (old version)"))
        issues.append("old_server_version")
    else:
        print(fail("MCP server file missing"))
        issues.append("missing_server")

    # Check launch.py
    launch_py = PRO_HOME / "launch.py"
    if launch_py.exists():
        content = launch_py.read_text()
        if "v7.5.py" in content:
            print(check("launch.py references v7.5 ✓"))
        elif "v7.4.py" in content:
            print(warn("launch.py references v7.4 (needs update)"))
            issues.append("launch_py_old_version")
        else:
            print(warn("launch.py version unclear"))

    return issues

def check_python_deps():
    """Check if required Python packages are installed"""
    header("Python Dependencies")

    issues = []
    venv_python = PRO_HOME / "venv" / "bin" / "python3"

    if not venv_python.exists():
        print(fail("Virtual environment not found"))
        issues.append("missing_venv")
        return issues

    print(check("Virtual environment exists"))

    required = ["mcp", "uvicorn", "anyio", "starlette"]
    for pkg in required:
        ok, _, _ = run_cmd(f"{venv_python} -c 'import {pkg}'")
        if ok:
            print(check(f"{pkg} installed"))
        else:
            print(fail(f"{pkg} NOT installed"))
            issues.append(f"missing_{pkg}")

    return issues

def check_mcp_config():
    """Check .mcp.json configuration"""
    header("MCP Configuration")

    issues = []

    if not MCP_CONFIG.exists():
        print(warn(".mcp.json not found (will be created on first run)"))
        return issues

    try:
        config = json.loads(MCP_CONFIG.read_text())
        servers = config.get("mcpServers", {})

        if "graperoot-pro" not in servers:
            print(warn("graperoot-pro not in .mcp.json"))
            return issues

        grp = servers["graperoot-pro"]
        args = grp.get("args", [])

        # Check version in config
        version_found = None
        for arg in args:
            if "v7.5.py" in str(arg):
                version_found = "v7.5"
            elif "v7.4.py" in str(arg):
                version_found = "v7.4"

        if version_found == "v7.5":
            print(check(".mcp.json references v7.5 ✓"))
        elif version_found == "v7.4":
            print(warn(".mcp.json references v7.4 (needs update)"))
            issues.append("mcp_config_old_version")
        else:
            print(warn("Version unclear in .mcp.json"))

    except Exception as e:
        print(fail(f".mcp.json parse error: {e}"))
        issues.append("mcp_config_parse_error")

    return issues

def check_orphan_servers():
    """Check for orphaned MCP server processes"""
    header("Server Processes")

    issues = []
    ok, stdout, _ = run_cmd("ps aux | grep mcp_graph_server | grep -v grep")

    if ok and stdout.strip():
        lines = [l for l in stdout.strip().split('\n') if l]
        print(warn(f"Found {len(lines)} running MCP server(s)"))
        for line in lines:
            parts = line.split()
            if len(parts) > 1:
                print(f"  PID {parts[1]}: {' '.join(parts[10:])}")
        issues.append("orphan_servers")
    else:
        print(check("No orphan servers"))

    return issues

def check_hooks():
    """Check for Claude Code hooks that might block stdio"""
    header("Claude Code Hooks")

    issues = []

    if not CLAUDE_HOOKS.exists():
        print(check("No hooks directory (good)"))
        return issues

    hooks = list(CLAUDE_HOOKS.glob("*"))
    if not hooks:
        print(check("Hooks directory empty (good)"))
        return issues

    print(warn(f"Found {len(hooks)} hook(s):"))
    for hook in hooks:
        print(f"  • {hook.name}")

    print("\n  Hooks can interfere with MCP stdio communication.")
    print("  If MCP times out, try temporarily disabling:")
    print(f"    mv {CLAUDE_HOOKS} {CLAUDE_HOOKS.parent}/hooks.disabled")

    issues.append("hooks_present")
    return issues

def test_server_startup():
    """Try to start the MCP server manually"""
    header("Server Startup Test")

    issues = []
    venv_python = PRO_HOME / "venv" / "bin" / "python3"
    server = PRO_HOME / "mcp_graph_server_v7.5.py"

    if not server.exists():
        print(warn("Skipping (v7.5 server not found)"))
        return issues

    print("Testing server startup (5 second timeout)...")

    cmd = f"{venv_python} {server} --stdio"
    ok, stdout, stderr = run_cmd(f"timeout 5 {cmd}", timeout=6)

    if "timeout" in stderr:
        print(check("Server started (timed out waiting, which is expected)"))
    elif ok or "Traceback" not in stderr:
        print(check("Server started successfully"))
    else:
        print(fail("Server crashed on startup"))
        print(f"\n  Error: {stderr[:200]}")
        issues.append("server_crash")

    return issues

# ─────────────────────────────────────────────────────────────────────────────
# Auto-fixes
# ─────────────────────────────────────────────────────────────────────────────

def fix_launch_py_version():
    """Update launch.py from v7.4 to v7.5"""
    launch_py = PRO_HOME / "launch.py"
    if not launch_py.exists():
        return False

    content = launch_py.read_text()
    if "v7.4.py" not in content:
        return False

    new_content = content.replace("v7.4.py", "v7.5.py")
    launch_py.write_text(new_content)
    print(check("Updated launch.py: v7.4 → v7.5"))
    return True

def fix_mcp_config_version():
    """Update .mcp.json from v7.4 to v7.5"""
    if not MCP_CONFIG.exists():
        return False

    try:
        config = json.loads(MCP_CONFIG.read_text())
        servers = config.get("mcpServers", {})

        if "graperoot-pro" not in servers:
            return False

        grp = servers["graperoot-pro"]
        args = grp.get("args", [])

        updated = False
        for i, arg in enumerate(args):
            if "v7.4.py" in str(arg):
                args[i] = str(arg).replace("v7.4.py", "v7.5.py")
                updated = True

        if updated:
            MCP_CONFIG.write_text(json.dumps(config, indent=2))
            print(check("Updated .mcp.json: v7.4 → v7.5"))
            return True
    except Exception:
        pass

    return False

def fix_orphan_servers():
    """Kill orphaned MCP server processes"""
    ok, stdout, _ = run_cmd("ps aux | grep mcp_graph_server | grep -v grep")

    if not ok or not stdout.strip():
        return False

    ok, _, _ = run_cmd("pkill -f mcp_graph_server")
    if ok:
        print(check("Killed orphan server processes"))
        time.sleep(1)
        return True
    return False

def fix_missing_deps():
    """Install missing Python dependencies"""
    venv_pip = PRO_HOME / "venv" / "bin" / "pip"
    if not venv_pip.exists():
        return False

    print("Installing missing dependencies...")
    ok, _, _ = run_cmd(f"{venv_pip} install -q mcp uvicorn anyio starlette", timeout=60)
    if ok:
        print(check("Installed Python dependencies"))
        return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GrapeRoot Pro Health Check & Auto-Fix")
    parser.add_argument("--fix", action="store_true", help="Auto-fix detected issues")
    parser.add_argument("--check-mcp", action="store_true", help="Only check MCP connectivity")
    args = parser.parse_args()

    print(f"\n{color('GrapeRoot Pro Doctor', Colors.BOLD + Colors.BLUE)}")
    print(f"{color('━' * 60, Colors.BLUE)}\n")

    all_issues = []

    # Run diagnostics
    all_issues.extend(check_installation())
    all_issues.extend(check_python_deps())
    all_issues.extend(check_mcp_config())
    all_issues.extend(check_orphan_servers())
    all_issues.extend(check_hooks())
    all_issues.extend(test_server_startup())

    # Summary
    header("Summary")

    if not all_issues:
        print(f"{color('✓ No issues found!', Colors.GREEN + Colors.BOLD)}")
        print("\nIf MCP still won't connect:")
        print("  1. Restart Claude Code (Cmd+Q, then reopen)")
        print("  2. Try: mv ~/.claude/hooks ~/.claude/hooks.disabled")
        return 0

    print(f"Found {len(all_issues)} issue(s):\n")
    for issue in set(all_issues):
        print(f"  • {issue}")

    # Auto-fix
    if args.fix:
        header("Auto-Fix")

        fixed = []

        if "launch_py_old_version" in all_issues:
            if fix_launch_py_version():
                fixed.append("launch.py version")

        if "mcp_config_old_version" in all_issues:
            if fix_mcp_config_version():
                fixed.append(".mcp.json version")

        if "orphan_servers" in all_issues:
            if fix_orphan_servers():
                fixed.append("orphan servers")

        if any("missing_" in i for i in all_issues):
            if fix_missing_deps():
                fixed.append("Python dependencies")

        if fixed:
            print(f"\n{color('✓ Fixed:', Colors.GREEN)} {', '.join(fixed)}")
            print(f"\n{color('Next step:', Colors.BOLD)} Restart Claude Code (Cmd+Q, reopen)")
        else:
            print(warn("No auto-fixes available for detected issues"))
            print("\nManual steps:")
            if "hooks_present" in all_issues:
                print(f"  mv {CLAUDE_HOOKS} {CLAUDE_HOOKS.parent}/hooks.disabled")
            if "missing_install" in all_issues:
                print("  Reinstall: curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- YOUR-KEY")
    else:
        print(f"\n{color('To auto-fix:', Colors.YELLOW)} Run with --fix flag:")
        print(f"  python3 {Path(__file__).name} --fix")

    return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(1)
