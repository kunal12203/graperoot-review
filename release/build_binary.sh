#!/usr/bin/env bash
# Build graperoot-pro MCP server as a native binary using Nuitka.
# Output: dist/mcp-server-{platform}
#
# Usage:
#   ./release/build_binary.sh              # build for current platform
#   ./release/build_binary.sh --clean      # clean dist/ and rebuild
#
# Runs on: macOS (arm64 + x86_64), Linux (x86_64 + arm64)
# Windows: use build_binary.ps1

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$SCRIPT_DIR/release/dist"
ENTRY="$SCRIPT_DIR/mcp_graph_server_v7.5.py"

# ── Platform detection ────────────────────────────────────────────────────────
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS-$ARCH" in
    Darwin-arm64)   PLATFORM="macos-arm64";   BIN="mcp-server" ;;
    Darwin-x86_64)  PLATFORM="macos-x86_64";  BIN="mcp-server" ;;
    Linux-x86_64)   PLATFORM="linux-x86_64";  BIN="mcp-server-linux" ;;
    Linux-aarch64)  PLATFORM="linux-arm64";   BIN="mcp-server-linux-arm" ;;
    *)              echo "Unsupported: $OS-$ARCH" >&2; exit 1 ;;
esac
echo "[build] Platform: $PLATFORM → release/dist/$BIN"

[[ "${1:-}" == "--clean" ]] && rm -rf "$DIST_DIR" "$SCRIPT_DIR/mcp_graph_server_v7.5.build" "$SCRIPT_DIR/mcp_graph_server_v7.5.dist"
mkdir -p "$DIST_DIR"

# ── Python 3.13 ───────────────────────────────────────────────────────────────
PYTHON=python3.13
command -v $PYTHON &>/dev/null || { PYTHON=python3; }
echo "[build] Python: $($PYTHON --version)"

# ── Fix: importlib.util dynamic load breaks Nuitka ───────────────────────────
# graph_builder_v6.2.py is loaded at runtime from disk via importlib.util.
# Nuitka has no .py on disk — rename it to graph_builder_pro.py so Nuitka
# can compile it in as a proper module, then patch the import.
cp "$SCRIPT_DIR/graph_builder_v6.2.py" "$SCRIPT_DIR/graph_builder_pro.py"

# Patch: replace the importlib.util dynamic load with a direct import
$PYTHON -c "
content = open('$ENTRY').read()
old = '''try:
    import importlib.util as _imputil
    _gb_spec = _imputil.spec_from_file_location(
        \"graph_builder_v6_2\",
        str(Path(__file__).resolve().parent / \"graph_builder_v6.2.py\"),
    )
    _gb_mod = _imputil.module_from_spec(_gb_spec)
    _gb_mod.__name__ = \"graph_builder_v6_2\"
    _sys.modules[\"graph_builder_v6_2\"] = _gb_mod
    _gb_spec.loader.exec_module(_gb_mod)
    _gb_scan = _gb_mod.scan
except Exception:  # noqa: BLE001
    # Fall back to compiled module if local script fails'''
new = '''try:
    import graph_builder_pro as _gb_mod  # compiled in by Nuitka
    _gb_scan = _gb_mod.scan
except Exception:  # noqa: BLE001
    # Fall back to compiled module if local script fails'''
if old in content:
    open('$ENTRY', 'w').write(content.replace(old, new))
    print('[build] Patched importlib.util → direct import of graph_builder_pro')
else:
    print('[build] WARNING: patch target not found — importlib.util may break at runtime')
"

# Restore on exit
cleanup() {
    # Restore server to original importlib.util form
    $PYTHON -c "
content = open('$ENTRY').read()
new_block = '''try:
    import graph_builder_pro as _gb_mod  # compiled in by Nuitka
    _gb_scan = _gb_mod.scan
except Exception:  # noqa: BLE001
    # Fall back to compiled module if local script fails'''
old_block = '''try:
    import importlib.util as _imputil
    _gb_spec = _imputil.spec_from_file_location(
        \"graph_builder_v6_2\",
        str(Path(__file__).resolve().parent / \"graph_builder_v6.2.py\"),
    )
    _gb_mod = _imputil.module_from_spec(_gb_spec)
    _gb_mod.__name__ = \"graph_builder_v6_2\"
    _sys.modules[\"graph_builder_v6_2\"] = _gb_mod
    _gb_spec.loader.exec_module(_gb_mod)
    _gb_scan = _gb_mod.scan
except Exception:  # noqa: BLE001
    # Fall back to compiled module if local script fails'''
if new_block in content:
    open('$ENTRY', 'w').write(content.replace(new_block, old_block))
    print('[build] Restored mcp_graph_server_v7.5.py')
" 2>/dev/null || true
    rm -f "$SCRIPT_DIR/graph_builder_pro.py"
}
trap cleanup EXIT

# ── Install Nuitka + deps ─────────────────────────────────────────────────────
echo "[build] Installing build dependencies..."
$PYTHON -m pip install --quiet --break-system-packages \
    nuitka zstandard ordered-set \
    "mcp>=1.3.0" uvicorn anyio starlette \
    "psycopg2-binary>=2.9.9" \
    "PyJWT>=2.8.0" cryptography certifi
echo "[build] Dependencies ready."

# ── Compile ───────────────────────────────────────────────────────────────────
echo "[build] Compiling with Nuitka (~8 min)..."
$PYTHON -m nuitka \
    --standalone \
    --onefile \
    --assume-yes-for-downloads \
    --output-dir="$DIST_DIR" \
    --output-filename="$BIN" \
    --include-package=mcp \
    --include-package=uvicorn \
    --include-package=anyio \
    --include-package=starlette \
    --include-package=certifi \
    --include-package=jwt \
    --include-package=cryptography \
    --include-package=psycopg2 \
    --include-module=graph_builder_pro \
    --include-data-files="$SCRIPT_DIR/release/bin/version.txt=VERSION" \
    --python-flag=no_site \
    --warn-implicit-exceptions \
    "$ENTRY"

echo ""
echo "[build] ✓ Binary: release/dist/$BIN"
echo "[build] Size: $(du -sh "$DIST_DIR/$BIN" | cut -f1)"
echo ""
echo "Test:"
echo "  ./release/dist/$BIN --help"
echo ""
echo "Package for release:"
echo "  cp release/dist/$BIN release/graperoot-pro-$PLATFORM"
echo "  gh release upload v\$(cat release/bin/version.txt) release/graperoot-pro-$PLATFORM --repo kunal12203/graperoot-pro-releases"
