# GrapeRoot Pro — Release Checklist

Follow this exactly when cutting a new Pro release.

## Repos

| Purpose | Repo | Visibility |
|---|---|---|
| **Pro source** (this repo) | `kunal12203/graperoot-review` | private |
| **Pro public assets** | `kunal12203/graperoot-pro-public` | public — launcher scripts |
| **Pro release tarballs** | `kunal12203/graperoot-pro-releases` | private — license-gated |

**Why 3 repos:**
- Launcher scripts (`dgc-pro`, `launch_pro.sh`, `version.txt`, `changelog.txt`) must be public so install.sh can fetch them without auth.
- The Pro engine tarball (`graperoot-pro.tar.gz`) is private — only downloaded after license validation via the Cloudflare Worker.
- Source stays in this repo; only built artifacts go to the other two.

## What's in the tarball

`graperoot-pro.tar.gz` contains:
```
graperoot-pro/
  mcp_graph_server_v7.5.py   ← the Pro MCP server (update this in build_tarball.sh when upgrading)
  graph_builder.py            ← copied from graph_builder_v6.2.py
  launch.py                   ← thin Python wrapper called by launch_pro.sh/.ps1
  graph_gate.py               ← PreToolUse hook (Claude file-access gate)
  graph_sync.py               ← PostToolUse hook (graph sync after edits)
  requirements.txt
  VERSION
  LICENSE.txt
  bin/
    dgc-pro-doctor            ← diagnostic + auto-fix tool
    dgc-pro-doctor.py
```

**Note:** The tarball ships plain `.py` files (no compilation). Source is protected by
the license-gated download — only users with a valid license get the URL.
Future: compile with Nuitka for binary-only distribution.

## Single source of truth

`release/bin/version.txt` is the canonical version. Everything else derives from it.

## Pre-release stability checklist

Run this before building the tarball:

```bash
cd "/Users/krishnakant/Documents/Personal Projects/GrapeRoot Pro"

# 1. Syntax check
python3 -c "
import ast
for f in ['mcp_graph_server_v7.5.py','graph_builder_v6.2.py','release/bin/launch.py']:
    ast.parse(open(f).read()); print(f'✓ {f}')
"

# 2. Stability checks
python3 -c "
src = open('mcp_graph_server_v7.5.py').read()
checks = [
    ('session middleware',     'session_middleware' in src),
    ('gate in graph_read',     '\"action_required\": \"graph_continue\"' in src),
    ('gc sets flag',           'graph_continue_called\"] = True' in src),
    ('no debug prints',        '[debug' not in src and '[gate]' not in src),
    ('v7.6 in docstring',      'v7.6' in src),
    ('workers=1',              'workers=1' in src),
    ('Rust support',           'extract_symbols_rust' in open('graph_builder_v6.2.py').read()),
]
for label, ok in checks: print(f'  {\"✓\" if ok else \"✗\"} {label}')
"

# 3. Quick gate test (server must be running)
# PORT=18765 DG_DATA_DIR=... python3 mcp_graph_server_v7.5.py &
# python3 release/tools/test_gates.py   ← (create this)
```

## Step-by-step release

### 1. Check current version
```bash
cat release/bin/version.txt
# → 1.0.13
```

### 2. Update changelog FIRST
Edit `release/bin/changelog.txt` — prepend new entry at top:
```
1.0.14  (YYYY-MM-DD)
- Fixed: …
- Added: …
```
**Never release without updating changelog.** Users see it on auto-update.

### 3. Bump version (no trailing newline)
```bash
printf "1.0.14" > release/bin/version.txt
```

### 4. Update `build_tarball.sh` if MCP server version changed
Check line 12 — it must reference the current server file:
```bash
grep "mcp_graph_server" release/build_tarball.sh
# Should show: cp "$PROJ_ROOT/mcp_graph_server_v7.5.py" "$STAGE/"
```
If you upgrade to v7.6, change this line first.

### 5. Build tarball
```bash
cd "/Users/krishnakant/Documents/Personal Projects/GrapeRoot Pro"
bash release/build_tarball.sh 1.0.14
# → release/graperoot-pro.tar.gz
```

Verify contents:
```bash
tar -tzf release/graperoot-pro.tar.gz
# Must include: mcp_graph_server_v7.5.py, graph_builder.py, launch.py, VERSION
```

### 6. Upload to private releases repo
```bash
gh release create v1.0.14 \
  "release/graperoot-pro.tar.gz" \
  --repo kunal12203/graperoot-pro-releases \
  --title "GrapeRoot Pro v1.0.14" \
  --notes "$(head -10 release/bin/changelog.txt)"
```

### 7. Update Worker PRO_VERSION
```bash
# Edit release/worker/wrangler.toml:
#   PRO_VERSION = "v1.0.14"
sed -i '' 's/PRO_VERSION.*=.*/PRO_VERSION      = "v1.0.14"/' release/worker/wrangler.toml

# Deploy worker
cd release/worker && wrangler deploy
```
**Do this BEFORE pushing to pro-public** — otherwise the launcher will fetch a
version.txt pointing at v1.0.14 but the Worker still serves v1.0.13.

### 8. Push launcher files to pro-public
```bash
RELEASE="/Users/krishnakant/Documents/Personal Projects/GrapeRoot Pro/release"
for f in version.txt changelog.txt launch_pro.sh launch_pro.ps1 \
          dgc-pro dgc-pro.cmd dgc-pro.ps1 \
          dg-pro dg-pro.cmd dg-pro.ps1 \
          graperoot-pro graperoot-pro.cmd graperoot-pro.ps1; do
  SHA=$(gh api repos/kunal12203/graperoot-pro-public/contents/bin/$f --jq '.sha' 2>/dev/null || echo "")
  CONTENT=$(base64 < "$RELEASE/bin/$f")
  if [ -n "$SHA" ]; then
    gh api repos/kunal12203/graperoot-pro-public/contents/bin/$f -X PUT \
      -f message="v1.0.XX: <description>" -f content="$CONTENT" -f sha="$SHA" --jq '.commit.sha'
    echo "updated bin/$f"
  fi
done
```

All shims that must exist in `bin/`:
- `dgc-pro` / `dgc-pro.cmd` / `dgc-pro.ps1` — Claude launcher
- `dg-pro` / `dg-pro.cmd` / `dg-pro.ps1` — Codex launcher
- `graperoot-pro` / `graperoot-pro.cmd` / `graperoot-pro.ps1` — multi-platform launcher

### 9. Smoke test (verify auto-update)
On an existing install that has an older version:
```bash
dgc-pro /tmp/some-project
# Should print: [update] GrapeRoot Pro 1.0.13 → 1.0.14
# Then start normally
```

### 10. Mark release as latest (remove prerelease flag)
```bash
gh release edit v1.0.14 \
  --repo kunal12203/graperoot-pro-releases \
  --latest \
  --prerelease=false
```

---

## Order matters — always do this sequence

```
1. Syntax + stability checks
2. Changelog updated
3. Version bumped
4. Tarball built and verified
5. Tarball uploaded to graperoot-pro-releases  ← must exist before Worker deploys
6. Worker deployed (PRO_VERSION updated)        ← must be live before pro-public pushed
7. pro-public pushed (version.txt updated)      ← triggers auto-update for all users
8. Smoke test
9. Mark release as latest
```

---

## Version locations (all must match)

| File | Field | Current |
|---|---|---|
| `release/bin/version.txt` | entire file | `1.0.26` |
| `release/worker/wrangler.toml` | `PRO_VERSION` | `"v1.0.26"` |
| `release/bin/changelog.txt` | top entry version | `1.0.26` |
| `release/bin/launch.py` | version comment (line 4) + fallback string | `1.0.26` |
| `release/build_tarball.sh` | `mcp_graph_server_v?.?.py` | `v7.5` |

---

## Issuing a license
```bash
curl -X POST https://api.graperoot.dev/v1/admin/issue \
  -H "Authorization: Bearer $GRAPEROOT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customer":"Acme Corp","email":"cto@acme.com","tier":"pro","seats":5,"expires":"2027-04-22"}'
```

## Revoking a license
```bash
curl -X POST https://api.graperoot.dev/v1/admin/revoke \
  -H "Authorization: Bearer $GRAPEROOT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"license_key":"GRP-XXXX-XXXX-XXXX"}'
```
Revocation takes effect within 24h (Worker cache TTL).

---

## CI smoke tests (kunal12203/graperoot-pro-public)

The `Windows install smoke test` workflow tests all 4 platforms (PS5.1, PS7, Ubuntu, Fedora) on every push.

**Secret required:** `GRAPEROOT_LICENSE_KEY` must be set on `kunal12203/graperoot-pro-public`:
```bash
gh secret set GRAPEROOT_LICENSE_KEY --repo kunal12203/graperoot-pro-public --body "GRP-UREE-9YYZ-LRVF"
```

**Key used:** King (`GRP-UREE-9YYZ-LRVF`) with `seats: 0` (unlimited — CI runners get random hostnames every run, so a finite seat limit will fill up within a few runs).

**Verify scripts** live at `.github/workflows/scripts/verify-install.ps1` and `verify-install.sh`. They check:
- All expected files present (including `mcp_graph_server_v7.5.py` — update when upgrading server)
- Server does NOT contain `0.0.0.0` (security regression guard)
- Python imports work
- `launch.py --version` exits cleanly

**Trigger manually:**
```bash
gh workflow run "Windows install smoke test" --repo kunal12203/graperoot-pro-public --field license_key="GRP-UREE-9YYZ-LRVF"
```

---

## KV key management

License keys are in Cloudflare KV namespace `LICENSES` (binding id `273da98358724f30ada795f7d0e8713d`).

```bash
# Read a key
cd release/worker && wrangler kv key get --remote --binding=LICENSES "GRP-XXXX-XXXX-XXXX"

# Update a key (fetch → modify → put back)
RECORD=$(wrangler kv key get --remote --binding=LICENSES "GRP-XXXX-XXXX-XXXX")
UPDATED=$(echo "$RECORD" | python3 -c "import json,sys; r=json.load(sys.stdin); r['seats']=5; print(json.dumps(r))")
wrangler kv key put --remote --binding=LICENSES "GRP-XXXX-XXXX-XXXX" "$UPDATED"
```

**Seat limit:** `seats: 0` = unlimited (bypasses the `if (record.seats && ...)` guard in the Worker).  
**Never delete keys to clean up** — deleted keys can't be recovered from KV. Only revoke (`revoked: true`).

---

## Common pitfalls

- **Changelog not updated** → users upgrade silently, can't see what changed
- **Worker not deployed before pro-public pushed** → launcher fetches new version.txt, Worker still returns old tarball URL
- **build_tarball.sh pointing at old server version** → tarball ships outdated MCP server
- **Trailing newline in version.txt** → use `printf` not `echo`, version comparison breaks
- **Pushing pro-public before tarball exists in releases** → fresh installs 404 on download
- **CI seat limit hit** → King key must have `seats: 0`; each CI run uses a fresh random hostname
- **Verify scripts check old server version** → update `.github/workflows/scripts/verify-install.{ps1,sh}` when upgrading MCP server
- **Server binding 0.0.0.0** → mcp_graph_server must have no `0.0.0.0` strings; verify script enforces this
- **launch.py fallback version stale** → update the `else "1.0.XX"` fallback on line ~732 when bumping version
