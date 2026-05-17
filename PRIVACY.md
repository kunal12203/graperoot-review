# Privacy Policy

**Dual-Graph Context** collects minimal anonymous data to understand usage and improve the tool.

## What we collect

| Data | Description | Where stored |
|------|-------------|--------------|
| Install ID | Random UUID generated at install time — no hardware info | Our server |
| Platform | `linux`, `darwin`, or `windows` | Our server |
| Install tool | e.g. `install-sh`, `install-ps1` | Our server |
| Session timestamps | When you first installed and last used the tool | Our server |
| Error reports | Script step + error message on install/launch failures | Google Sheets (via webhook) |
| Feedback | Optional star rating + free-text improvement suggestion | Google Sheets (via webhook) |

## What we do NOT collect

- No hardware identifiers, MAC address, CPU serial, or device fingerprint
- No file names, project contents, code, or prompts
- No IP addresses stored
- No personal information (name, email) unless you voluntarily provide it

## How the install ID works

A random UUID (e.g. `a3f8c2...`) is generated on first install and saved to `~/.dual-graph/identity.json`. It is the same format on all platforms. It is **not** derived from your hardware — it is purely random and cannot be used to identify you or your machine.

On reinstall, your existing ID is preserved so your usage history stays continuous.

## When data is sent

- **On each MCP session start** — a ping with your install ID, platform, and tool is sent to our server to update your last-seen timestamp
- **On install/launch errors** — error message and script step sent to Google Sheets to help us debug issues
- **Once, ~7 days after install** — an optional one-time prompt asks for a star rating and improvement suggestion (you can skip it)

## How to opt out

Delete or clear your identity file:

```bash
# macOS / Linux
rm ~/.dual-graph/identity.json

# Windows (PowerShell)
Remove-Item "$env:USERPROFILE\.dual-graph\identity.json"
```

A new random ID will be created on the next session. To stop pings permanently, remove the file before each session or uninstall the tool.

## Data retention

Usage data is retained indefinitely to track long-term active users. Error and feedback data in Google Sheets is reviewed periodically and not shared with third parties.

## Contact

Questions? Open an issue at https://github.com/kunal12203/Codex-CLI-Compact/issues
