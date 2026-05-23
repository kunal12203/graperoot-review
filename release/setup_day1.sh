#!/usr/bin/env bash
# Day 1 setup — run ONCE end-to-end. Stops before operations that need your input.
set -Eeuo pipefail

WORKER_DIR="/Users/krishnakant/Documents/Personal Projects/GrapeRoot Pro/release/worker"

banner() { echo; echo "════════════════════════════════════════════════════════════"; echo " $1"; echo "════════════════════════════════════════════════════════════"; }
pause() { echo; read -p "Press ENTER when done with the browser step above…"; echo; }

# ── 0. Prereqs ─────────────────────────────────────────────────────────────
banner "0.  Checking CLI tools"
command -v gh >/dev/null       || { echo "brew install gh"; exit 1; }
command -v wrangler >/dev/null || { echo "npm install -g wrangler"; exit 1; }
command -v openssl >/dev/null  || { echo "openssl required"; exit 1; }
gh auth status >/dev/null 2>&1  || { echo "Run: gh auth login"; exit 1; }
wrangler whoami >/dev/null 2>&1 || { echo "Run: wrangler login"; exit 1; }
echo "✓ gh + wrangler + openssl ready"

# ── 1. Admin token ─────────────────────────────────────────────────────────
banner "1.  Generating ADMIN_TOKEN (save it!)"
ADMIN_TOKEN=$(openssl rand -hex 24)
cat <<EOF

╔══════════════════════════════════════════════════════════════════════╗
║                        YOUR ADMIN TOKEN                              ║
║                                                                      ║
║   $ADMIN_TOKEN   ║
║                                                                      ║
║  → Save to password manager NOW. Shown only once here.               ║
║  → You need it to issue/revoke customer licenses.                    ║
╚══════════════════════════════════════════════════════════════════════╝
EOF
read -p "Saved? Press ENTER to continue… "

# ── 2. GitHub repos ────────────────────────────────────────────────────────
banner "2.  Creating GitHub repos"
gh repo create kunal12203/graperoot-pro-releases --private \
  --description "GrapeRoot Pro release tarballs (license-gated)" 2>&1 | grep -v "already exists" || \
  echo "• graperoot-pro-releases already exists — OK"
gh repo create kunal12203/graperoot-pro-public --public \
  --description "GrapeRoot Pro launcher scripts and installers" 2>&1 | grep -v "already exists" || \
  echo "• graperoot-pro-public already exists — OK"

# ── 3. GitHub PAT (browser) ────────────────────────────────────────────────
banner "3.  GitHub PAT — BROWSER STEP"
cat <<'EOF'
Open: https://github.com/settings/tokens?type=beta

Click "Generate new token" → fill in:

  • Token name:          graperoot-pro-worker
  • Expiration:          1 year
  • Resource owner:      kunal12203
  • Repository access:   Only select repositories
                         → add: kunal12203/graperoot-pro-releases
  • Permissions → Repository → Contents: Read-only

Click "Generate token" → COPY the "github_pat_..." value.

(Don't paste it here in the terminal; you'll paste it into wrangler on the next step.)
EOF
pause

# ── 4. KV namespace + wrangler.toml patch ──────────────────────────────────
banner "4.  Creating KV namespace"
cd "$WORKER_DIR"
echo "Running:  wrangler kv namespace create LICENSES"
echo "Look for a line like:   id = \"abc123def456...\"  (32 hex chars)"
echo
# Run directly (no capture) so wrangler sees a real TTY and uses its OAuth login
wrangler kv namespace create LICENSES || echo "(if 'already exists', you can reuse its id from: wrangler kv namespace list)"
echo
read -p "Paste the KV namespace id from the output above: " KV_ID
if [ -z "$KV_ID" ]; then
  echo "No id provided — aborting. Re-run the script and paste it when prompted."
  exit 1
fi
sed -i '' "s/REPLACE_WITH_KV_ID/$KV_ID/" wrangler.toml 2>/dev/null || true
grep -A1 kv_namespaces wrangler.toml
echo "Patching wrangler.toml with KV id: $KV_ID"
sed -i '' "s/REPLACE_WITH_KV_ID/$KV_ID/" wrangler.toml 2>/dev/null || true
grep -A1 kv_namespaces wrangler.toml

# ── 5. Secrets ──────────────────────────────────────────────────────────────
banner "5.  Uploading secrets to Cloudflare Worker"
echo
echo "About to push 2 secrets. For each, wrangler will prompt:  'Enter a secret value:'"
echo "  1) ADMIN_TOKEN — paste the token printed in Step 1 ($ADMIN_TOKEN)"
echo "  2) GITHUB_TOKEN — paste the github_pat_... from Step 3"
echo
read -p "Ready? Press ENTER to push ADMIN_TOKEN… "
wrangler secret put ADMIN_TOKEN
echo
read -p "Now press ENTER to push GITHUB_TOKEN… "
wrangler secret put GITHUB_TOKEN

# ── 6. Deploy ──────────────────────────────────────────────────────────────
banner "6.  Deploying Worker"
wrangler deploy

# ── 7. Health check ────────────────────────────────────────────────────────
banner "7.  Testing /health"
echo "If graperoot.dev isn't fully active yet, the api.graperoot.dev route may lag by 1-5 min."
echo "Trying /health on the workers.dev subdomain first (always works immediately)…"
WORKERS_URL=$(wrangler deployments list 2>/dev/null | grep -oE 'https://graperoot-pro-license\.[^ )]+\.workers\.dev' | head -1 || true)
if [ -z "$WORKERS_URL" ]; then
  echo "(Could not auto-detect workers.dev URL. It was printed above by 'wrangler deploy'.)"
else
  echo "  $WORKERS_URL/health"
  curl -s "$WORKERS_URL/health" | python3 -m json.tool || echo "(retry in a moment)"
fi
echo
echo "Now testing api.graperoot.dev …"
sleep 2
curl -s https://api.graperoot.dev/health | python3 -m json.tool 2>/dev/null || echo "api.graperoot.dev not responding yet — this is OK for up to 5 min after first deploy."

banner "✅  Day 1 complete"
cat <<EOF

What you just accomplished:
  ✓ Cloudflare Worker deployed with KV-backed license storage
  ✓ Worker bound to api.graperoot.dev
  ✓ ADMIN_TOKEN set (test it by issuing a license)
  ✓ GITHUB_TOKEN set (Worker can fetch private release tarballs)
  ✓ Both GitHub repos exist

NEXT:
  → Reply when api.graperoot.dev/health returns {"ok":true,...}
  → I'll walk you through Day 2 (first tarball + public assets)
EOF
