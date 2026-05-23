# GrapeRoot Pro — Known Issues & Troubleshooting

This page covers install-time and runtime issues customers may hit, with the exact fix for each.
Start here before emailing support.

---

## Install-time

### Installer exits silently after installing ripgrep / Node / Claude Code

**Fixed 2026-04-23.** In `curl | bash` mode, child processes (brew/apt/npm) were inheriting stdin from the curl pipe and consuming the remaining bytes of the installer script as their own input. After the child finished, bash had no more commands to run — the installer appeared to exit mid-setup. Every external install command now redirects stdin to `/dev/null`.

**Recovery:** the installer is idempotent. Just re-run it:

```bash
curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX
```

It skips anything already present and only installs what's missing.

### Installer can't find Node.js / Claude Code on a fresh machine

**Fixed 2026-04-23 (v1.0.8+).** The installer now detects missing Node.js and offers to install it via:

- **macOS:** `brew install node`
- **Ubuntu/Debian:** `sudo apt-get install nodejs npm`
- **Fedora:** `sudo dnf install nodejs npm`
- **Arch:** `sudo pacman -S nodejs npm`
- **Windows:** `winget install OpenJS.NodeJS.LTS` (or `scoop` / `choco`)

Node 18+ is required by Claude Code. The installer warns if an older Node is present.

### Windows install.ps1 crash on PowerShell 5.1 (MissingEndCurlyBrace)

**Fixed 2026-04-23.** PS5.1's parser crashes on certain non-ASCII bytes (0x94 in particular — from em-dashes and box-drawing chars). The installer is now pure ASCII. If you're on an older cached copy and see this error, force a refresh:

```powershell
$ProgressPreference = "SilentlyContinue"
irm "https://graperoot.dev/pro/install.ps1?bust=$(Get-Random)" | iex
```

### "License server unreachable" / "Cannot reach license server"

**v1.0.9+ tells you exactly why.** The error message identifies whether it's DNS, TCP, TLS, or a corp proxy. Below is the matrix of fixes per cause.

If you're on **v1.0.8 or older**, this error message was ambiguous (it also fired for typo'd / revoked keys). Re-run the installer to upgrade — the v1.0.9 installer prints precise diagnostics.

| Symptom in error output | Root cause | Fix |
|---|---|---|
| `DNS lookup for api.graperoot.dev failed` | Corporate DNS filter blocks `.dev` TLD | Ask IT to whitelist `*.graperoot.dev` (or use `8.8.8.8` temporarily) |
| `TCP connection refused` | Firewall egress rule blocks Cloudflare | Send IT this whitelist: `api.graperoot.dev, graperoot.dev, pub-pro-r2.graperoot.dev` |
| `connection timed out` | Firewall silently drops packets to Cloudflare's edge | Same as above. The installer auto-retries 3x with backoff. |
| `TLS / certificate validation failed` | SSL-inspecting proxy with custom CA | `export GRAPEROOT_CA_BUNDLE=/path/to/corp-ca.pem` then re-run |

**Behind an HTTP proxy?** Set `HTTPS_PROXY` before running the installer:

```bash
export HTTPS_PROXY="http://your-proxy:8080"
export NO_PROXY=localhost,127.0.0.1
curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXXX-XXXX-XXXX
```

**Test connectivity quickly:**

```bash
curl -v https://api.graperoot.dev/v1/license/verify -X POST \
  -H "Content-Type: application/json" -d '{"license_key":"x"}'
```

If you see `HTTP/2 4xx` and a JSON body, the network is fine — the issue is your license key. Check the dashboard at `https://graperoot.dev/pro/login`.

### "License rejected: seat limit reached"

Another teammate is already using that seat. Either:
- Free a seat from the dashboard: `https://graperoot.dev/pro/login` → revoke a device
- Or contact sales to add more seats

### "Python 3.10+ is required"

- **macOS**: `brew install python@3.11`
- **Ubuntu/Debian**: `sudo apt-get install python3.11 python3.11-venv`
- **Windows**: `winget install Python.Python.3.11`

### "tar extraction failed (Windows)"

Your Windows is older than Windows 10 1803, which introduced native `tar`. Either upgrade Windows, or install Git for Windows (ships with `tar`).

### "dgc-pro: command not found" after install

- **macOS/Linux**: open a new terminal, or run `source ~/.zshrc` (or `~/.bashrc`). If still missing, run `echo $PATH | tr ':' '\n' | grep graperoot-pro` — it should show `~/.graperoot-pro/bin`.
- **Windows**: open a NEW PowerShell window (PATH doesn't refresh in the current one). If still missing, add `%USERPROFILE%\.graperoot-pro\bin` to your user PATH under System Properties → Environment Variables.

### Installer hangs or asks "[Y/n]" silently in CI

The installer is waiting for TTY input. In CI/automation, this auto-accepts after v1.0.5. If you want to skip all optional package installs (ripgrep, Claude Code prompts) from CI, set:

```bash
GRAPEROOT_SKIP_OPTIONAL=1 curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- GRP-XXX
```

### "brew: command not found" on Mac

Install Homebrew first:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Then re-run the GrapeRoot Pro installer.

### Windows Defender / antivirus flags the installer

This is a false positive. The installer is a plain PowerShell script that you can read at `https://graperoot.dev/pro/install.ps1`. Add an exception for `%USERPROFILE%\.graperoot-pro\` if needed.

---

## Runtime (while using `dgc-pro`)

### "MCP server failed to start within 30s"

Check `~/.graperoot-pro/server.log` for the actual error. Common causes:

- **Port already in use** — another instance on a weird port. Kill orphans with:
  ```bash
  pkill -f mcp_graph_server_v7
  ```
  Or just re-run `dgc-pro` — it auto-cleans orphan servers since v1.0.3.
- **Python venv broken** — rare. Fix: `rm -rf ~/.graperoot-pro && curl … install.sh | bash -s -- KEY`.

### "Claude CLI not found"

Install Claude Code globally:
```bash
npm install -g @anthropic-ai/claude-code
```

### Graph build is slow on first run

One-time cost: ~2 min per 10k files. Cached at `<project>/.dual-graph-pro/`. Subsequent runs are instant.

### `fallback_rg` / `graph_grep_all` fail — "rg not found"

Install ripgrep:
```bash
# macOS:        brew install ripgrep
# Ubuntu:       sudo apt-get install ripgrep
# Fedora:       sudo dnf install ripgrep
# Arch:         sudo pacman -S ripgrep
# Windows:      winget install BurntSushi.ripgrep.MSVC
```

Since v1.0.2 the installer prompts to auto-install it.

### License suddenly says "expired"

Check the expiration date in the dashboard: `https://graperoot.dev/pro/login`. Contact support@graperoot.dev to renew.

### License offline for > 7 days

Pro caches your license for 7 days of fully-offline use. After that, you need to reconnect to the internet once for `dgc-pro` to re-verify. Quick workaround if stuck: reconnect, run `dgc-pro` once, you're good for another 7 days offline.

---

## Dashboard

### "Sign In" button is disabled even with a valid key

Cache issue. Hard-refresh (Cmd/Ctrl+Shift+R) and retype the key. Fixed in the website build published 2026-04-23.

### "invalid origin" on login

You're opening `/pro/login` from a URL other than `https://graperoot.dev`. If your customer domain is custom, contact support — we'll whitelist it.

### Can I invite teammates?

A license is seat-limited. Share the license key privately with teammates; each person's install consumes one seat. To manage devices (revoke an old laptop to free a seat), sign in at `https://graperoot.dev/pro/login`.

---

## Coexistence with GrapeRoot Free

Pro never touches the free install. Run them side-by-side:
- `dgc` → free (at `~/.dual-graph/`)
- `dgc-pro` → pro (at `~/.graperoot-pro/`)

Projects can have both MCP servers registered simultaneously in `.mcp.json`; Claude Code will use whichever one you've configured in your CLAUDE.md.

---

## Uninstall

**macOS / Linux:**
```bash
rm -rf ~/.graperoot-pro
# Optional: clean shell rc
sed -i.bak '/graperoot-pro/d' ~/.zshrc ~/.bash_profile ~/.bashrc 2>/dev/null
```

**Windows (PowerShell):**
```powershell
Remove-Item -Recurse -Force "$HOME\.graperoot-pro"
$p = [Environment]::GetEnvironmentVariable("PATH","User") -split ";" |
     Where-Object { $_ -notmatch "graperoot-pro" -and $_ }
[Environment]::SetEnvironmentVariable("PATH", ($p -join ";"), "User")
```

Your project files (`.dual-graph-pro/`) and license file are removed. Free install (if any) is untouched.

---

## Still stuck?

Email **support@graperoot.dev** with:
- Output of `dgc-pro --version`
- OS + version (e.g. "macOS 14.3")
- Python version (`python3 --version`)
- The exact error message + the 10 lines before it

Typical response time: same business day.
