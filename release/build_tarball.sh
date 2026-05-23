#!/usr/bin/env bash
# Build graperoot-pro.tar.gz for GitHub release attachment.
# Usage: ./build_tarball.sh [VERSION]
set -Eeuo pipefail

VERSION="${1:-$(cat release/bin/version.txt 2>/dev/null || echo 1.0.16)}"
PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_DIR="$PROJ_ROOT/release"
BUILD_DIR="$(mktemp -d -t grp-pro-build.XXXXXX)"
OUT="$RELEASE_DIR/graperoot-pro.tar.gz"
STAGE="$BUILD_DIR/graperoot-pro"

mkdir -p "$STAGE"

# 1. MCP server (the actual Pro engine)
cp "$PROJ_ROOT/mcp_graph_server_v7.5.py" "$STAGE/"

# 2. Graph builder (prefer v6.2 — what the server expects)
cp "$PROJ_ROOT/graph_builder_v6.2.py" "$STAGE/graph_builder.py"

# 3. Python deps. graperoot provides dg.retrieve for in-process retrieval —
# without it, Pro's graph_continue silently falls back to HTTP against the
# free dashboard on :8787 and fails. Tree-sitter deps are left out intentionally
# (graph_builder falls back to regex cleanly; matches free behaviour).
cat > "$STAGE/requirements.txt" <<'EOF'
mcp>=1.3.0
uvicorn>=0.29.0
anyio>=4.0.0
starlette>=0.36.0
graperoot>=3.9.0
EOF

# 4. Version + launch.py (the Python core called by launch_pro.{sh,ps1})
cp "$RELEASE_DIR/bin/launch.py"   "$STAGE/launch.py"
cp "$RELEASE_DIR/bin/version.txt" "$STAGE/VERSION"

# 5. Doctor script (diagnostic + auto-fix for MCP issues)
mkdir -p "$STAGE/bin"
cp "$RELEASE_DIR/bin/dgc-pro-doctor.py" "$STAGE/bin/"
cp "$RELEASE_DIR/bin/dgc-pro-doctor"    "$STAGE/bin/"
chmod +x "$STAGE/bin/dgc-pro-doctor"

# 6. LICENSE / terms
cat > "$STAGE/LICENSE.txt" <<'EOF'
GrapeRoot Pro — Commercial License
© 2026 GrapeRoot.  All rights reserved.

This software is licensed per-seat per the terms in your purchase agreement.
Use, copying, or redistribution outside the seat count on your license is prohibited.
Contact support@graperoot.dev for enterprise licensing.
EOF

tar -czf "$OUT" -C "$BUILD_DIR" "graperoot-pro"
rm -rf "$BUILD_DIR"

echo "Built: $OUT"
echo "Version: $VERSION"
echo ""
echo "Upload:"
echo "  gh release create v$VERSION $OUT --repo kunal12203/graperoot-pro-releases --prerelease --notes \"v$VERSION\""
