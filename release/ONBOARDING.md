# Welcome to GrapeRoot Pro

Thank you for choosing GrapeRoot Pro. Your license is active and ready to use.

---

## Your License

```
License Key:  {{ LICENSE_KEY }}
Customer:     {{ CUSTOMER_NAME }}
Seats:        {{ SEATS }}
Expires:      {{ EXPIRES }}
Support:      support@graperoot.dev
```

> Keep this key confidential. It is bound to your seat count.

---

## Install — one command

**macOS / Linux**

```bash
curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- {{ LICENSE_KEY }}
```

**Windows (PowerShell)**

```powershell
$env:GRAPEROOT_LICENSE_KEY = "{{ LICENSE_KEY }}"
irm https://graperoot.dev/pro/install.ps1 | iex
```

**Requirements:** Python ≥ 3.10 · Claude Code CLI (the installer offers to install it if missing).

What the installer does:

1. Verifies your license against our server.
2. Downloads the Pro package to `~/.graperoot-pro/` (Unix) or `%USERPROFILE%\.graperoot-pro\` (Windows).
3. Creates an isolated Python environment — never touches your system Python.
4. Registers the `dgc-pro` command on your PATH.
5. If you have GrapeRoot Free installed, leaves it 100% untouched.

---

## Quick Start

```bash
# macOS / Linux
source ~/.zshrc                    # reload PATH
dgc-pro /path/to/your/project

# Windows — open a new terminal, then:
dgc-pro C:\path\to\your\project
```

First run scans your project and builds the dual-graph index (one-time, ~2 min per 10k files). Subsequent runs start in seconds.

---

## Coexistence with GrapeRoot Free

| | GrapeRoot Free | **GrapeRoot Pro** |
|---|---|---|
| Install path | `~/.dual-graph/` | `~/.graperoot-pro/` |
| Command | `dgc` | **`dgc-pro`** |
| MCP server name | `dual-graph` | **`graperoot-pro`** |
| Project data dir | `.dual-graph/` | `.dual-graph-pro/` |

Both run side-by-side. Your existing `dgc` workflows continue unchanged.

---

## What's in Pro v1.0

- **Symbol-level reads** — `file::symbol` returns a 30-line function body instead of the whole 400-line file
- **Priority-queue action memory** — retains meaningful reads and edits across sessions, not just the most recent
- **Confidence-calibrated retrieval** — tells Claude when to stop, with a safety-net grep when the signal is weak
- **Cross-turn deduplication** — files already read in the session return a pointer, not the bytes

**Benchmark results** (7,881-file Go codebase, 25 engineering prompts, Claude Sonnet 4.6):

| | GrapeRoot Free | **GrapeRoot Pro v1.0** |
|---|---|---|
| Avg quality (0-100) | 85.0 | **86.4** |
| Avg cost per prompt | $0.84 | **$0.59**  (−29%) |
| Top-tier (Q ≥ 90) | 7 of 25 | **13 of 25** |

---

## Updates

Pro auto-updates on each launch — it checks for a newer version, pulls the update bundle (license-gated), and shows you the changelog. No manual action needed.

---

## Troubleshooting

**License not accepted**
Contact support@graperoot.dev with your key. Verify `~/.graperoot-pro/license.key` is present and readable.

**`dgc-pro: command not found`**
- *macOS / Linux:* restart your shell, or `source ~/.zshrc` / `source ~/.bashrc`.
- *Windows:* open a **new** terminal so the updated PATH is picked up.

**Offline use**
Pro caches your license for 24h between verifications and supports 7 days of fully-offline operation on a single cached verification.

**Uninstall**

*macOS / Linux:*
```bash
rm -rf ~/.graperoot-pro
```

*Windows (PowerShell):*
```powershell
Remove-Item -Recurse -Force "$HOME\.graperoot-pro"
$p = [Environment]::GetEnvironmentVariable("PATH","User") -split ";" |
     Where-Object { $_ -notmatch "graperoot-pro" -and $_ }
[Environment]::SetEnvironmentVariable("PATH", ($p -join ";"), "User")
```

Your free install and project files are untouched.

---

## Support

- **Email:** support@graperoot.dev
- **Docs:** https://graperoot.dev/pro/docs
- **Private Slack:** invite link in your welcome email

Enterprise features — SSO license activation, custom indexing rules, on-prem license server — are available on the Enterprise tier.
