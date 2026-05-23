# GrapeRoot Pro — Release Artifacts

This folder contains everything needed to ship GrapeRoot Pro to paying customers.

## Files

```
release/
├── install.sh                  # Unix installer — host at https://graperoot.dev/pro/install.sh
├── install.ps1                 # Windows installer — host at https://graperoot.dev/pro/install.ps1
├── build_tarball.sh            # Build graperoot-pro.tar.gz from the project source
├── bin/
│   ├── version.txt             # Canonical version ("1.0.2")
│   ├── changelog.txt           # Auto-update shows this to users
│   ├── launch_pro.sh           # Unix runtime launcher (self-update + license + MCP + claude)
│   ├── launch_pro.ps1          # Windows runtime launcher
│   ├── launch.py               # Python core (called by both shells)
│   ├── dgc-pro                 # Unix shim (execs launch_pro.sh)
│   ├── dgc-pro.cmd             # Windows cmd shim
│   └── dgc-pro.ps1             # Windows PowerShell shim
├── worker/
│   ├── src/license-worker.js   # Cloudflare Worker (license verify + download proxy + admin)
│   └── wrangler.toml           # Worker config (KV binding, env vars)
├── ONBOARDING.md               # Customer-facing — fill in license key, export to PDF, send
├── RELEASING.md                # Internal release runbook
└── README.md                   # This file
```

## How it fits together

```
Customer runs:  curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX

1.  install.sh verifies license against api.graperoot.dev (Cloudflare Worker)
2.  Worker returns a license-gated download_url → install.sh fetches graperoot-pro.tar.gz
3.  Tarball extracts to ~/.graperoot-pro/  (Unix) or %USERPROFILE%\.graperoot-pro\  (Windows)
4.  install.sh also downloads bin/launch_pro.sh, bin/dgc-pro, etc. from R2 / GitHub
5.  On each `dgc-pro /path/to/project` invocation:
      - launch_pro.{sh,ps1} checks bin/version.txt against R2 (self-update)
      - re-verifies license (once per 24h; 7d offline grace)
      - calls launch.py → starts MCP server → merges .mcp.json → execs claude
```

## Platform matrix

| Platform | Installer | Install dir | Command | Python venv |
|---|---|---|---|---|
| macOS (Intel, Apple Silicon) | `install.sh` | `~/.graperoot-pro/` | `dgc-pro` | `venv/bin/` |
| Linux (x86_64, arm64) | `install.sh` | `~/.graperoot-pro/` | `dgc-pro` | `venv/bin/` |
| Windows 10/11 | `install.ps1` | `%USERPROFILE%\.graperoot-pro\` | `dgc-pro.cmd` | `venv\Scripts\` |

All platforms: Python ≥ 3.10, Claude Code CLI (auto-offered during install), isolated venv.

## Coexistence with GrapeRoot Free

Zero collision. Pro uses a different install dir, different command name, different MCP server name, different project data dir. If the user has the free install, the Pro installer detects and reports it, then installs alongside — never overwrites.

## One-time infrastructure setup

1. **Register `graperoot.dev/pro/install.sh` and `/install.ps1`** as static files on your marketing site (or Cloudflare Pages).
2. **Deploy the Worker:**
   ```bash
   cd release/worker
   wrangler kv namespace create LICENSES
   # paste the returned id into wrangler.toml
   wrangler secret put GITHUB_TOKEN    # PAT: scope = repo, for kunal12203/graperoot-pro-releases
   wrangler secret put ADMIN_TOKEN     # random 32+ char string
   wrangler deploy
   ```
3. **DNS:** point `api.graperoot.dev` CNAME at the Worker.
4. **Create the private GitHub release:**
   ```bash
   ./build_tarball.sh
   gh release create v1.0.2 graperoot-pro.tar.gz --repo kunal12203/graperoot-pro-releases --prerelease
   ```
5. **R2 bucket `graperoot-pro`** (optional but recommended for launcher CDN): upload `bin/*`, `install.sh`, `install.ps1`.

After that, issuing a new license is one API call (see `RELEASING.md` § "Issuing a customer license").

## Effort to go live (from now)

- Day 1: deploy Worker, configure DNS, test `/v1/license/verify` locally
- Day 2: build first tarball, upload to private release, issue test license, run `install.sh` end-to-end on clean Mac + Linux VM
- Day 3: same on Windows 10/11 VM; export ONBOARDING.md → PDF; send first customer invitation

## What to send the customer

1. PDF export of `ONBOARDING.md` with their license key filled in
2. One email — the one-line install command + license key + support channel

That's it. No GitHub link, no manual steps.
