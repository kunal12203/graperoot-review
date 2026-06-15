# GrapeRoot Pro — Release Checklist

Follow this checklist **exactly** when releasing a new version. All locations must stay in sync.

## Repos involved

| Repo | Local path | Remote | Purpose |
|------|-----------|--------|---------|
| **GrapeRoot Pro** (this) | `~/Documents/Personal Projects/GrapeRoot Pro` | `kunal12203/graperoot-review` | MCP server, webhook, tarball |
| **Pro Releases** | — (GitHub-only) | `kunal12203/graperoot-pro-releases` | Tarball hosting via GitHub Releases |
| **Pro Public** | `~/Documents/Personal Projects/graperoot-pro-public` | `kunal12203/graperoot-pro-public` | `bin/version.txt` for installer/auto-update |
| **Cloudflare Worker** | `release/worker/` in this repo | `api.graperoot.dev` | License verify + PRO_VERSION for download URL |

## Version locations (ALL must match)

| Location | File | What to update |
|----------|------|----------------|
| This repo | `release/bin/version.txt` | `1.0.XX` |
| This repo | `release/bin/changelog.txt` | Add entry at top |
| This repo | `release/bin/launch.py` | Version note in docstring header |
| This repo | `release/worker/wrangler.toml` | `PRO_VERSION = "v1.0.XX"` |
| This repo | `release/graperoot-pro.tar.gz` | Rebuilt via `build_tarball.sh` |
| Pro Public | `bin/version.txt` | `1.0.XX` (triggers auto-update for users) |
| Cloudflare Worker | KV env `PRO_VERSION` | Deployed via `wrangler deploy` |

## Step-by-step

### 1. Make your changes

Edit the source files (`mcp_graph_server_v7.5.py`, `webhook.py`, `release/bin/launch.py`, etc.)

### 2. Syntax check

```bash
python3 -c "import ast; ast.parse(open('mcp_graph_server_v7.5.py').read())"
python3 -c "import ast; ast.parse(open('webhook.py').read())"
python3 -c "import ast; ast.parse(open('release/bin/launch.py').read())"
```

### 3. Bump version in ALL locations

```bash
# 1. version.txt
echo "1.0.XX" > release/bin/version.txt

# 2. wrangler.toml
# Edit: PRO_VERSION = "v1.0.XX"

# 3. changelog.txt — add entry at TOP
# 4. launch.py — add version note at top of docstring
```

### 4. Build tarball

```bash
bash release/build_tarball.sh 1.0.XX
```

This bundles: `mcp_graph_server_v7.5.py`, `graph_builder_v6.2.py`, `launch.py`,
`version.txt`, `graph_gate.py`, `graph_sync.py`, shims, doctor, LICENSE.

### 5. Deploy locally (test)

```bash
cp mcp_graph_server_v7.5.py ~/.graperoot-pro/mcp_graph_server_v7.5.py
```

Restart `dgc-pro .` and verify the change works.

### 6. Commit and push to master

```bash
git add mcp_graph_server_v7.5.py webhook.py release/bin/version.txt \
        release/bin/changelog.txt release/bin/launch.py \
        release/graperoot-pro.tar.gz release/worker/wrangler.toml
git commit -m "v1.0.XX: <short description>"
git push origin master
```

**Railway auto-deploys `webhook.py` from master.** The DB migration runs on startup.

### 7. Create GitHub Release (tarball)

```bash
gh release create v1.0.XX release/graperoot-pro.tar.gz \
  --repo kunal12203/graperoot-pro-releases \
  --prerelease \
  --notes "v1.0.XX — <one-line summary>"
```

### 8. Bump graperoot-pro-public

```bash
cd ~/Documents/Personal\ Projects/graperoot-pro-public
echo "1.0.XX" > bin/version.txt
git add bin/version.txt
git commit -m "bump version.txt to 1.0.XX — sync with Pro release"
git pull --rebase origin main   # resolve conflicts: always pick new version
git push origin main
```

This is what the installer and auto-update check. Once pushed, users on older
versions will auto-update on their next `dgc-pro .` run.

### 9. Deploy Cloudflare Worker

```bash
cd release/worker
npx wrangler deploy
```

Verify in output: `env.PRO_VERSION ("v1.0.XX")`

This updates the download URL that `api.graperoot.dev/v1/license/verify` returns,
so new installs get the correct tarball version.

### 10. Verify

- [ ] `curl -s https://graperoot-review-production.up.railway.app/health` returns `"status": "ok"`
- [ ] Run `dgc-pro .` in a test project → check `~/.graperoot-pro/server.log` for startup
- [ ] End session → check Railway logs for incoming POST to `/api/usage`
- [ ] `gh release view v1.0.XX --repo kunal12203/graperoot-pro-releases` shows the tarball

## Push order (matters!)

1. **This repo → master** (Railway deploys webhook; needs to be ready before clients hit it)
2. **GitHub Release** (tarball must exist before auto-update tries to download it)
3. **graperoot-pro-public** (triggers auto-update — clients fetch tarball from step 2)
4. **Cloudflare Worker** (new installs get correct version — can be parallel with step 3)

## Backwards compatibility

When adding new fields to the telemetry pipeline:

1. **Webhook columns**: use `ADD COLUMN IF NOT EXISTS` in `_migrate_db()` with a DEFAULT
2. **Stop hook payload**: old hooks send fewer fields — webhook must handle missing fields
   via `data.get("new_field", 0)` (never crash on missing keys)
3. **API responses**: new fields can appear freely — frontends ignore unknown keys
4. **Placeholder counts**: if INSERT gains columns, update `_pro_ph(N)` to match

## Common mistakes

- **Forgetting wrangler.toml** — PRO_VERSION stays old, new installs get wrong tarball
- **Forgetting graperoot-pro-public** — auto-update never fires for existing users
- **Pushing public before GitHub Release** — auto-update tries to download a tarball that doesn't exist yet (404)
- **Not rebuilding tarball after changelog edit** — changelog is bundled in the tarball
- **Placeholder count mismatch** — `_pro_ph(N)` must equal the number of columns in INSERT
- **Testing only locally** — always verify Railway received the push (`/health` endpoint)
