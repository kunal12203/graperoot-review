#!/usr/bin/env bash
# Install grp-admin CLI into ~/.local/bin (on PATH) and run one-time setup.
set -Eeuo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

ln -sfn "$TOOLS_DIR/grp-admin" "$BIN_DIR/grp-admin"
chmod +x "$TOOLS_DIR/grp-admin"
echo "✓ Symlinked $BIN_DIR/grp-admin → $TOOLS_DIR/grp-admin"

# Ensure ~/.local/bin is on PATH for the current shell + future shells
SHELL_RC="$HOME/.zshrc"
[[ "${SHELL:-}" == */bash ]] && SHELL_RC="$HOME/.bash_profile"
if ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
  echo '' >> "$SHELL_RC"
  echo '# Added by graperoot grp-admin installer' >> "$SHELL_RC"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
  echo "✓ Added $BIN_DIR to PATH in $SHELL_RC"
fi
export PATH="$HOME/.local/bin:$PATH"

echo
echo "Running one-time token setup…"
echo
if grp-admin info 2>/dev/null | grep -q "Token present:  yes"; then
  echo "Token already stored. Use  grp-admin info  to inspect, or"
  echo "                            grp-admin setup --force  to replace."
else
  grp-admin setup
fi

echo
echo "Verify with:"
echo "  grp-admin info"
echo "  grp-admin smoke"
