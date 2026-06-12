#!/usr/bin/env bash
# GrapeRoot Pro — one-time setup (macOS / Linux)
# Usage:  curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX

set -euo pipefail

INSTALL_DIR="$HOME/.graperoot-pro"
VENV="$INSTALL_DIR/venv"
FREE_DIR="$HOME/.dual-graph"
R2="${GRAPEROOT_PRO_R2:-https://pub-pro-r2.graperoot.dev}"
API="${GRAPEROOT_PRO_API:-https://api.graperoot.dev}"
BASE_URL="${GRAPEROOT_PRO_GH:-https://raw.githubusercontent.com/kunal12203/graperoot-pro-public/main}"

LICENSE_KEY="${1:-${GRAPEROOT_LICENSE_KEY:-}}"

# ══════════════════════════════════════════════════════════════════════════════
# BRANDING
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           GrapeRoot Pro — Installer  ·  v1.0                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

if [[ -z "$LICENSE_KEY" ]]; then
  echo "[error] License key required."
  echo ""
  echo "  Usage:  curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX"
  echo ""
  echo "  No license? Purchase at https://graperoot.dev/pro or email sales@graperoot.dev"
  exit 1
fi

confirm() {
  # Three modes:
  #   1. Interactive TTY → prompt Y/n, read user answer
  #   2. No TTY (CI, curl|bash piped, SSH -T) → default YES (user invoked the installer)
  #   3. Opt-out via env: GRAPEROOT_SKIP_OPTIONAL=1 → default NO
  #
  # TTY probe: `{ : </dev/tty; } 2>/dev/null` opens /dev/tty for reading via no-op `:`,
  # with stderr redirected so "Device not configured" errors never leak to output.
  local answer=""
  if { : </dev/tty; } 2>/dev/null; then
    printf "%s [Y/n] " "$1"
    read -r answer </dev/tty 2>/dev/null || answer=""
  else
    if [ "${GRAPEROOT_SKIP_OPTIONAL:-0}" = "1" ]; then
      echo "$1 [skipped — GRAPEROOT_SKIP_OPTIONAL=1]"
      return 1
    fi
    echo "$1 [Y/n] (auto-Y, no tty)"
    answer="Y"
  fi
  case "$answer" in [Nn]*) return 1 ;; *) return 0 ;; esac
}

# ══════════════════════════════════════════════════════════════════════════════
# PREREQUISITES — match free installer's approach (Python 3.10+, Node, Claude)
# ══════════════════════════════════════════════════════════════════════════════
OS_TYPE="$(uname -s)"
PYTHON=""
for py in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    if "$py" -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
      PYTHON="$py"; break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo "[check] Python 3.10+ is NOT installed."
  case "$OS_TYPE" in
    Darwin*)
      if command -v brew >/dev/null 2>&1 && confirm "[check] Install Python 3.11 via Homebrew?"; then
        brew install python@3.11 </dev/null && PYTHON="python3.11"
      else
        echo "  Install manually:  brew install python@3.11   (or download from https://python.org)"
        exit 1
      fi
      ;;
    Linux*)
      if command -v apt-get >/dev/null 2>&1 && confirm "[check] Install Python via apt? (sudo)"; then
        sudo apt-get update </dev/null && sudo apt-get install -y python3.11 python3.11-venv </dev/null && PYTHON="python3.11"
      elif command -v dnf >/dev/null 2>&1 && confirm "[check] Install Python via dnf? (sudo)"; then
        sudo dnf install -y python3.11 </dev/null && PYTHON="python3.11"
      else
        echo "  Install Python 3.10+ manually, then re-run the installer."
        exit 1
      fi
      ;;
    *)
      echo "  Unsupported OS: $OS_TYPE"
      exit 1
      ;;
  esac
fi
echo "[check] Python:       $($PYTHON --version)"

if ! command -v rg >/dev/null 2>&1; then
  echo "[check] ripgrep:      NOT FOUND"
  case "$OS_TYPE" in
    Darwin*)
      if command -v brew >/dev/null 2>&1 && confirm "[check] Install ripgrep via Homebrew?"; then
        brew install ripgrep </dev/null || echo "[warn] brew install ripgrep failed; install manually later."
      else
        echo "[warn] Install later:  brew install ripgrep   (needed for fallback_rg / graph_grep_all)"
      fi
      ;;
    Linux*)
      if command -v apt-get >/dev/null 2>&1 && confirm "[check] Install ripgrep via apt? (sudo)"; then
        sudo apt-get install -y ripgrep </dev/null || echo "[warn] apt install ripgrep failed; install manually later."
      elif command -v dnf >/dev/null 2>&1 && confirm "[check] Install ripgrep via dnf? (sudo)"; then
        sudo dnf install -y ripgrep </dev/null || echo "[warn] dnf install ripgrep failed; install manually later."
      elif command -v pacman >/dev/null 2>&1 && confirm "[check] Install ripgrep via pacman? (sudo)"; then
        sudo pacman -S --noconfirm ripgrep </dev/null || echo "[warn] pacman install ripgrep failed; install manually later."
      else
        echo "[warn] Install ripgrep from your package manager (needed for fallback_rg / graph_grep_all)"
      fi
      ;;
  esac
else
  echo "[check] ripgrep:      $(rg --version 2>/dev/null | head -1)"
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[check] Node.js:      NOT FOUND"
  case "$OS_TYPE" in
    Darwin*)
      if command -v brew >/dev/null 2>&1 && confirm "[check] Install Node.js (LTS) via Homebrew?"; then
        brew install node </dev/null || echo "[warn] brew install node failed; install manually later."
      else
        echo "[warn] Install later:  brew install node   (Claude Code requires Node 18+)"
      fi
      ;;
    Linux*)
      if command -v apt-get >/dev/null 2>&1 && confirm "[check] Install Node.js via apt? (sudo)"; then
        sudo apt-get install -y nodejs npm </dev/null || echo "[warn] apt install nodejs failed; install manually later."
      elif command -v dnf >/dev/null 2>&1 && confirm "[check] Install Node.js via dnf? (sudo)"; then
        sudo dnf install -y nodejs npm </dev/null || echo "[warn] dnf install nodejs failed; install manually later."
      elif command -v pacman >/dev/null 2>&1 && confirm "[check] Install Node.js via pacman? (sudo)"; then
        sudo pacman -S --noconfirm nodejs npm </dev/null || echo "[warn] pacman install nodejs failed; install manually later."
      else
        echo "[warn] Install Node.js 18+ from https://nodejs.org, then re-run for Claude Code install."
      fi
      ;;
  esac
fi
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1)
  if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 18 ] 2>/dev/null; then
    echo "[warn] Node $(node -v) is older than v18; Claude Code may fail. Upgrade recommended."
  else
    echo "[check] Node.js:      $(node -v)"
  fi
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "[check] Claude Code:  NOT FOUND"
  if command -v npm >/dev/null 2>&1 && confirm "[check] Install Claude Code via npm?"; then
    npm install -g @anthropic-ai/claude-code </dev/null 2>/dev/null || sudo npm install -g @anthropic-ai/claude-code </dev/null 2>/dev/null || true
    command -v claude >/dev/null 2>&1 && echo "[check] Claude Code:  installed" || echo "[warn] Claude Code install failed; install manually later: npm i -g @anthropic-ai/claude-code"
  else
    echo "[warn] Install later:  npm install -g @anthropic-ai/claude-code   (needs Node 18+)"
  fi
else
  echo "[check] Claude Code:  $(claude --version 2>/dev/null | head -1 || echo 'installed')"
fi

# ══════════════════════════════════════════════════════════════════════════════
# COEXISTENCE — detect free install, install Pro alongside (never touch it)
# ══════════════════════════════════════════════════════════════════════════════
if [[ -d "$FREE_DIR" ]]; then
  echo "[check] GrapeRoot Free detected at $FREE_DIR — Pro will install alongside (free install untouched)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# LICENSE VERIFY — precise diagnostics on failure (network vs server vs key)
# ══════════════════════════════════════════════════════════════════════════════
# v1.0.12: previously any 4xx/curl-fail collapsed into "License server unreachable".
# Customers couldn't tell typo'd-key from corp-firewall block. Now we distinguish:
#   - curl exit 6/7/28/TLS  → real network problem, with IT-whitelist instructions
#   - HTTP 4xx + body       → server reached, show the body's "reason" field
verify_license() {
  local http_code curl_exit=0 attempt=0 max_attempts=3 backoff
  local extra_args=()

  if [[ -n "${GRAPEROOT_CA_BUNDLE:-}" && -f "${GRAPEROOT_CA_BUNDLE}" ]]; then
    echo "[verify] Using custom CA bundle: $GRAPEROOT_CA_BUNDLE"
    extra_args+=(--cacert "$GRAPEROOT_CA_BUNDLE")
  fi
  if [[ -n "${HTTPS_PROXY:-${https_proxy:-}}" ]]; then
    echo "[verify] Routing via proxy: ${HTTPS_PROXY:-$https_proxy}"
  fi

  while [[ $attempt -lt $max_attempts ]]; do
    attempt=$((attempt + 1))
    local tmpfile; tmpfile="$(mktemp -t grp-verify.XXXXXX 2>/dev/null || mktemp)"
    http_code=$(curl -sSL -o "$tmpfile" -w "%{http_code}" \
      -X POST "$API/v1/license/verify" \
      -H "Content-Type: application/json" \
      -d "{\"license_key\":\"$LICENSE_KEY\",\"host\":\"$(hostname 2>/dev/null || echo unknown)\",\"os\":\"$OS_TYPE\"}" \
      --max-time 15 \
      ${extra_args[@]+"${extra_args[@]}"} 2>/dev/null) && curl_exit=0 || curl_exit=$?
    VERIFY_JSON="$(cat "$tmpfile" 2>/dev/null || echo '')"
    rm -f "$tmpfile"

    if [[ "$curl_exit" == "0" && -n "$http_code" && "$http_code" != "000" ]]; then
      VERIFY_HTTP="$http_code"
      return 0
    fi

    if [[ $attempt -lt $max_attempts ]]; then
      backoff=$((attempt * 5))
      echo "[verify] Attempt $attempt: cannot reach $API (curl exit $curl_exit). Retrying in ${backoff}s..." >&2
      sleep "$backoff"
    fi
  done

  local host; host="$(echo "$API" | sed -e 's|^https*://||' -e 's|^http*://||' -e 's|/.*||')"
  echo "" >&2
  echo "[error] Cannot reach license server after $max_attempts attempts." >&2
  echo "" >&2
  case "$curl_exit" in
    6)  echo "  Cause: DNS lookup for $host failed." >&2
        echo "  Likely: corporate DNS filter blocks the .dev TLD, or you're offline." >&2
        echo "  Test:  nslookup $host" >&2 ;;
    7)  echo "  Cause: TCP connection refused." >&2
        echo "  Likely: corporate firewall blocks outbound HTTPS to Cloudflare." >&2 ;;
    28) echo "  Cause: connection timed out (server did not respond within 15s)." >&2
        echo "  Likely: corporate firewall silently dropping packets to $host." >&2 ;;
    35|51|58|59|60|77)
        echo "  Cause: TLS / certificate validation failed (curl exit $curl_exit)." >&2
        echo "  Likely: SSL-inspecting corporate proxy with a custom CA." >&2
        echo "  Fix:   export GRAPEROOT_CA_BUNDLE=/path/to/corp-ca.pem  and re-run." >&2 ;;
    *)  echo "  Cause: curl exit $curl_exit (https://everything.curl.dev/usingcurl/returns)" >&2 ;;
  esac
  cat <<EOF >&2

  -- Send to your IT team to whitelist --
  HTTPS allow:  api.graperoot.dev, graperoot.dev, pub-pro-r2.graperoot.dev
  IP allow:     104.21.91.161, 172.67.175.90  (Cloudflare, may rotate)

  -- If you have a corporate proxy --
  export HTTPS_PROXY=http://your-proxy:8080
  export NO_PROXY=localhost,127.0.0.1
  export GRAPEROOT_CA_BUNDLE=/path/to/corp-ca.pem  # only if SSL inspection
  Then re-run the installer.

  Support: support@graperoot.dev  (include this whole error message)
EOF
  exit 1
}

echo "[verify] Validating license..."
verify_license

# Server responded. Parse the body and check validity.
_is_valid=$("$PYTHON" -c "import json,sys;d=json.loads(sys.argv[1]);print('1' if d.get('valid') else '0')" "$VERIFY_JSON" 2>/dev/null || echo 0)
if [[ "$_is_valid" != "1" ]]; then
  REASON=$("$PYTHON" -c "import json,sys;print(json.loads(sys.argv[1]).get('reason','invalid response'))" "$VERIFY_JSON" 2>/dev/null || echo "HTTP $VERIFY_HTTP")
  echo "[error] License rejected: $REASON" >&2
  echo "        HTTP $VERIFY_HTTP from $API" >&2
  echo "        Support: support@graperoot.dev" >&2
  exit 1
fi
CUSTOMER=$("$PYTHON" -c "import json,sys;print(json.loads(sys.argv[1]).get('customer','-'))" "$VERIFY_JSON" 2>/dev/null)
EXPIRES=$("$PYTHON"  -c "import json,sys;print(json.loads(sys.argv[1]).get('expires','perpetual'))" "$VERIFY_JSON" 2>/dev/null)
DL_URL=$("$PYTHON"   -c "import json,sys;print(json.loads(sys.argv[1])['download_url'])" "$VERIFY_JSON" 2>/dev/null)
echo "[verify] Valid  ·  $CUSTOMER  ·  expires: $EXPIRES"

# ══════════════════════════════════════════════════════════════════════════════
# INSTALL
# ══════════════════════════════════════════════════════════════════════════════
mkdir -p "$INSTALL_DIR/bin"

# Download bundled package (server, graph_builder, requirements, VERSION)
echo "[install] Downloading GrapeRoot Pro package…"
TMP_TGZ="$(mktemp -t graperoot-pro.XXXXXX.tgz)"
trap 'rm -f "$TMP_TGZ"' EXIT
curl -fsSL "$DL_URL" -o "$TMP_TGZ" --max-time 120
tar -xzf "$TMP_TGZ" -C "$INSTALL_DIR" --strip-components=1
chmod 700 "$INSTALL_DIR"

# Download launcher + shim from R2 (with GitHub fallback)
echo "[install] Downloading launcher…"
for f in launch_pro.sh dgc-pro dg-pro graperoot-pro version.txt changelog.txt; do
  curl -fsSL "$R2/bin/$f" -o "$INSTALL_DIR/bin/$f" 2>/dev/null \
    || curl -fsSL "$BASE_URL/bin/$f" -o "$INSTALL_DIR/bin/$f"
done
chmod +x "$INSTALL_DIR/bin/launch_pro.sh" "$INSTALL_DIR/bin/dgc-pro" "$INSTALL_DIR/bin/dg-pro" "$INSTALL_DIR/bin/graperoot-pro"

# Python venv + dependencies
echo "[install] Creating isolated Python venv…"
"$PYTHON" -m venv "$VENV" </dev/null
"$VENV/bin/pip" install --upgrade pip --quiet </dev/null
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" </dev/null

# Persist license (owner-only)
echo -n "$LICENSE_KEY" > "$INSTALL_DIR/license.key"
chmod 600 "$INSTALL_DIR/license.key"

# PATH — symlink shims into a directory that is already on PATH so commands
# work immediately in any new terminal without sourcing anything.
#
# Priority order:
#   1. ~/.local/bin  — XDG standard; pre-added to PATH by Ubuntu/Debian/WSL
#                      default ~/.profile via `if [ -d "$HOME/.local/bin" ]`
#   2. /usr/local/bin — always on PATH everywhere, requires sudo
#   3. Fallback: write export to shell rc files (last resort)
#
# Symlinking means `dgc-pro`, `dg-pro`, `graperoot-pro` work instantly in
# any new shell — no sourcing, no terminal restart.

SHIMS=(dgc-pro dg-pro graperoot-pro)
SYMLINK_DIR=""

# Try ~/.local/bin first (no sudo, works on Ubuntu/Debian/WSL out of the box)
if mkdir -p "$HOME/.local/bin" 2>/dev/null; then
  SYMLINK_DIR="$HOME/.local/bin"
elif command -v sudo >/dev/null 2>&1 && sudo test -w /usr/local/bin 2>/dev/null; then
  SYMLINK_DIR="/usr/local/bin"
fi

if [[ -n "$SYMLINK_DIR" ]]; then
  for shim in "${SHIMS[@]}"; do
    # Remove stale symlink or old copy before re-creating
    rm -f "$SYMLINK_DIR/$shim" 2>/dev/null || sudo rm -f "$SYMLINK_DIR/$shim" 2>/dev/null || true
    if ln -sf "$INSTALL_DIR/bin/$shim" "$SYMLINK_DIR/$shim" 2>/dev/null || \
       sudo ln -sf "$INSTALL_DIR/bin/$shim" "$SYMLINK_DIR/$shim" 2>/dev/null; then
      : # symlink created
    fi
  done
  echo "[install] Symlinked dgc-pro, dg-pro, graperoot-pro → $SYMLINK_DIR"
fi

# ~/.local/bin is guaranteed on PATH for NEW terminals on Ubuntu/Debian/WSL
# (default ~/.profile checks for it). For the CURRENT shell session we also
# need to add it — export here so commands work without reopening terminal.
if [[ "$SYMLINK_DIR" == "$HOME/.local/bin" ]]; then
  export PATH="$PATH:$HOME/.local/bin"
fi

# Also write to shell rc files as a belt-and-suspenders fallback (covers edge
# cases like minimal containers, non-standard distros, custom ~/.profile).
_add_path_to_rc() {
  local rc="$1" dir="$2"
  if ! grep -q "$dir" "$rc" 2>/dev/null; then
    { echo ""; echo "# GrapeRoot Pro"; echo "export PATH=\"\$PATH:$dir\""; } >> "$rc"
  fi
}

if [[ -n "$SYMLINK_DIR" && "$SYMLINK_DIR" != "$HOME/.local/bin" ]]; then
  : # /usr/local/bin is already on PATH everywhere — nothing to add to rc files
elif [[ -z "$SYMLINK_DIR" ]]; then
  # Could not symlink anywhere — fall back to PATH export in rc files
  if [[ "$OS_TYPE" == "Darwin" ]]; then
    [[ "${SHELL:-}" == */bash ]] \
      && _add_path_to_rc "$HOME/.bash_profile" "$INSTALL_DIR/bin" \
      || _add_path_to_rc "$HOME/.zshrc"        "$INSTALL_DIR/bin"
  else
    _add_path_to_rc "$HOME/.profile" "$INSTALL_DIR/bin"
    [[ -f "$HOME/.bashrc" || "${SHELL:-}" == */bash ]] && _add_path_to_rc "$HOME/.bashrc" "$INSTALL_DIR/bin"
    command -v zsh >/dev/null 2>&1 && _add_path_to_rc "$HOME/.zshrc" "$INSTALL_DIR/bin"
    echo "[install] Added $INSTALL_DIR/bin to PATH in shell rc files"
  fi
fi

VER=$(cat "$INSTALL_DIR/bin/version.txt" 2>/dev/null || echo "1.0.40")
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Install complete.  GrapeRoot Pro v$VER                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# Post-install health check — detect and offer to fix common issues
# ══════════════════════════════════════════════════════════════════════════════
DOCTOR="$INSTALL_DIR/bin/dgc-pro-doctor.py"
if [[ -f "$DOCTOR" && -x "$VENV/bin/python3" ]]; then
  echo "[check] Running post-install diagnostics..."
  if "$VENV/bin/python3" "$DOCTOR" --check-mcp 2>/dev/null; then
    echo "[check] ✓ No issues detected"
  else
    echo ""
    echo "[check] Found potential issues."
    if confirm "[check] Run auto-fix? (kills orphan servers, updates configs)"; then
      "$VENV/bin/python3" "$DOCTOR" --fix || true
      echo ""
      echo "[check] Fixes applied. Restart Claude Code (Cmd+Q, reopen) to reconnect MCP."
    else
      echo "[check] Skipped. Run manually: dgc-pro-doctor --fix"
    fi
  fi
  echo ""
fi

echo "  Commands are ready now. Per project:"
echo "    dgc-pro /path/to/your/project       # Claude Code (default)"
echo "    dg-pro  /path/to/your/project       # OpenAI Codex"
echo "    graperoot-pro --gemini /path/...    # Gemini CLI"
echo "    graperoot-pro --opencode /path/...  # opencode"
echo ""
echo "  Troubleshooting:"
echo "    dgc-pro-doctor          # Diagnose MCP issues"
echo "    dgc-pro-doctor --fix    # Auto-fix common problems"
echo ""
echo "  Docs:    https://graperoot.dev/pro/docs"
echo "  Support: support@graperoot.dev"
echo ""
