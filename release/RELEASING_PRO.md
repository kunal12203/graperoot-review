# GrapeRoot Pro Release Checklist

Complete guide for releasing a new version of GrapeRoot Pro.

## Pre-Release Checklist

- [ ] All PRs merged and tested
- [ ] No known critical bugs
- [ ] MCP server tests passing
- [ ] License validation working
- [ ] Version mismatch audit completed

## Version Consistency Check

**CRITICAL**: All these files MUST have the same version number:

| File | Line/Location | Format |
|------|---------------|--------|
| `release/bin/version.txt` | Entire file | `1.0.X` |
| `release/build_tarball.sh` | Line 6 fallback | `echo 1.0.X` |
| `release/install.sh` | Line 316 fallback | `echo "1.0.X"` |
| `release/bin/launch.py` | Line 4 comment | `v1.0.X:` |
| `release/bin/launch.py` | Line 279 fallback | `"1.0.X"` |
| `release/worker/wrangler.toml` | PRO_VERSION | `"v1.0.X"` |

Run the version audit:
```bash
cd release
grep -r "1\.0\.[0-9]" bin/version.txt bin/launch.py install.sh build_tarball.sh worker/wrangler.toml
```

All occurrences should show the SAME version.

## MCP Server Version Check

**CRITICAL**: MCP server file and all references must match:

| File | What to Check |
|------|---------------|
| Project root | Latest `mcp_graph_server_v7.X.py` file |
| `build_tarball.sh` | Line 16: `cp "$PROJ_ROOT/mcp_graph_server_v7.X.py"` |
| `launch.py` | Lines 152, 238: `mcp_graph_server_v7.X.py` |

Run the MCP version audit:
```bash
cd release
ls -1 ../mcp_graph_server_v*.py | tail -1  # Latest version
grep "mcp_graph_server_v7" bin/launch.py build_tarball.sh
```

All should reference the SAME v7.X version.

## Release Steps

### 1. Determine Next Version

Check current version across all locations:
```bash
cat release/bin/version.txt
grep PRO_VERSION release/worker/wrangler.toml
```

Increment patch version: `1.0.14` → `1.0.15`

### 2. Update ALL Version References

**DO NOT skip any of these files** (use find/replace to avoid typos):

```bash
# Update version.txt
echo "1.0.15" > release/bin/version.txt

# Update build_tarball.sh fallback (line 6)
sed -i.bak 's/echo 1.0.[0-9]*/echo 1.0.15/' release/build_tarball.sh

# Update install.sh fallback (line 316)
sed -i.bak 's/echo "1.0.[0-9]*"/echo "1.0.15"/' release/install.sh

# Update launch.py comment (line 4)
sed -i.bak 's/v1.0.[0-9]*/v1.0.15/' release/bin/launch.py

# Update launch.py fallback (line 279)
sed -i.bak 's/"1.0.[0-9]*"/"1.0.15"/' release/bin/launch.py

# Verify all changed
grep -n "1\.0\.15" release/bin/version.txt release/build_tarball.sh release/install.sh release/bin/launch.py
```

### 3. Build Release Tarball

```bash
cd release
./build_tarball.sh 1.0.15
```

Verify:
```bash
tar -tzf graperoot-pro.tar.gz
tar -xzf graperoot-pro.tar.gz -O graperoot-pro/VERSION  # Should show 1.0.15
tar -xzf graperoot-pro.tar.gz -O graperoot-pro/launch.py | grep "1\.0\.[0-9]*"  # All 1.0.15
```

### 4. Test Installation Locally

```bash
# Backup existing install
mv ~/.graperoot-pro ~/.graperoot-pro.backup

# Test install from local tarball
cd release
tar -xzf graperoot-pro.tar.gz -C /tmp
cd /tmp/graperoot-pro
# Manually copy to ~/.graperoot-pro and test

# Or test full install script
bash install.sh YOUR-TEST-LICENSE-KEY
```

Verify:
- [ ] Version shows correctly: `cat ~/.graperoot-pro/bin/version.txt`
- [ ] MCP server file exists: `ls ~/.graperoot-pro/mcp_graph_server_v7.*.py`
- [ ] Doctor tool works: `dgc-pro-doctor`
- [ ] Can start: `dgc-pro /tmp/test-project`

### 5. Commit Changes

```bash
git add release/bin/version.txt \
        release/build_tarball.sh \
        release/install.sh \
        release/bin/launch.py \
        release/graperoot-pro.tar.gz

git commit -m "v1.0.15: <brief description of changes>

Changes:
- <list key changes>
- Updated all version references to 1.0.15

Co-Authored-By: Claude Sonnet 4.5 (1M context) <noreply@anthropic.com>"

git tag v1.0.15
git push && git push --tags
```

### 6. Upload to GitHub Releases

```bash
cd release

# Upload tarball to private repo
gh release create v1.0.15 graperoot-pro.tar.gz \
  --repo kunal12203/graperoot-pro-releases \
  --title "GrapeRoot Pro v1.0.15" \
  --notes "## Changes

- <list changes here>

## Installation

\`\`\`bash
curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- YOUR-LICENSE-KEY
\`\`\`

## Upgrade

\`\`\`bash
dgc-pro-doctor --fix  # Auto-fix any issues
# Or reinstall
\`\`\`
"
```

### 7. Update Cloudflare Worker

Update the download version:

```bash
cd release/worker

# Edit wrangler.toml
# Change: PRO_VERSION = "v1.0.15"

wrangler deploy
```

Verify:
```bash
curl -X POST https://api.graperoot.dev/v1/license/verify \
  -H "Content-Type: application/json" \
  -d '{"license_key":"TEST-KEY","host":"test","os":"Darwin"}' | jq .download_url
```

Should return URL with `v1.0.15`.

### 8. Test End-to-End

Create a test perpetual license and install:

```bash
# Create test license
curl -X POST https://api.graperoot.dev/v1/admin/issue \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customer":"Release Test","email":"test@test.com","expires":"perpetual","seats":1}' | jq .license.key

# Test install
curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX

# Verify version
cat ~/.graperoot-pro/bin/version.txt  # Should show 1.0.15

# Test MCP
dgc-pro /tmp/test-project
# In Claude Code, check /mcp shows graperoot-pro connected
```

### 9. Update Documentation

- [ ] Update CHANGELOG.md with release notes
- [ ] Update README.md if features changed
- [ ] Update graperoot.dev/pro/docs if needed

### 10. Notify Users (Optional)

For major releases:
- Post in Discord/Slack
- Email announcement
- Update website banner

## Post-Release Verification

Run these commands to verify the release is live:

```bash
# Check GitHub release exists
gh release view v1.0.15 --repo kunal12203/graperoot-pro-releases

# Check worker version
curl https://api.graperoot.dev/ping

# Check download works
curl -I "https://api.graperoot.dev/v1/release/asset?k=TEST-KEY"
# Should return 200 or 403 (not 404)

# Check version audit
cd release
./bin/dgc-pro-doctor.py  # Should show all v1.0.15
```

## Common Mistakes to Avoid

### ❌ Version Mismatches
**Problem**: Updating version.txt but forgetting fallbacks in scripts.

**Check**: Run `grep -r "1\.0\.[0-9]" release/` and verify ALL match.

### ❌ MCP Server Version Mismatch
**Problem**: Tarball has v7.5.py but launch.py looks for v7.4.py.

**Check**: 
```bash
tar -tzf release/graperoot-pro.tar.gz | grep mcp_graph_server
grep mcp_graph_server release/bin/launch.py
```

### ❌ Forgetting to Update Worker
**Problem**: Tarball uploaded but PRO_VERSION still points to old version.

**Check**: Users download wrong version.

**Fix**: Always update `wrangler.toml` PRO_VERSION and redeploy.

### ❌ Not Testing Install
**Problem**: Tarball builds but install fails due to missing files.

**Check**: Always test install script locally before releasing.

### ❌ Stale Tarball
**Problem**: Built tarball before updating version files.

**Fix**: Delete `graperoot-pro.tar.gz` and rebuild after version updates.

## Rollback Procedure

If a release has critical issues:

```bash
# 1. Revert Cloudflare Worker to previous version
cd release/worker
# Edit wrangler.toml: PRO_VERSION = "v1.0.14"
wrangler deploy

# 2. Delete GitHub release
gh release delete v1.0.15 --repo kunal12203/graperoot-pro-releases --yes

# 3. Revert git commits
git revert HEAD
git push

# 4. Notify users
```

## Automated Checks (TODO)

Future improvements:
- [ ] Pre-commit hook to verify version consistency
- [ ] CI job to build and test tarball
- [ ] Automated MCP connectivity test
- [ ] Version mismatch detector in doctor tool

## Support

If you encounter issues during release:
- Check `KNOWN_ISSUES.md` for workarounds
- Test with `dgc-pro-doctor --fix`
- Contact: support@graperoot.dev
