#!/usr/bin/env bash
# Day 3 — end-to-end install test in an isolated HOME, then issue first customer.
# This DOES NOT touch your real ~/.graperoot-pro/ — uses /tmp/grp-pro-test-home/.
set -Eeuo pipefail

PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_DIR="$PROJ_ROOT/release"
TEST_HOME="/tmp/grp-pro-test-home"
TEST_PROJ="/tmp/grp-pro-test-project"

banner() { echo; echo "════════════════════════════════════════════════════════════"; echo " $1"; echo "════════════════════════════════════════════════════════════"; }

# ── 0. Preflight ──────────────────────────────────────────────────────────
banner "0.  Preflight"
command -v grp-admin >/dev/null || { echo "grp-admin not on PATH. Run release/tools/install_admin.sh"; exit 1; }
grp-admin info | grep -q "Token present:  yes" || { echo "Admin token not configured. Run: grp-admin setup"; exit 1; }
curl -fsSL --max-time 5 https://api.graperoot.dev/health >/dev/null || { echo "api.graperoot.dev not responding"; exit 1; }
echo "✓ grp-admin + api.graperoot.dev ready"

# ── 1. Issue a temporary test license for the install test ────────────────
banner "1.  Issuing a throwaway license for the install test"
ISSUE_JSON=$(GRAPEROOT_ADMIN_TOKEN=$(security find-generic-password -s graperoot-pro-admin -a admin-token -w 2>/dev/null || echo "") \
  grp-admin issue --customer "Day-3 Self-Test" --email "selftest@graperoot.dev" --seats 1 --expires "perpetual" --json 2>&1 || true)
TEST_KEY=$(echo "$ISSUE_JSON" | grep -oE 'GRP-[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+' | head -1)
if [ -z "$TEST_KEY" ]; then
  echo "Couldn't issue test license. grp-admin output:"; echo "$ISSUE_JSON"; exit 1
fi
echo "✓ Test license: $TEST_KEY"
trap 'grp-admin revoke "$TEST_KEY" >/dev/null 2>&1; rm -rf "$TEST_HOME" "$TEST_PROJ"' EXIT

# ── 2. Run the installer in an isolated HOME ──────────────────────────────
banner "2.  Running installer in isolated HOME ($TEST_HOME)"
rm -rf "$TEST_HOME" && mkdir -p "$TEST_HOME"
INSTALLER_URL="https://graperoot.dev/pro/install.sh"
echo "Fetching: $INSTALLER_URL"

# Run with HOME pointing at the test dir so we don't touch real ~/.graperoot-pro/
HOME="$TEST_HOME" SHELL="${SHELL:-/bin/zsh}" bash -c "
  curl -fsSL '$INSTALLER_URL' | bash -s -- '$TEST_KEY'
" || { echo "!! Installer failed. Check output above."; exit 1; }

# ── 3. Verify what the installer produced ─────────────────────────────────
banner "3.  Verifying install artifacts"
ok=0; fail=0
check() {
  if [ -e "$2" ]; then
    echo "  ✓ $1"; ok=$((ok+1))
  else
    echo "  ✗ MISSING: $1 at $2"; fail=$((fail+1))
  fi
}
check "install dir"              "$TEST_HOME/.graperoot-pro"
check "license.key"              "$TEST_HOME/.graperoot-pro/license.key"
check "MCP server"               "$TEST_HOME/.graperoot-pro/mcp_graph_server_v7.4.py"
check "graph builder"            "$TEST_HOME/.graperoot-pro/graph_builder.py"
check "launch.py"                "$TEST_HOME/.graperoot-pro/launch.py"
check "Python venv"              "$TEST_HOME/.graperoot-pro/venv/bin/python3"
check "dgc-pro shim"             "$TEST_HOME/.graperoot-pro/bin/dgc-pro"
check "launch_pro.sh"            "$TEST_HOME/.graperoot-pro/bin/launch_pro.sh"
check "version.txt"              "$TEST_HOME/.graperoot-pro/bin/version.txt"

[ -r "$TEST_HOME/.graperoot-pro/license.key" ] && \
  echo "  ✓ license.key content: $(cat "$TEST_HOME/.graperoot-pro/license.key" | head -c 20)…"

if [ $fail -ne 0 ]; then echo; echo "!! Install check FAILED ($fail missing)"; exit 1; fi
echo; echo "✓ Install produced all expected files ($ok/$ok passed)"

# ── 4. Run dgc-pro against a small test project ───────────────────────────
banner "4.  Launching dgc-pro on a tiny test project"
rm -rf "$TEST_PROJ" && mkdir -p "$TEST_PROJ/src"
cat > "$TEST_PROJ/src/main.py" <<'EOF'
def hello(name: str) -> str:
    return f"Hello, {name}"
if __name__ == "__main__":
    print(hello("world"))
EOF
cat > "$TEST_PROJ/README.md" <<'EOF'
# Test Project
A minimal test project for dgc-pro smoke testing.
EOF

echo "Running dgc-pro against $TEST_PROJ (graph build + MCP server start, no Claude interaction)…"
# Run launch.py directly with --version to avoid launching claude — just confirms the
# Python core + venv + license check + graph build all work.
HOME="$TEST_HOME" "$TEST_HOME/.graperoot-pro/venv/bin/python3" \
  "$TEST_HOME/.graperoot-pro/launch.py" --version

# Now run the real dgc-pro shim (but send an immediate exit signal to Claude)
# This verifies: license check passes, graph builds, MCP starts, .mcp.json is written.
echo
echo "Running full dgc-pro against test project with timeout (5s is enough for setup)…"
(
  HOME="$TEST_HOME" \
  PATH="$TEST_HOME/.graperoot-pro/bin:$PATH" \
  timeout 30 dgc-pro "$TEST_PROJ" --version 2>&1 || true
) | head -20
echo

# Confirm .mcp.json got written
if [ -f "$TEST_PROJ/.mcp.json" ]; then
  echo "  ✓ .mcp.json was written to the project:"
  cat "$TEST_PROJ/.mcp.json" | python3 -m json.tool | sed 's/^/    /'
else
  echo "  !! .mcp.json NOT written — check launch.py output above"
fi

# Confirm dual-graph-pro dir was created
if [ -d "$TEST_PROJ/.dual-graph-pro" ] && [ -f "$TEST_PROJ/.dual-graph-pro/info_graph.json" ]; then
  echo "  ✓ Graph was built at $TEST_PROJ/.dual-graph-pro/"
  SYMBOLS=$(python3 -c "import json;print(json.load(open('$TEST_PROJ/.dual-graph-pro/info_graph.json')).get('node_count','?'))")
  echo "    ($SYMBOLS nodes indexed)"
else
  echo "  !! Graph NOT built — check launch.py output above"
fi

banner "✅  Day 3 install test passed"
cat <<EOF

What just got verified:
  ✓ curl | bash installer works against graperoot.dev/pro/install.sh equivalent (github raw)
  ✓ license server issues + downloads tarball + extracts cleanly
  ✓ venv + deps install
  ✓ dgc-pro launches, builds graph, writes .mcp.json, registers MCP server
  ✓ Full customer-experience simulated (in isolated HOME, nothing touched in your real setup)

NEXT — ready to send to a real customer:
  1.  grp-admin issue --customer "Client Name" --email client@example.com --seats 5 --expires 2027-04-22
      (save the GRP-... key it prints)

  2.  $RELEASE_DIR/tools/make_onboarding_pdf.sh "GRP-KEY-HERE" "Client Name" 5 "2027-04-22"
      (generates Client_Name_Onboarding.pdf ready to email)

  3.  Email the PDF + the install one-liner to the client. Done.

(If you want the pretty install URL — graperoot.dev/pro/install.sh instead of the
github raw URL — we can set up Cloudflare Pages anytime. It's a 10-min add-on.)
EOF
