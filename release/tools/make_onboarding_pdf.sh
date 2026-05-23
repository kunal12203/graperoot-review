#!/usr/bin/env bash
# Generate a customer-specific onboarding PDF from the template.
# Usage: make_onboarding_pdf.sh "GRP-XXXX-XXXX-XXXX" "Customer Name" <seats> "YYYY-MM-DD-or-perpetual"
set -Eeuo pipefail

if [ $# -lt 4 ]; then
  echo "Usage: $0 <LICENSE_KEY> <CUSTOMER_NAME> <SEATS> <EXPIRES>"
  echo "Example: $0 GRP-ABCD-EFGH-IJKL \"Acme Corp\" 5 2027-04-22"
  exit 1
fi

LICENSE_KEY="$1"
CUSTOMER_NAME="$2"
SEATS="$3"
EXPIRES="$4"

TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/ONBOARDING.md"
OUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/customers"
mkdir -p "$OUT_DIR"

# Sanitize customer name for filename
SAFE_NAME=$(echo "$CUSTOMER_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')
STAMP=$(date +%Y%m%d)
MD_OUT="$OUT_DIR/${SAFE_NAME}_${STAMP}_Onboarding.md"
PDF_OUT="$OUT_DIR/${SAFE_NAME}_${STAMP}_Onboarding.pdf"

# Fill template
sed \
  -e "s|{{ LICENSE_KEY }}|$LICENSE_KEY|g" \
  -e "s|{{ CUSTOMER_NAME }}|$CUSTOMER_NAME|g" \
  -e "s|{{ SEATS }}|$SEATS|g" \
  -e "s|{{ EXPIRES }}|$EXPIRES|g" \
  "$TEMPLATE" > "$MD_OUT"

echo "✓ Filled template → $MD_OUT"

# Generate PDF if pandoc + weasyprint are available
if command -v pandoc >/dev/null && command -v weasyprint >/dev/null; then
  STYLE=/tmp/graperoot_onboarding_style.css
  cat > "$STYLE" <<'EOF'
@page { size: A4; margin: 1in; @bottom-right { content: counter(page) " / " counter(pages); font-size: 9pt; color: #666; } }
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 10.5pt; line-height: 1.5; color: #111; }
h1 { color: #1a3c6e; font-size: 22pt; border-bottom: 3px solid #1a3c6e; padding-bottom: 8px; page-break-after: avoid; }
h2 { color: #1a3c6e; font-size: 15pt; margin-top: 1.6em; border-bottom: 1px solid #ccc; padding-bottom: 4px; page-break-after: avoid; }
h3 { color: #2a4a7a; font-size: 12pt; margin-top: 1.1em; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 9.5pt; page-break-inside: avoid; }
th { background: #1a3c6e; color: white; padding: 7px 9px; text-align: left; font-weight: 600; }
th strong, th b { color: white; }
td { padding: 6px 9px; border-bottom: 1px solid #e0e0e0; }
tr:nth-child(even) td { background: #f7f9fc; }
strong { color: #0b1e3b; }
code { background: #f0f2f5; padding: 1px 5px; border-radius: 3px; font-size: 0.92em; font-family: "SF Mono", Menlo, Consolas, monospace; }
pre { background: #0b1e3b; color: #f5f7fb; padding: 10px 14px; border-radius: 6px; font-size: 9pt; overflow-x: auto; }
pre code { background: transparent; color: inherit; padding: 0; }
blockquote { border-left: 4px solid #1a3c6e; padding: 0.5em 1em; background: #f7f9fc; color: #333; margin: 1em 0; }
hr { border: none; border-top: 1px solid #ccc; margin: 2em 0; }
ul, ol { padding-left: 1.4em; }
li { margin: 0.25em 0; }
EOF
  pandoc "$MD_OUT" --pdf-engine=weasyprint --css="$STYLE" --standalone -o "$PDF_OUT" 2>/dev/null || {
    echo "⚠ pandoc failed — check $MD_OUT and run pandoc manually."
    exit 1
  }
  echo "✓ Generated PDF → $PDF_OUT"
else
  echo "⚠ pandoc or weasyprint not installed. The Markdown is ready at $MD_OUT."
  echo "   Install: brew install pandoc && pip3 install --user weasyprint"
fi

# Email template
cat <<EOF

════════════════════════════════════════════════════════════
  EMAIL TEMPLATE — copy into your mail client
════════════════════════════════════════════════════════════

To:       <client-email>
Subject:  Welcome to GrapeRoot Pro

Hi <Name>,

Your GrapeRoot Pro access is ready. Attached is your onboarding
guide with your license key and install instructions.

Install in one command — pick your OS:

macOS / Linux:
  curl -fsSL https://graperoot.dev/pro/install.sh | bash -s -- $LICENSE_KEY

Windows (PowerShell):
  \$env:GRAPEROOT_LICENSE_KEY = "$LICENSE_KEY"
  irm https://graperoot.dev/pro/install.ps1 | iex

Any questions, reply to this email or reach us at support@graperoot.dev.

— Kunal

════════════════════════════════════════════════════════════

Attach:  $PDF_OUT
EOF
