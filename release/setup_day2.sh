#!/usr/bin/env bash
# Day 2 setup — build tarball, upload to private release, sync public repo.
# Run once after Day 1 (api.graperoot.dev/health must return valid JSON).
set -Eeuo pipefail

PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_DIR="$PROJ_ROOT/release"
PUBLIC_REPO_DIR="${GRAPEROOT_PRO_PUBLIC_DIR:-/tmp/graperoot-pro-public}"

banner() { echo; echo "════════════════════════════════════════════════════════════"; echo " $1"; echo "════════════════════════════════════════════════════════════"; }

# ── 0. Preflight ──────────────────────────────────────────────────────────
banner "0.  Preflight"
command -v gh >/dev/null   || { echo "brew install gh"; exit 1; }
command -v tar >/dev/null  || { echo "tar required"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh auth login first"; exit 1; }

# Check Day 1 is actually live
if ! curl -fsSL --max-time 5 https://api.graperoot.dev/health >/dev/null 2>&1; then
  echo "!! api.graperoot.dev/health is not responding. Finish Day 1 first."
  exit 1
fi
echo "✓ api.graperoot.dev is live"

VERSION=$(cat "$RELEASE_DIR/bin/version.txt" | tr -d '[:space:]')
echo "✓ Release version: $VERSION"

# ── 1. Build the tarball ──────────────────────────────────────────────────
banner "1.  Building graperoot-pro.tar.gz"
"$RELEASE_DIR/build_tarball.sh" "$VERSION"
TARBALL="$RELEASE_DIR/graperoot-pro.tar.gz"
if [ ! -f "$TARBALL" ]; then
  echo "!! Tarball missing at $TARBALL"; exit 1
fi
SIZE=$(du -h "$TARBALL" | cut -f1)
echo "✓ Built $TARBALL ($SIZE)"

# ── 2. Inspect the tarball ────────────────────────────────────────────────
banner "2.  Tarball contents"
tar -tzf "$TARBALL" | sed 's/^/  /'

# ── 3. Upload to private release repo ─────────────────────────────────────
banner "3.  Creating GitHub release"
TAG="v$VERSION"
if gh release view "$TAG" --repo kunal12203/graperoot-pro-releases >/dev/null 2>&1; then
  echo "• Release $TAG already exists — deleting and re-creating"
  gh release delete "$TAG" --repo kunal12203/graperoot-pro-releases --yes --cleanup-tag
fi
gh release create "$TAG" "$TARBALL" \
  --repo kunal12203/graperoot-pro-releases \
  --title "GrapeRoot Pro $TAG" \
  --notes-file "$RELEASE_DIR/bin/changelog.txt" \
  --prerelease
echo "✓ Published $TAG to kunal12203/graperoot-pro-releases (private)"

# ── 4. Prepare pro-public repo (clone if needed) ──────────────────────────
banner "4.  Syncing pro-public repo"
if [ ! -d "$PUBLIC_REPO_DIR/.git" ]; then
  echo "Cloning pro-public into $PUBLIC_REPO_DIR"
  git clone https://github.com/kunal12203/graperoot-pro-public.git "$PUBLIC_REPO_DIR"
else
  echo "Pulling latest $PUBLIC_REPO_DIR"
  (cd "$PUBLIC_REPO_DIR" && git pull --ff-only || true)
fi

# ── 5. Copy launcher files into pro-public ────────────────────────────────
mkdir -p "$PUBLIC_REPO_DIR/bin"
cp "$RELEASE_DIR/install.sh"              "$PUBLIC_REPO_DIR/install.sh"
cp "$RELEASE_DIR/install.ps1"             "$PUBLIC_REPO_DIR/install.ps1"
cp "$RELEASE_DIR/bin/launch_pro.sh"       "$PUBLIC_REPO_DIR/bin/"
cp "$RELEASE_DIR/bin/launch_pro.ps1"      "$PUBLIC_REPO_DIR/bin/"
cp "$RELEASE_DIR/bin/dgc-pro"             "$PUBLIC_REPO_DIR/bin/"
cp "$RELEASE_DIR/bin/dgc-pro.cmd"         "$PUBLIC_REPO_DIR/bin/"
cp "$RELEASE_DIR/bin/dgc-pro.ps1"         "$PUBLIC_REPO_DIR/bin/"
cp "$RELEASE_DIR/bin/version.txt"         "$PUBLIC_REPO_DIR/bin/"
cp "$RELEASE_DIR/bin/changelog.txt"       "$PUBLIC_REPO_DIR/bin/"

# Minimal README in public repo
cat > "$PUBLIC_REPO_DIR/README.md" <<EOF
# GrapeRoot Pro — Public Launcher Assets

This repo hosts the installer scripts and launcher binaries for GrapeRoot Pro.
The actual Pro engine (MCP server + graph builder) is in a separate **private** repo
and fetched only with a valid license.

Customers run:

\`\`\`bash
# macOS / Linux
curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX

# Windows (PowerShell)
\$env:GRAPEROOT_LICENSE_KEY = "GRP-XXXX-XXXX-XXXX"
irm https://graperoot.dev/pro/install.ps1 | iex
\`\`\`

Buy a license: https://graperoot.dev/pro
Support: support@graperoot.dev
EOF

# ── 6. Commit + push ──────────────────────────────────────────────────────
banner "6.  Committing & pushing pro-public"
cd "$PUBLIC_REPO_DIR"
git add .
if git diff --cached --quiet; then
  echo "• No changes to commit (public repo already up-to-date)"
else
  git commit -m "v$VERSION: launcher + installer scripts"
  git push
  echo "✓ Pushed pro-public"
fi

# ── 7. Smoke test via grp-admin (stores token in Keychain once, never asks again) ──
banner "7.  Smoke test"
cd "$PROJ_ROOT"
# Use locally-installed grp-admin first (if ~/.local/bin is on PATH), fall back to release/tools.
if command -v grp-admin >/dev/null 2>&1; then
  GRP_ADMIN=grp-admin
elif [ -x "$RELEASE_DIR/tools/grp-admin" ]; then
  GRP_ADMIN="$RELEASE_DIR/tools/grp-admin"
else
  echo "!! grp-admin not found. Run:  $RELEASE_DIR/tools/install_admin.sh"
  exit 1
fi

if ! $GRP_ADMIN info 2>/dev/null | grep -q "Token present:  yes"; then
  echo "No admin token stored yet."
  echo "Run once:  $RELEASE_DIR/tools/install_admin.sh"
  echo "Then re-run this script."
  exit 1
fi

$GRP_ADMIN smoke

banner "✅  Day 2 complete"
cat <<EOF

What you just accomplished:
  ✓ Built graperoot-pro.tar.gz
  ✓ Published v$VERSION to kunal12203/graperoot-pro-releases (private, license-gated)
  ✓ Pushed installer scripts to kunal12203/graperoot-pro-public (public)
  ✓ Verified the full license → download_url → asset stream flow end-to-end

WHAT STILL NEEDS DOING (you, browser — 15 min):
  → Set up Cloudflare Pages so graperoot.dev/pro/install.sh serves the public repo.
    Instructions in the next reply.

  → Once Pages is live, you can issue your first real customer license and send the PDF.

NEXT:
  → Reply "done" and I'll walk you through Cloudflare Pages + first customer send-off.
EOF
