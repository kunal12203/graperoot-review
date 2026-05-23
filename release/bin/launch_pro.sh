#!/usr/bin/env bash
# GrapeRoot Pro — runtime launcher (Unix)
# Called by `dgc-pro` shim. Responsibilities:
#   1. Self-update (check remote version.txt, pull newer core tarball if available)
#   2. Periodic license re-verification (every 24h online; 7d offline grace)
#   3. Build dual-graph index for the target project on first run
#   4. Start MCP server on a free port, register .mcp.json, launch claude
#   5. Clean up MCP server on exit

set -Eeuo pipefail

INSTALL_DIR="${GRAPEROOT_PRO_HOME:-$HOME/.graperoot-pro}"
BIN_DIR="$INSTALL_DIR/bin"
VENV_PY="$INSTALL_DIR/venv/bin/python3"
LICENSE_FILE="$INSTALL_DIR/license.key"
CACHE_FILE="$INSTALL_DIR/.license_cache"
R2="${GRAPEROOT_PRO_R2:-https://pub-pro-r2.graperoot.dev}"
API="${GRAPEROOT_PRO_API:-https://api.graperoot.dev}"
BASE_URL="${GRAPEROOT_PRO_GH:-https://raw.githubusercontent.com/kunal12203/graperoot-pro-public/main}"

# ── Self-update ────────────────────────────────────────────────────────────
# Fast: check version.txt with a 3s timeout; if newer, pull tarball out-of-band.
# Matches the free launcher's pattern.
self_update() {
  local local_ver remote_ver
  local_ver="$(cat "$BIN_DIR/version.txt" 2>/dev/null || echo 0)"
  remote_ver="$(curl -sf --max-time 3 "$R2/bin/version.txt" 2>/dev/null \
    || curl -sf --max-time 3 "$BASE_URL/bin/version.txt" 2>/dev/null || echo 0)"
  [[ -z "$remote_ver" || "$remote_ver" == "$local_ver" ]] && return 0

  # Compare using dotted version sort
  local newer
  newer=$(printf "%s\n%s\n" "$local_ver" "$remote_ver" | sort -V | tail -1)
  [[ "$newer" != "$remote_ver" ]] && return 0

  echo "[update] GrapeRoot Pro $local_ver → $remote_ver" >&2
  # License-gated download of new bundle
  if [[ -f "$LICENSE_FILE" ]]; then
    local dl_url
    dl_url=$(_verify_online | "$VENV_PY" -c 'import json,sys;d=json.load(sys.stdin);print(d.get("download_url","")) if d.get("valid") else ""' 2>/dev/null || true)
    if [[ -n "$dl_url" ]]; then
      local tmp; tmp="$(mktemp -t grp-pro.XXXXXX.tgz)"
      if curl -fsSL --max-time 60 "$dl_url" -o "$tmp" 2>/dev/null; then
        tar -xzf "$tmp" -C "$INSTALL_DIR" --strip-components=1 2>/dev/null || true
        rm -f "$tmp"
        # Also refresh launcher binaries — R2 first, GitHub fallback (R2 may not be set up)
        for f in launch_pro.sh dgc-pro version.txt changelog.txt; do
          if curl -fsSL --max-time 15 "$R2/bin/$f" -o "$BIN_DIR/$f.new" 2>/dev/null && [ -s "$BIN_DIR/$f.new" ]; then
            : # fetched from R2
          elif curl -fsSL --max-time 15 "$BASE_URL/bin/$f" -o "$BIN_DIR/$f.new" 2>/dev/null && [ -s "$BIN_DIR/$f.new" ]; then
            : # fetched from GitHub fallback
          else
            rm -f "$BIN_DIR/$f.new"
            continue
          fi
          mv "$BIN_DIR/$f.new" "$BIN_DIR/$f"
        done
        chmod +x "$BIN_DIR/launch_pro.sh" "$BIN_DIR/dgc-pro" 2>/dev/null || true
        # Show changelog
        [[ -f "$BIN_DIR/changelog.txt" ]] && head -20 "$BIN_DIR/changelog.txt" >&2
      fi
    fi
  fi
}

# ── License verification ───────────────────────────────────────────────────
# v1.0.9: drop -f so 4xx (revoked/expired/unknown) returns the body instead of empty.
# Empty body now exclusively means "could not connect" → falls into 7-day offline grace.
# Body-with-valid:false → real rejection → exit with proper reason (not "offline").
_verify_online() {
  local key; key="$(cat "$LICENSE_FILE" 2>/dev/null)"
  [[ -z "$key" ]] && return 1
  local extra=()
  [[ -n "${GRAPEROOT_CA_BUNDLE:-}" && -f "${GRAPEROOT_CA_BUNDLE}" ]] && extra+=(--cacert "$GRAPEROOT_CA_BUNDLE")
  local tmp; tmp="$(mktemp -t grp-rt.XXXXXX 2>/dev/null || mktemp)"
  local code; code=$(curl -sSL -o "$tmp" -w "%{http_code}" \
    -X POST "$API/v1/license/verify" \
    -H "Content-Type: application/json" \
    -d "{\"license_key\":\"$key\",\"host\":\"$(hostname 2>/dev/null || echo unknown)\",\"os\":\"$(uname -s)\"}" \
    --max-time 10 \
    ${extra[@]+"${extra[@]}"} 2>/dev/null) || code=""
  # Network-layer failure → empty stdout (caller treats as offline)
  if [[ -z "$code" || "$code" == "000" ]]; then
    rm -f "$tmp"
    return 1
  fi
  cat "$tmp"
  rm -f "$tmp"
}

check_license() {
  if [[ ! -f "$LICENSE_FILE" ]]; then
    echo "[error] GrapeRoot Pro: license missing. Re-run the installer." >&2
    exit 1
  fi
  local now; now=$(date +%s)
  # Use cached result if < 24h old
  if [[ -f "$CACHE_FILE" ]]; then
    local cached_ts cached_valid
    cached_ts=$("$VENV_PY" -c "import json;d=json.load(open('$CACHE_FILE'));print(int(d.get('ts',0)))" 2>/dev/null || echo 0)
    cached_valid=$("$VENV_PY" -c "import json;d=json.load(open('$CACHE_FILE'));print(1 if d.get('valid') else 0)" 2>/dev/null || echo 0)
    local age=$((now - cached_ts))
    if [[ "$cached_valid" == "1" && $age -lt 86400 ]]; then return 0; fi
  fi
  # Online re-verify
  local resp; resp=$(_verify_online || true)
  if [[ -z "$resp" ]]; then
    # Offline grace — allow if last-good cache is < 7d old
    if [[ -f "$CACHE_FILE" ]]; then
      local cached_ts; cached_ts=$("$VENV_PY" -c "import json;d=json.load(open('$CACHE_FILE'));print(int(d.get('ts',0)))" 2>/dev/null || echo 0)
      if [[ $((now - cached_ts)) -lt 604800 ]]; then
        echo "[dgc-pro] license re-verify offline; running on 7-day grace" >&2
        return 0
      fi
    fi
    echo "[error] GrapeRoot Pro: license cannot be verified (offline > 7d). Reconnect and retry." >&2
    exit 1
  fi
  # Check validity, update cache
  local valid; valid=$("$VENV_PY" -c "import json,sys;print(1 if json.loads('''$resp''').get('valid') else 0)" 2>/dev/null || echo 0)
  if [[ "$valid" != "1" ]]; then
    local reason; reason=$("$VENV_PY" -c "import json,sys;print(json.loads('''$resp''').get('reason','unknown'))" 2>/dev/null || echo unknown)
    echo "[error] GrapeRoot Pro: license rejected — $reason" >&2
    echo "        Support: support@graperoot.dev" >&2
    exit 1
  fi
  "$VENV_PY" -c "import json,sys;d=json.loads('''$resp''');d['ts']=$now;open('$CACHE_FILE','w').write(json.dumps(d))" 2>/dev/null || true
}

# ── Run ────────────────────────────────────────────────────────────────────
self_update || true
check_license

PROJECT="${1:-.}"; shift || true

# Delegate to Python core for the real work (MCP start, graph build, claude launch)
export GRAPEROOT_PRO_HOME="$INSTALL_DIR"
exec "$VENV_PY" "$INSTALL_DIR/launch.py" "$PROJECT" "$@"
