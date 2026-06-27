#!/usr/bin/env python3
"""security.py — Phase 5 security scanning tools for GrapeRoot Pro MCP server.

Provides:
  5A. Scanners:
      scan_secrets()        — hardcoded credential detection
      scan_sast()           — static analysis (Python, JS/TS, Go, Java/Kotlin)
      scan_vulnerabilities() — OSV.dev dependency vulnerability check
      scan_licenses()       — license audit across lock files
      scan_env_parity()     — Docker Compose vs Kubernetes env-var comparison

  5B. Reporting:
      generate_sbom()       — SBOM in summary or CycloneDX 1.4 JSON format
      compute_debt_score()  — weighted security debt score (0-100, A-F grade)

  Module interface:
      run_all_scanners()    — run every scanner, return combined report
      get_security_summary() — quick counts + debt score
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", "coverage",
    ".tox", ".eggs", "*.egg-info",
}

_SKIP_SECRET_PATHS = {
    "test", "tests", "__tests__",
}


def _is_binary(path: Path) -> bool:
    """Return True if the first 512 bytes contain a null byte (binary file)."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(512)
        return b"\x00" in chunk
    except OSError:
        return True


def _should_skip_dir(part: str) -> bool:
    return part in _SKIP_DIRS or part.endswith(".egg-info")


def _iter_text_files(root: Path, extra_skip_dirs: set[str] | None = None):
    """Yield all non-binary, <1 MB text files under root, skipping build dirs."""
    skip = _SKIP_DIRS | (extra_skip_dirs or set())
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(_should_skip_dir(p) or p in skip for p in path.parts):
            continue
        if path.stat().st_size > 1_048_576:  # 1 MB
            continue
        if _is_binary(path):
            continue
        yield path


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_lines(path: Path) -> list[str]:
    for enc in ("utf-8", "latin-1"):
        try:
            return path.read_text(encoding=enc).splitlines()
        except (UnicodeDecodeError, OSError):
            pass
    return []


# ---------------------------------------------------------------------------
# 5A-1  Secret Detection
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, str, str]] = [
    # (regex_pattern, type_label, severity)
    (r"AKIA[0-9A-Z]{16}", "aws_access_key_id", "high"),
    (r"aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}", "aws_secret_access_key", "critical"),
    (r"ghp_[A-Za-z0-9]{36}", "github_pat", "high"),
    (r"gho_[A-Za-z0-9]{36}", "github_oauth_token", "high"),
    (r"github_pat_[A-Za-z0-9_]{22,}", "github_fine_grained_pat", "high"),
    (r"sk-[A-Za-z0-9]{48}", "openai_api_key", "critical"),
    (r"sk-proj-[A-Za-z0-9\-_]{20,}", "openai_project_key", "critical"),
    (r"xoxb-[0-9]{11}-[0-9]{11}-[A-Za-z0-9]{24}", "slack_bot_token", "critical"),
    (r"xoxp-[0-9A-Za-z\-]+", "slack_user_token", "high"),
    (r"-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH)\s+PRIVATE\s+KEY-----", "private_key", "critical"),
    (r"mongodb\+srv://[^:]+:[^@]+@", "mongodb_credentials", "critical"),
    (r"postgres(?:ql)?://[^:]+:[^@]+@", "postgresql_credentials", "critical"),
    (r"mysql://[^:]+:[^@]+@", "mysql_credentials", "critical"),
    (r"redis://:([^@]+)@", "redis_password", "high"),
    (r"""password\s*=\s*["'][^"']{8,}["']""", "hardcoded_password", "medium"),
    (r"""secret\s*=\s*["'][^"']{8,}["']""", "hardcoded_secret", "medium"),
    (r"""api_key\s*=\s*["'][A-Za-z0-9\-_]{16,}["']""", "generic_api_key", "medium"),
    (r"Authorization:\s*Bearer\s+[A-Za-z0-9\-._~+/]+=*", "bearer_token", "high"),
    (r"""PRIVATE_KEY\s*=\s*["']-----""", "private_key_env", "critical"),
    # Stripe
    (r"sk_live_[A-Za-z0-9]{24,}", "stripe_secret_key", "critical"),
    (r"rk_live_[A-Za-z0-9]{24,}", "stripe_restricted_key", "critical"),
    (r"pk_live_[A-Za-z0-9]{24,}", "stripe_publishable_key", "medium"),
    # Google
    (r"AIza[0-9A-Za-z\-_]{35}", "google_api_key", "high"),
    (r'"type":\s*"service_account"', "gcp_service_account_json", "critical"),
    # Twilio
    (r"AC[a-f0-9]{32}", "twilio_account_sid", "high"),
    (r"SK[a-f0-9]{32}", "twilio_api_key", "high"),
    # SendGrid
    (r"SG\.[A-Za-z0-9\-_]{22}\.[A-Za-z0-9\-_]{43}", "sendgrid_api_key", "critical"),
    # Anthropic
    (r"sk-ant-[A-Za-z0-9\-_]{40,}", "anthropic_api_key", "critical"),
    # npm
    (r"npm_[A-Za-z0-9]{36}", "npm_token", "high"),
    # Docker Hub
    (r"dckr_pat_[A-Za-z0-9\-_]{27}", "dockerhub_pat", "high"),
    # Azure
    (r"DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{88}", "azure_storage_connection_string", "critical"),
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}.*client.?secret", "azure_client_secret", "high"),
    # HashiCorp Vault
    (r"hvs\.[A-Za-z0-9]{24,}", "vault_token", "critical"),
    (r"hvb\.[A-Za-z0-9]{24,}", "vault_batch_token", "high"),
    # Okta
    (r"00[A-Za-z0-9\-_]{38}", "okta_api_token", "high"),
    # Datadog
    (r"DD_API_KEY\s*=\s*[A-Za-z0-9]{32}", "datadog_api_key", "high"),
    # Grafana
    (r"glsa_[A-Za-z0-9]{32}_[A-Za-z0-9]{8}", "grafana_service_account", "high"),
    # Algolia
    (r"[A-Za-z0-9]{32}.*algolia", "algolia_api_key", "medium"),
    # Mapbox
    (r"pk\.eyJ1IjoiJ[A-Za-z0-9\._-]{50,}", "mapbox_token", "medium"),
    # Vercel
    (r"vercel_[A-Za-z0-9_-]{24,}", "vercel_token", "high"),
]

_COMPILED_SECRETS = [
    (re.compile(pat, re.IGNORECASE), label, sev)
    for pat, label, sev in _SECRET_PATTERNS
]

_SUPPRESS_MARKERS = (
    "# noqa", "# nosec", "// nosec",
    "example", "placeholder", "<your-", "YOUR_", "REPLACE_ME",
    "your_", "<YOUR", "EXAMPLE", "DUMMY", "FAKE",
    "test_", "testing", "sample_", "demo_", "<placeholder>", "INSERT_HERE",
)

_SECRET_SKIP_EXT = {".md", ".rst", ".txt"}  # skip obvious docs? No — scan all per spec
_SECRET_SKIP_PATH_PARTS = {"node_modules", ".git", "venv", ".venv", "__pycache__"}


def _mask_value(match_str: str) -> str:
    """Return first 8 chars + *** for the matched value."""
    if len(match_str) <= 8:
        return match_str[:4] + "***"
    return match_str[:8] + "***"


def _mask_line(line: str, match_str: str) -> str:
    """Replace the matched secret in the line with a masked version."""
    masked = _mask_value(match_str)
    return line.replace(match_str, masked, 1)


def scan_secrets(project_root: str) -> list[dict]:
    """Scan all files under project_root for hardcoded secrets.

    Returns a list of finding dicts; see module docstring for schema.
    """
    root = Path(project_root).resolve()
    findings: list[dict] = []

    skip_path_keywords = _SECRET_SKIP_PATH_PARTS | _SKIP_SECRET_PATHS

    for filepath in _iter_text_files(root):
        rel_path = _rel(filepath, root)

        # Skip test/spec files and markdown
        rel_lower = rel_path.lower()
        if any(s in rel_lower for s in (".test.", ".spec.", "test/", "tests/", ".md")):
            continue
        if filepath.suffix.lower() in {".md"}:
            continue

        lines = _read_lines(filepath)
        for lineno, line in enumerate(lines, start=1):
            # Skip suppressed lines
            if any(marker.lower() in line.lower() for marker in _SUPPRESS_MARKERS):
                continue

            for compiled_re, label, severity in _COMPILED_SECRETS:
                m = compiled_re.search(line)
                if not m:
                    continue
                match_str = m.group(0)
                findings.append({
                    "file": rel_path,
                    "line": lineno,
                    "type": label,
                    "severity": severity,
                    "match": _mask_value(match_str),
                    "context": _mask_line(line.strip(), match_str),
                })

    return findings


# ---------------------------------------------------------------------------
# 5A-2  SAST Scanner
# ---------------------------------------------------------------------------

_SAST_RULES: dict[str, list[dict]] = {
    "python": [
        {
            "id": "PY001",
            "pattern": r"(?<!#\s)eval\s*\(",
            "title": "Use of eval() — arbitrary code execution",
            "severity": "high",
            "cwe": "CWE-95",
            "fix": "Avoid eval(); use ast.literal_eval() for safe expression parsing.",
        },
        {
            "id": "PY002",
            "pattern": r"\bexec\s*\(",
            "title": "Use of exec() — arbitrary code execution",
            "severity": "high",
            "cwe": "CWE-95",
            "fix": "Remove exec(); refactor to use explicit function calls.",
        },
        {
            "id": "PY003",
            "pattern": r"subprocess\.[a-zA-Z_]+\s*\(.*shell\s*=\s*True",
            "title": "subprocess called with shell=True — command injection risk",
            "severity": "critical",
            "cwe": "CWE-78",
            "fix": "Pass a list of arguments instead; set shell=False (the default).",
        },
        {
            "id": "PY004",
            "pattern": r"pickle\.loads\s*\(",
            "title": "Unsafe pickle deserialization",
            "severity": "high",
            "cwe": "CWE-502",
            "fix": "Use JSON or a safe serialization format. Never unpickle untrusted data.",
        },
        {
            "id": "PY005",
            "pattern": r"yaml\.load\s*\(",
            "title": "yaml.load() without safe Loader — deserialization risk",
            "severity": "medium",
            "cwe": "CWE-502",
            "fix": "Use yaml.safe_load() or pass Loader=yaml.SafeLoader explicitly.",
            "false_positive_filter": r"Loader\s*=",
        },
        {
            "id": "PY006",
            "pattern": r"os\.system\s*\(",
            "title": "os.system() — command injection risk",
            "severity": "high",
            "cwe": "CWE-78",
            "fix": "Use subprocess.run() with a list of arguments instead.",
        },
        {
            "id": "PY007",
            "pattern": r"cursor\.execute\s*\(.*(?:%[sd]|\.format\s*\()",
            "title": "Potential SQL injection via string formatting in cursor.execute()",
            "severity": "critical",
            "cwe": "CWE-89",
            "fix": "Use parameterized queries: cursor.execute(sql, (param,))",
        },
        {
            "id": "PY008",
            "pattern": r"hashlib\.(md5|sha1)\s*\(",
            "title": "Weak cryptographic hash (MD5/SHA-1)",
            "severity": "low",
            "cwe": "CWE-328",
            "fix": "Use hashlib.sha256() or stronger.",
        },
        {
            "id": "PY009",
            "pattern": r"random\.(random|randint)\s*\(",
            "title": "Non-cryptographic RNG used near sensitive context",
            "severity": "medium",
            "cwe": "CWE-330",
            "fix": "Use secrets module for token/key/password generation.",
            "context_filter": r"(?i)(token|secret|password|key)",
        },
        {
            "id": "PY010",
            "pattern": r"tempfile\.mktemp\s*\(",
            "title": "Insecure temp file via tempfile.mktemp()",
            "severity": "medium",
            "cwe": "CWE-377",
            "fix": "Use tempfile.mkstemp() or tempfile.NamedTemporaryFile().",
        },
        {
            "id": "PY011",
            "pattern": r"(?i)(flask\.request\.args\.get.*sql|SELECT.*FROM.*request\.args|INSERT.*request\.form)",
            "title": "Possible SQL injection via Flask request data",
            "severity": "high",
            "cwe": "CWE-89",
            "fix": "Use parameterized queries; never concatenate user input into SQL.",
        },
        {
            "id": "PY012",
            "pattern": r"\bassert\s+",
            "title": "assert statement in production code (disabled with -O)",
            "severity": "low",
            "cwe": "N/A",
            "fix": "Replace assert with explicit if/raise for runtime checks.",
        },
        {
            "id": "PY013",
            "pattern": r"__import__\s*\(",
            "title": "Dynamic __import__() — code injection risk",
            "severity": "medium",
            "cwe": "CWE-95",
            "fix": "Use importlib.import_module() with a validated module allowlist.",
        },
        {
            "id": "PY014",
            "pattern": r"marshal\.loads\s*\(",
            "title": "Unsafe marshal deserialization",
            "severity": "high",
            "cwe": "CWE-502",
            "fix": "Avoid marshal for untrusted data; use JSON or a safe format.",
        },
    ],
    "javascript": [
        {
            "id": "JS001",
            "pattern": r"\beval\s*\(",
            "title": "Use of eval() — code injection risk",
            "severity": "high",
            "cwe": "CWE-95",
            "fix": "Remove eval(); parse JSON with JSON.parse(), refactor logic.",
        },
        {
            "id": "JS002",
            "pattern": r"innerHTML\s*[\+]?=",
            "title": "innerHTML assignment — XSS risk",
            "severity": "high",
            "cwe": "CWE-79",
            "fix": "Use textContent or DOMPurify to sanitize HTML before insertion.",
        },
        {
            "id": "JS003",
            "pattern": r"document\.write\s*\(",
            "title": "document.write() — XSS risk",
            "severity": "high",
            "cwe": "CWE-79",
            "fix": "Use DOM APIs (createElement, appendChild) instead.",
        },
        {
            "id": "JS004",
            "pattern": r"dangerouslySetInnerHTML",
            "title": "dangerouslySetInnerHTML used — review for XSS",
            "severity": "medium",
            "cwe": "CWE-79",
            "fix": "Sanitize content with DOMPurify before passing to dangerouslySetInnerHTML.",
        },
        {
            "id": "JS005",
            "pattern": r"child_process\.exec\s*\(",
            "title": "child_process.exec() — command injection risk",
            "severity": "critical",
            "cwe": "CWE-78",
            "fix": "Use child_process.execFile() or spawn() with an argument array.",
        },
        {
            "id": "JS006",
            "pattern": r"require\s*\(\s*['\"]child_process['\"]\s*\)\.exec\s*\(",
            "title": "Inline require('child_process').exec() — command injection",
            "severity": "critical",
            "cwe": "CWE-78",
            "fix": "Use execFile() or spawn() with an explicit argument array.",
        },
        {
            "id": "JS007",
            "pattern": r"window\.location(?:\.href)?\s*=",
            "title": "Open redirect risk via window.location assignment",
            "severity": "medium",
            "cwe": "CWE-601",
            "fix": "Validate redirect URLs against an allowlist before assigning.",
        },
        {
            "id": "JS008",
            "pattern": r"crypto\.createHash\s*\(\s*['\"](?:md5|sha1)['\"]\s*\)",
            "title": "Weak cryptographic hash (MD5/SHA-1)",
            "severity": "low",
            "cwe": "CWE-328",
            "fix": "Use crypto.createHash('sha256') or stronger.",
        },
        {
            "id": "JS009",
            "pattern": r"Math\.random\s*\(",
            "title": "Non-cryptographic RNG near sensitive context",
            "severity": "medium",
            "cwe": "CWE-330",
            "fix": "Use crypto.randomBytes() or crypto.randomUUID() for tokens.",
            "context_filter": r"(?i)(token|secret|password|key)",
        },
        {
            "id": "JS010",
            "pattern": r"localStorage\.setItem\s*\(.*(?:token|password|secret)",
            "title": "Sensitive data stored in localStorage",
            "severity": "medium",
            "cwe": "CWE-312",
            "fix": "Use sessionStorage or httpOnly cookies for sensitive tokens.",
        },
        {
            "id": "JS011",
            "pattern": r"document\.cookie\s*=",
            "title": "Direct document.cookie assignment — review cookie flags",
            "severity": "low",
            "cwe": "CWE-614",
            "fix": "Ensure Secure and HttpOnly flags are set; prefer server-side Set-Cookie.",
        },
        {
            "id": "JS012",
            "pattern": r"\bnew\s+Function\s*\(",
            "title": "new Function() — dynamic code execution risk",
            "severity": "high",
            "cwe": "CWE-95",
            "fix": "Avoid new Function(); refactor to use explicit function definitions.",
        },
        {
            "id": "JS013",
            "pattern": r"(?:setTimeout|setInterval)\s*\(\s*['\"]",
            "title": "String passed to setTimeout/setInterval — implicit eval",
            "severity": "medium",
            "cwe": "CWE-95",
            "fix": "Pass a function reference, not a string, to setTimeout/setInterval.",
        },
    ],
    "go": [
        {
            "id": "GO001",
            "pattern": r"exec\.Command\s*\(.*shell",
            "title": "Shell command execution via exec.Command — injection risk",
            "severity": "high",
            "cwe": "CWE-78",
            "fix": "Pass validated arguments directly; avoid shell metacharacters.",
        },
        {
            "id": "GO002",
            "pattern": r'(?i)fmt\.Sprintf\s*\(.*(?:SELECT|INSERT|UPDATE|DELETE).*%[sv]',
            "title": "SQL query built with fmt.Sprintf — injection risk",
            "severity": "critical",
            "cwe": "CWE-89",
            "fix": "Use parameterized queries (db.Query with ? placeholders).",
        },
        {
            "id": "GO003",
            "pattern": r"md5\.New\s*\(\s*\)|sha1\.New\s*\(\s*\)",
            "title": "Weak cryptographic hash (MD5/SHA-1)",
            "severity": "low",
            "cwe": "CWE-328",
            "fix": "Use crypto/sha256 or crypto/sha512.",
        },
        {
            "id": "GO004",
            "pattern": r'"math/rand"',
            "title": "math/rand imported — non-cryptographic RNG",
            "severity": "medium",
            "cwe": "CWE-330",
            "fix": "Use crypto/rand for security-sensitive random values.",
        },
        {
            "id": "GO005",
            "pattern": r'http\.ListenAndServe\s*\(\s*":[0-9]',
            "title": "HTTP server without TLS (ListenAndServe)",
            "severity": "low",
            "cwe": "N/A",
            "fix": "Use http.ListenAndServeTLS() in production environments.",
        },
    ],
    "java": [
        {
            "id": "JV001",
            "pattern": r"Runtime\.getRuntime\s*\(\s*\)\.exec\s*\(",
            "title": "Runtime.exec() — command injection risk",
            "severity": "critical",
            "cwe": "CWE-78",
            "fix": "Use ProcessBuilder with a validated argument list.",
        },
        {
            "id": "JV002",
            "pattern": r"Statement\.execute\s*\(.*\+",
            "title": "SQL statement built with string concatenation — injection risk",
            "severity": "critical",
            "cwe": "CWE-89",
            "fix": "Use PreparedStatement with parameterized queries.",
        },
        {
            "id": "JV003",
            "pattern": r'MessageDigest\.getInstance\s*\(\s*"(?:MD5|SHA-1)"\s*\)',
            "title": "Weak cryptographic hash (MD5/SHA-1)",
            "severity": "low",
            "cwe": "CWE-328",
            "fix": 'Use MessageDigest.getInstance("SHA-256") or stronger.',
        },
        {
            "id": "JV004",
            "pattern": r"\bObjectInputStream\b",
            "title": "ObjectInputStream usage — deserialization risk",
            "severity": "high",
            "cwe": "CWE-502",
            "fix": "Validate/filter classes before deserialization; consider JSON/Protobuf.",
        },
        {
            "id": "JV005",
            "pattern": r"System\.exit\s*\(",
            "title": "System.exit() call — abrupt JVM termination",
            "severity": "low",
            "cwe": "N/A",
            "fix": "Throw an exception or return an error code instead.",
        },
    ],
    "csharp": [
        {"id": "CS001", "pattern": r"(?:Process\.Start|\.Start)\s*\(", "title": "Process.Start() — potential command injection", "severity": "high", "cwe": "CWE-78", "fix": "Avoid Process.Start with untrusted input; validate all args."},
        {"id": "CS002", "pattern": r"SqlCommand\s*\(", "title": "SqlCommand construction — review for SQL injection", "severity": "high", "cwe": "CWE-89", "fix": "Use parameterized queries with SqlParameter."},
        {"id": "CS003", "pattern": r"Response\.Write\s*\(", "title": "Response.Write() — XSS risk", "severity": "medium", "cwe": "CWE-79", "fix": "Use HtmlEncoder.Default.Encode() before writing user input."},
        {"id": "CS004", "pattern": r"BinaryFormatter\(\)", "title": "BinaryFormatter deserialization — arbitrary code execution", "severity": "critical", "cwe": "CWE-502", "fix": "Use System.Text.Json or XmlSerializer instead."},
        {"id": "CS005", "pattern": r"MD5\.Create\(\)|SHA1\.Create\(\)", "title": "Weak hash (MD5/SHA-1) — not collision resistant", "severity": "medium", "cwe": "CWE-327", "fix": "Use SHA-256 or SHA-512 via SHA256.Create()."},
        {"id": "CS006", "pattern": r'allowUnsafeUpdates\s*=\s*true', "title": "allowUnsafeUpdates=true (SharePoint) — CSRF risk", "severity": "high", "cwe": "CWE-352", "fix": "Remove allowUnsafeUpdates or add proper validation."},
        {"id": "CS007", "pattern": r'new Random\(\)', "title": "System.Random — not cryptographically secure", "severity": "medium", "cwe": "CWE-338", "fix": "Use System.Security.Cryptography.RandomNumberGenerator instead."},
        {"id": "CS008", "pattern": r'catch\s*\(\s*Exception\s+\w+\s*\)\s*\{[^}]*\}', "title": "Broad catch(Exception) — hides bugs", "severity": "low", "cwe": "CWE-390", "fix": "Catch specific exception types."},
    ],
    "ruby": [
        {"id": "RB001", "pattern": r'\beval\s*[(\s]', "title": "eval() — arbitrary code execution", "severity": "high", "cwe": "CWE-95", "fix": "Avoid eval; refactor to explicit logic."},
        {"id": "RB002", "pattern": r'`[^`]+`|\bsystem\s*\(|\bexec\s*\(|\bspawn\s*\(', "title": "Shell command execution — command injection risk", "severity": "high", "cwe": "CWE-78", "fix": "Use Open3 with an array form to avoid shell interpolation."},
        {"id": "RB003", "pattern": r'ActiveRecord.*where\s*\(["\'][^"\']*#\{', "title": "ActiveRecord where() with string interpolation — SQL injection", "severity": "high", "cwe": "CWE-89", "fix": "Use parameterized queries: where('col = ?', value)."},
        {"id": "RB004", "pattern": r'render\s+inline\s*:', "title": "render inline: — XSS if user content rendered", "severity": "medium", "cwe": "CWE-79", "fix": "Use templates; avoid inline: with user-controlled data."},
        {"id": "RB005", "pattern": r'Marshal\.load\s*\(', "title": "Marshal.load — arbitrary object deserialization", "severity": "critical", "cwe": "CWE-502", "fix": "Use JSON.parse or a safe serializer instead."},
        {"id": "RB006", "pattern": r'params\[.*\].*\.constantize', "title": "String#constantize with user input — remote code execution", "severity": "critical", "cwe": "CWE-94", "fix": "Whitelist allowed class names before constantize."},
        {"id": "RB007", "pattern": r'Digest::MD5|Digest::SHA1', "title": "Weak hash — MD5/SHA1 not collision-safe", "severity": "medium", "cwe": "CWE-327", "fix": "Use Digest::SHA256 or bcrypt for passwords."},
    ],
    "php": [
        {"id": "PHP001", "pattern": r'\beval\s*\(', "title": "eval() — arbitrary PHP execution", "severity": "critical", "cwe": "CWE-95", "fix": "Remove eval(); refactor to explicit logic."},
        {"id": "PHP002", "pattern": r'mysql_query\s*\(\s*["\'][^"\']*\$', "title": "mysql_query with variable interpolation — SQL injection", "severity": "critical", "cwe": "CWE-89", "fix": "Use PDO with prepared statements."},
        {"id": "PHP003", "pattern": r'\$_(?:GET|POST|REQUEST|COOKIE)\[[^\]]+\].*(?:echo|print|printf)', "title": "Echoing superglobal directly — XSS", "severity": "high", "cwe": "CWE-79", "fix": "Use htmlspecialchars() or htmlentities() before output."},
        {"id": "PHP004", "pattern": r'shell_exec\s*\(|passthru\s*\(|system\s*\(|exec\s*\(|popen\s*\(', "title": "Shell execution function — command injection", "severity": "high", "cwe": "CWE-78", "fix": "Avoid shell execution; use PHP native functions instead."},
        {"id": "PHP005", "pattern": r'unserialize\s*\(', "title": "unserialize() — arbitrary object injection", "severity": "critical", "cwe": "CWE-502", "fix": "Use json_decode() instead; never unserialize untrusted input."},
        {"id": "PHP006", "pattern": r'md5\s*\(|sha1\s*\(', "title": "Weak hash — md5/sha1 for passwords", "severity": "medium", "cwe": "CWE-327", "fix": "Use password_hash() with PASSWORD_BCRYPT or PASSWORD_ARGON2ID."},
        {"id": "PHP007", "pattern": r'file_get_contents\s*\(\s*\$_', "title": "file_get_contents with user-controlled path — LFI/SSRF", "severity": "high", "cwe": "CWE-73", "fix": "Validate and whitelist allowed paths before reading."},
        {"id": "PHP008", "pattern": r'include\s*\(\s*\$|require\s*\(\s*\$|include_once\s*\(\s*\$|require_once\s*\(\s*\$', "title": "include/require with variable — remote/local file inclusion", "severity": "critical", "cwe": "CWE-98", "fix": "Whitelist allowed files; never include() with user input."},
    ],
    "kotlin": [
        {"id": "KT001", "pattern": r'Runtime\.getRuntime\(\)\.exec\s*\(', "title": "Runtime.exec() — command injection risk", "severity": "high", "cwe": "CWE-78", "fix": "Use ProcessBuilder with a list to avoid shell injection."},
        {"id": "KT002", "pattern": r'MessageDigest\.getInstance\s*\(\s*"(?:MD5|SHA-1)"', "title": "Weak hash algorithm — MD5/SHA-1", "severity": "medium", "cwe": "CWE-327", "fix": "Use SHA-256 or SHA-512 instead."},
        {"id": "KT003", "pattern": r'ObjectInputStream\s*\(', "title": "ObjectInputStream — unsafe deserialization", "severity": "high", "cwe": "CWE-502", "fix": "Use JSON/Protobuf; validate class types if deserialization is required."},
        {"id": "KT004", "pattern": r'Log\.[dviwe]\s*\([^,]+,\s*(?:password|token|secret|key)', "title": "Logging sensitive data (Kotlin Log)", "severity": "medium", "cwe": "CWE-532", "fix": "Remove sensitive values from log statements."},
        {"id": "KT005", "pattern": r'\.execute\s*\(\s*"[^"]*\$\{', "title": "JDBC execute() with string template — SQL injection", "severity": "high", "cwe": "CWE-89", "fix": "Use PreparedStatement with parameters."},
    ],
}

_LANG_EXTENSIONS: dict[str, set[str]] = {
    "python": {".py"},
    "javascript": {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"},
    "go": {".go"},
    "java": {".java"},
    "csharp": {".cs", ".csx"},
    "ruby": {".rb", ".rake"},
    "php": {".php"},
    "kotlin": {".kt", ".kts"},
}

_COMPILED_SAST: dict[str, list[tuple[re.Pattern, re.Pattern | None, re.Pattern | None, dict]]] = {}

for _lang, _rules in _SAST_RULES.items():
    _compiled_rules = []
    for _rule in _rules:
        _pat = re.compile(_rule["pattern"], re.IGNORECASE)
        _fp_filter = (
            re.compile(_rule["false_positive_filter"], re.IGNORECASE)
            if "false_positive_filter" in _rule
            else None
        )
        _ctx_filter = (
            re.compile(_rule["context_filter"], re.IGNORECASE)
            if "context_filter" in _rule
            else None
        )
        _compiled_rules.append((_pat, _fp_filter, _ctx_filter, _rule))
    _COMPILED_SAST[_lang] = _compiled_rules


def _detect_language(path: Path) -> str | None:
    ext = path.suffix.lower()
    for lang, exts in _LANG_EXTENSIONS.items():
        if ext in exts:
            return lang
    return None


def scan_sast(project_root: str, language: str = "auto") -> list[dict]:
    """Run SAST rules over project source files.

    Args:
        project_root: Absolute path to the project root.
        language: "auto" (detect by extension) or one of python/javascript/go/java.

    Returns list of finding dicts; see module docstring for schema.
    """
    root = Path(project_root).resolve()
    findings: list[dict] = []

    for filepath in _iter_text_files(root):
        if language == "auto":
            lang = _detect_language(filepath)
        else:
            lang = language.lower()
            if lang not in _COMPILED_SAST:
                break  # unknown language — nothing to check

        if not lang or lang not in _COMPILED_SAST:
            continue

        rel_path = _rel(filepath, root)
        is_test_file = any(
            s in rel_path.lower()
            for s in (".test.", ".spec.", "test/", "tests/", "_test.", "_spec.")
        )

        lines = _read_lines(filepath)
        for lineno, line in enumerate(lines, start=1):
            for compiled_pat, fp_filter, ctx_filter, rule in _COMPILED_SAST[lang]:
                # PY012 (assert) — skip test files
                if rule["id"] == "PY012" and is_test_file:
                    continue

                if not compiled_pat.search(line):
                    continue

                # false-positive filter: if the filter matches, skip
                if fp_filter and fp_filter.search(line):
                    continue

                # context filter: rule only fires if surrounding context matches
                if ctx_filter:
                    window_start = max(0, lineno - 4)
                    window_end = min(len(lines), lineno + 3)
                    context_block = "\n".join(lines[window_start:window_end])
                    if not ctx_filter.search(context_block):
                        continue

                findings.append({
                    "file": rel_path,
                    "line": lineno,
                    "rule": rule["id"],
                    "rule_id": rule["id"],
                    "title": rule["title"],
                    "severity": rule["severity"],
                    "context": line.strip(),
                    "cwe": rule["cwe"],
                    "fix": rule["fix"],
                })

    return findings


# ---------------------------------------------------------------------------
# 5A-2b  Log Secret Leakage Scanner
# Real incident: Cloudflare parser bug leaked HTTP cookies, auth tokens, and POST
# bodies from memory into HTTP responses — some cached by search engines.
# Static equivalent: API keys / tokens interpolated directly into log calls.
# Rule IDs: LOG-001, LOG-002, LOG-003
# ---------------------------------------------------------------------------

# Variable names that strongly suggest a secret value
_SECRET_VAR_NAMES = re.compile(
    r"(?i)\b(?:api_?key|secret|token|password|passwd|auth|credential|"
    r"private_?key|access_?key|signing_?key|client_?secret|bearer|"
    r"api_?secret|stripe_?key|twilio_?key|sendgrid_?key|aws_?secret)\b"
)

# Python log calls: logging.info/debug/warning/error/critical and logger.* variants
# Pattern: log call that contains an f-string or % format with a secret var
_PY_LOG_CALL_RE = re.compile(
    r"(?:logging|logger|log)\s*\.\s*(?:debug|info|warning|error|critical|exception)\s*\("
)
_PY_FSTRING_RE  = re.compile(r'f["\'].*\{([^}]+)\}')
_PY_PERCENT_RE  = re.compile(r'%\s*(?:\(([^)]+)\)|[a-zA-Z_][a-zA-Z0-9_]*)')

# JavaScript / TypeScript: console.log/debug/info/warn/error
_JS_LOG_CALL_RE = re.compile(
    r"console\s*\.\s*(?:log|debug|info|warn|error)\s*\("
)
_JS_TEMPLATE_RE = re.compile(r'`[^`]*\$\{([^}]+)\}')

# Go: log.Printf / log.Println / logrus.WithField / zap.String etc
_GO_LOG_CALL_RE = re.compile(
    r'(?:log|logger|logrus|zap|sugar|slog)\s*\.\s*'
    r'(?:Printf|Println|Print|Infof?|Debugf?|Warnf?|Errorf?|Fatalf?|WithField)\s*\('
)
_GO_FMT_VERB_RE = re.compile(r'%[sdvq]')

# Java / Kotlin: log.info/debug/warn/error, SLF4J, Logback
_JAVA_LOG_CALL_RE = re.compile(
    r'(?:log|logger|LOG)\s*\.\s*(?:debug|info|warn|error|trace)\s*\('
)

_LOG_SCAN_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".kt"}


def scan_log_secret_leakage(project_root: str) -> list[dict]:
    """Detect secret variable values interpolated into log statements.

    This catches the pattern that the Cloudflare incident class represents:
    sensitive values (tokens, API keys, passwords) passed directly to a log
    call via f-strings, template literals, or format verbs — shipping secrets
    to stdout, Datadog, Splunk, or any log aggregator.

    Rule IDs:
        LOG-001  Python log call with f-string containing secret-named variable
        LOG-002  JS/TS console.log with template literal containing secret-named var
        LOG-003  Go log call with format verb and secret-named argument
        LOG-004  Java/Kotlin log call with secret-named variable in message

    Returns list of finding dicts compatible with scan_sast() output.
    """
    root = Path(project_root).resolve()
    findings: list[dict] = []

    for filepath in _iter_text_files(root):
        ext = filepath.suffix.lower()
        if ext not in _LOG_SCAN_EXTS:
            continue

        rel_path = _rel(filepath, root)

        # Skip test files — they often log fake keys intentionally
        rel_lower = rel_path.lower()
        if any(s in rel_lower for s in (".test.", ".spec.", "test/", "tests/", "_test.")):
            continue

        lines = _read_lines(filepath)

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()

            # ── LOG-001: Python ─────────────────────────────────────────────
            if ext == ".py" and _PY_LOG_CALL_RE.search(line):
                # Check for f-string with secret var name
                fm = _PY_FSTRING_RE.search(line)
                if fm and _SECRET_VAR_NAMES.search(fm.group(1)):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "rule": "LOG-001",
                        "rule_id": "LOG-001",
                        "title": "Secret variable interpolated into Python log call",
                        "severity": "high",
                        "context": stripped[:120],
                        "cwe": "CWE-532",
                        "fix": (
                            "Never interpolate secret values into log messages. "
                            "Log the operation name and a masked/truncated identifier "
                            "instead: logging.info('Calling Stripe', key_prefix=key[:4])"
                        ),
                    })
                    continue
                # Check % formatting with secret var
                pm = _PY_PERCENT_RE.search(line)
                if pm and _SECRET_VAR_NAMES.search(pm.group(1) or ""):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "rule": "LOG-001",
                        "rule_id": "LOG-001",
                        "title": "Secret variable interpolated into Python log call (% format)",
                        "severity": "high",
                        "context": stripped[:120],
                        "cwe": "CWE-532",
                        "fix": (
                            "Use a structured logger with a redact list, or mask the "
                            "value before logging: logging.debug('token=%s', token[:4] + '***')"
                        ),
                    })

            # ── LOG-002: JavaScript / TypeScript ────────────────────────────
            elif ext in {".js", ".ts", ".jsx", ".tsx"} and _JS_LOG_CALL_RE.search(line):
                tm = _JS_TEMPLATE_RE.search(line)
                if tm and _SECRET_VAR_NAMES.search(tm.group(1)):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "rule": "LOG-002",
                        "rule_id": "LOG-002",
                        "title": "Secret variable in JS/TS console.log template literal",
                        "severity": "high",
                        "context": stripped[:120],
                        "cwe": "CWE-532",
                        "fix": (
                            "Do not embed secret values in template literals passed to "
                            "console.log. Log the variable name and a masked prefix only."
                        ),
                    })

            # ── LOG-003: Go ──────────────────────────────────────────────────
            elif ext == ".go" and _GO_LOG_CALL_RE.search(line):
                if _GO_FMT_VERB_RE.search(line) and _SECRET_VAR_NAMES.search(line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "rule": "LOG-003",
                        "rule_id": "LOG-003",
                        "title": "Secret argument passed to Go log format call",
                        "severity": "high",
                        "context": stripped[:120],
                        "cwe": "CWE-532",
                        "fix": (
                            "Replace the secret argument with a masked version: "
                            "log.Printf(\"token=%s...\", token[:4])"
                        ),
                    })

            # ── LOG-004: Java / Kotlin ───────────────────────────────────────
            elif ext in {".java", ".kt"} and _JAVA_LOG_CALL_RE.search(line):
                if _SECRET_VAR_NAMES.search(line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "rule": "LOG-004",
                        "rule_id": "LOG-004",
                        "title": "Secret variable referenced in Java/Kotlin log call",
                        "severity": "high",
                        "context": stripped[:120],
                        "cwe": "CWE-532",
                        "fix": (
                            "Use a custom converter or masking utility. With Logback, "
                            "register a MessageConverter that redacts fields matching "
                            "secret name patterns."
                        ),
                    })

    return findings


# ---------------------------------------------------------------------------
# 5A-3  IaC Misconfiguration Scanner
# ---------------------------------------------------------------------------

_IAC_RULES: list[dict] = [
    {
        "id": "TF001",
        "title": "S3 bucket with public ACL",
        "pattern": r'acl\s*=\s*"public-read(?:-write)?"',
        "file_glob": r"\.tf$",
        "severity": "high",
        "fix": 'Set acl = "private" or use bucket policies with explicit grants.',
    },
    {
        "id": "TF002",
        "title": "Security group allows 0.0.0.0/0 ingress on port 22 (SSH)",
        "pattern": r'(?s)ingress[^}]*cidr_blocks[^}]*0\.0\.0\.0/0',
        "file_glob": r"\.tf$",
        "severity": "critical",
        "fix": "Restrict SSH to known IP ranges.",
    },
    {
        "id": "TF003",
        "title": "Security group allows 0.0.0.0/0 on all ports",
        "pattern": r'(?s)(?:from_port|to_port)\s*=\s*0[^}]*cidr_blocks[^}]*0\.0\.0\.0/0',
        "file_glob": r"\.tf$",
        "severity": "critical",
        "fix": "Restrict to specific ports and CIDR ranges.",
    },
    {
        "id": "TF004",
        "title": "RDS instance publicly accessible",
        "pattern": r'publicly_accessible\s*=\s*true',
        "file_glob": r"\.tf$",
        "severity": "high",
        "fix": "Set publicly_accessible = false; access via bastion or VPC.",
    },
    {
        "id": "TF005",
        "title": "S3 bucket missing versioning",
        "pattern": r'resource\s+"aws_s3_bucket"\s+"[^"]+"\s*\{(?:(?!versioning)[^}])*\}',
        "file_glob": r"\.tf$",
        "severity": "medium",
        "fix": "Add a versioning { enabled = true } block to the S3 bucket resource.",
    },
    {
        "id": "TF006",
        "title": "EKS cluster with public endpoint access",
        "pattern": r'endpoint_public_access\s*=\s*true',
        "file_glob": r"\.tf$",
        "severity": "medium",
        "fix": "Set endpoint_public_access = false and use private networking.",
    },
    {
        "id": "TF007",
        "title": "Lambda function with wide IAM permissions (*)",
        "pattern": r'"Action"\s*:\s*"\*"',
        "file_glob": r"\.(tf|json)$",
        "severity": "high",
        "fix": "Apply least-privilege — specify exact IAM actions needed.",
    },
    {
        "id": "TF008",
        "title": "KMS key with no key rotation",
        "pattern": r'resource\s+"aws_kms_key"(?:(?!enable_key_rotation)[^}])*\}',
        "file_glob": r"\.tf$",
        "severity": "medium",
        "fix": "Add enable_key_rotation = true to the aws_kms_key resource.",
    },
    {
        "id": "K8S001",
        "title": "Container running as root (no runAsNonRoot)",
        "pattern": r'(?s)containers:[^-]*-[^-]*image:[^-]*(?!runAsNonRoot)',
        "file_glob": r"\.(yaml|yml)$",
        "severity": "medium",
        "fix": "Add securityContext.runAsNonRoot: true to container spec.",
    },
    {
        "id": "K8S002",
        "title": "Privileged container",
        "pattern": r'privileged\s*:\s*true',
        "file_glob": r"\.(yaml|yml)$",
        "severity": "critical",
        "fix": "Remove privileged: true; use specific capabilities instead.",
    },
    {
        "id": "K8S003",
        "title": "Container missing resource limits",
        "pattern": r'(?s)containers:\s*-[^-]*image:[^-]*(?!limits:)',
        "file_glob": r"\.(yaml|yml)$",
        "severity": "low",
        "fix": "Add resources.limits.cpu and resources.limits.memory to prevent noisy-neighbor issues.",
    },
    {
        "id": "DOCKER001",
        "title": "Dockerfile runs as root (no USER directive)",
        "pattern": r'^FROM\s+\S+',
        "file_glob": r"Dockerfile",
        "severity": "medium",
        "fix": "Add a USER instruction to run as a non-root user.",
        "negative_pattern": r'^USER\s+\S+',  # flag only if USER is absent
    },
    {
        "id": "DOCKER002",
        "title": "Dockerfile uses :latest tag",
        "pattern": r'FROM\s+\S+:latest\b',
        "file_glob": r"Dockerfile",
        "severity": "low",
        "fix": "Pin to a specific image version for reproducible builds.",
    },
]

_IAC_SKIP_PATHS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build"}


def scan_iac_misconfigs(project_root: str) -> list[dict]:
    """Scan Terraform, Kubernetes YAML, and Dockerfile for security misconfigs.

    Returns a list of finding dicts with keys:
        rule_id, title, severity, fix, file, line, snippet
    """
    root = Path(project_root).resolve()
    findings: list[dict] = []

    # Pre-compile all rule patterns
    compiled_rules: list[tuple[re.Pattern, re.Pattern | None, dict]] = []
    for rule in _IAC_RULES:
        pat = re.compile(rule["pattern"], re.MULTILINE)
        neg_pat = (
            re.compile(rule["negative_pattern"], re.MULTILINE)
            if "negative_pattern" in rule
            else None
        )
        compiled_rules.append((pat, neg_pat, rule))

    def _file_matches_glob(path: Path, glob_pattern: str) -> bool:
        """Return True if the file path matches the rule's file_glob."""
        # Dockerfile rules: match by exact filename
        if not glob_pattern.startswith(r"\."):
            return re.search(glob_pattern, path.name) is not None
        return bool(re.search(glob_pattern, path.name))

    for filepath in root.rglob("*"):
        if not filepath.is_file():
            continue
        # Skip build/vendor dirs
        if any(p in _IAC_SKIP_PATHS for p in filepath.parts):
            continue
        if filepath.stat().st_size > 2_097_152:  # 2 MB cap
            continue

        for compiled_pat, neg_pat, rule in compiled_rules:
            if not _file_matches_glob(filepath, rule["file_glob"]):
                continue

            content = ""
            for enc in ("utf-8", "latin-1"):
                try:
                    content = filepath.read_text(encoding=enc)
                    break
                except (UnicodeDecodeError, OSError):
                    pass
            if not content:
                continue

            # For rules with a negative_pattern, flag only if the negative
            # pattern is ABSENT from the whole file.
            if neg_pat is not None:
                if compiled_pat.search(content) and not neg_pat.search(content):
                    # Report at line 1 (file-level finding)
                    findings.append({
                        "rule_id": rule["id"],
                        "title": rule["title"],
                        "severity": rule["severity"],
                        "fix": rule["fix"],
                        "file": _rel(filepath, root),
                        "line": 1,
                        "snippet": content.splitlines()[0].strip() if content else "",
                    })
                continue

            # Standard: report each match with its line number
            lines = content.splitlines()
            # Use finditer for line-level precision; for multi-line patterns
            # find the line number from match start offset.
            for m in compiled_pat.finditer(content):
                # Compute line number from offset
                line_no = content[: m.start()].count("\n") + 1
                snippet = lines[line_no - 1].strip() if line_no <= len(lines) else ""
                findings.append({
                    "rule_id": rule["id"],
                    "title": rule["title"],
                    "severity": rule["severity"],
                    "fix": rule["fix"],
                    "file": _rel(filepath, root),
                    "line": line_no,
                    "snippet": snippet,
                })
                # For multi-line rules (re.DOTALL via (?s)), only report once
                # per file to avoid flooding.
                if "(?s)" in rule["pattern"]:
                    break

    return findings


# ---------------------------------------------------------------------------
# 5A-4  Vulnerability Scanner (OSV.dev)
# ---------------------------------------------------------------------------

_LOCK_FILE_ECOSYSTEM: dict[str, str] = {
    "package-lock.json": "npm",
    "yarn.lock": "npm",
    "pnpm-lock.yaml": "npm",
    "requirements.txt": "PyPI",
    "Pipfile.lock": "PyPI",
    "poetry.lock": "PyPI",
    "Cargo.lock": "crates.io",
    "go.sum": "Go",
    "Gemfile.lock": "RubyGems",
}

_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_OSV_BATCH_SIZE = 1000


def _parse_package_lock(path: Path) -> list[tuple[str, str]]:
    """Parse npm package-lock.json → list of (name, version)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    pkgs: list[tuple[str, str]] = []

    def _extract(node: dict):
        for key, val in node.items():
            if isinstance(val, dict):
                name = val.get("name") or key.lstrip("/").split("node_modules/")[-1]
                version = val.get("version", "")
                if version:
                    pkgs.append((name, version))
                nested = val.get("packages") or val.get("dependencies") or {}
                _extract(nested)

    packages = data.get("packages") or data.get("dependencies") or {}
    _extract(packages)
    return pkgs


def _parse_yarn_lock(path: Path) -> list[tuple[str, str]]:
    """Parse yarn.lock → list of (name, version)."""
    lines = _read_lines(path)
    pkgs: list[tuple[str, str]] = []
    current_name: str | None = None
    for line in lines:
        # Entry header: "\"lodash@^4.17.15\":"  or  "lodash@^4.17.15:"
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            current_name = None
            continue
        if stripped.endswith(":") and not stripped.startswith(" "):
            # Extract package name (first spec before @version)
            raw = stripped.rstrip(":")
            # Handle quoted entries
            raw = raw.strip('"')
            # Take first comma-separated spec
            first_spec = raw.split(",")[0].strip().strip('"')
            # Name is everything before the last @
            at_idx = first_spec.rfind("@")
            if at_idx > 0:
                current_name = first_spec[:at_idx]
            else:
                current_name = first_spec
        elif stripped.startswith("version") and current_name:
            m = re.search(r'version\s+"?([^"]+)"?', stripped)
            if m:
                pkgs.append((current_name, m.group(1)))
                current_name = None
    return pkgs


def _parse_pnpm_lock(path: Path) -> list[tuple[str, str]]:
    """Parse pnpm-lock.yaml → (name, version) pairs (best-effort, no yaml dep)."""
    lines = _read_lines(path)
    pkgs: list[tuple[str, str]] = []
    in_packages = False
    for line in lines:
        if line.startswith("packages:"):
            in_packages = True
            continue
        if in_packages:
            m = re.match(r"^\s+/([^@]+)@([^:\s]+)", line)
            if m:
                pkgs.append((m.group(1), m.group(2)))
    return pkgs


def _parse_requirements_txt(path: Path) -> list[tuple[str, str]]:
    """Parse requirements.txt → (name, version) for pinned entries."""
    pkgs: list[tuple[str, str]] = []
    for line in _read_lines(path):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;#]+)", line)
        if m:
            pkgs.append((m.group(1), m.group(2)))
    return pkgs


def _parse_pipfile_lock(path: Path) -> list[tuple[str, str]]:
    """Parse Pipfile.lock → (name, version)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    pkgs: list[tuple[str, str]] = []
    for section in ("default", "develop"):
        for name, meta in data.get(section, {}).items():
            version = meta.get("version", "").lstrip("=")
            if version:
                pkgs.append((name, version))
    return pkgs


def _parse_poetry_lock(path: Path) -> list[tuple[str, str]]:
    """Parse poetry.lock → (name, version) pairs."""
    lines = _read_lines(path)
    pkgs: list[tuple[str, str]] = []
    current_name: str | None = None
    for line in lines:
        stripped = line.strip()
        m_name = re.match(r'^name\s*=\s*"([^"]+)"', stripped)
        if m_name:
            current_name = m_name.group(1)
        m_ver = re.match(r'^version\s*=\s*"([^"]+)"', stripped)
        if m_ver and current_name:
            pkgs.append((current_name, m_ver.group(1)))
            current_name = None
    return pkgs


def _parse_cargo_lock(path: Path) -> list[tuple[str, str]]:
    """Parse Cargo.lock → (name, version) pairs."""
    lines = _read_lines(path)
    pkgs: list[tuple[str, str]] = []
    current_name: str | None = None
    in_package = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[[package]]":
            in_package = True
            current_name = None
            continue
        if in_package:
            m_name = re.match(r'^name\s*=\s*"([^"]+)"', stripped)
            if m_name:
                current_name = m_name.group(1)
            m_ver = re.match(r'^version\s*=\s*"([^"]+)"', stripped)
            if m_ver and current_name:
                pkgs.append((current_name, m_ver.group(1)))
    return pkgs


def _parse_go_sum(path: Path) -> list[tuple[str, str]]:
    """Parse go.sum → (module, version) pairs (deduplicated)."""
    seen: set[tuple[str, str]] = set()
    pkgs: list[tuple[str, str]] = []
    for line in _read_lines(path):
        parts = line.split()
        if len(parts) < 2:
            continue
        module = parts[0]
        version = parts[1].split("/")[0]  # strip /go.mod suffix
        version = version.lstrip("v")
        key = (module, version)
        if key not in seen:
            seen.add(key)
            pkgs.append((module, version))
    return pkgs


def _parse_gemfile_lock(path: Path) -> list[tuple[str, str]]:
    """Parse Gemfile.lock → (name, version) pairs from GEM/specs section."""
    lines = _read_lines(path)
    pkgs: list[tuple[str, str]] = []
    in_specs = False
    for line in lines:
        stripped = line.strip()
        if stripped == "specs:":
            in_specs = True
            continue
        if in_specs:
            if stripped == "" or (not line.startswith("    ") and not line.startswith("  ")):
                in_specs = False
                continue
            # "    gemname (version)"
            m = re.match(r"^\s{4}([A-Za-z0-9_\-\.]+)\s+\(([^)]+)\)", line)
            if m:
                pkgs.append((m.group(1), m.group(2)))
    return pkgs


_LOCK_PARSERS = {
    "package-lock.json": _parse_package_lock,
    "yarn.lock": _parse_yarn_lock,
    "pnpm-lock.yaml": _parse_pnpm_lock,
    "requirements.txt": _parse_requirements_txt,
    "Pipfile.lock": _parse_pipfile_lock,
    "poetry.lock": _parse_poetry_lock,
    "Cargo.lock": _parse_cargo_lock,
    "go.sum": _parse_go_sum,
    "Gemfile.lock": _parse_gemfile_lock,
}


def _osv_batch_query(queries: list[dict], timeout: int = 30) -> list[dict]:
    """POST to OSV batch API. Returns list of result objects (same length as queries)."""
    payload = json.dumps({"queries": queries}).encode()
    req = urllib.request.Request(
        _OSV_BATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("results", [])
    except Exception:
        return []


def _severity_from_osv(vuln: dict) -> str:
    """Extract severity string from an OSV vulnerability record."""
    for severity_item in vuln.get("severity", []):
        score = severity_item.get("score", "")
        # CVSS score string like "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        m = re.search(r"(\d+\.\d+)$", score)
        if m:
            cvss = float(m.group(1))
            if cvss >= 9.0:
                return "critical"
            if cvss >= 7.0:
                return "high"
            if cvss >= 4.0:
                return "medium"
            return "low"
    # Fallback: look in database_specific or ecosystem_specific
    db = vuln.get("database_specific", {})
    sev = db.get("severity", "").lower()
    if sev in ("critical", "high", "medium", "low"):
        return sev
    return "unknown"


def _fix_version_from_osv(vuln: dict) -> str:
    """Best-effort extraction of a fix version from an OSV record."""
    for affected in vuln.get("affected", []):
        for rng in affected.get("ranges", []):
            for event in rng.get("events", []):
                fixed = event.get("fixed")
                if fixed:
                    return fixed
    return ""


def scan_vulnerabilities(project_root: str) -> dict:
    """Parse lock files and query OSV.dev for known vulnerabilities.

    Returns a dict; see module docstring for schema.
    """
    root = Path(project_root).resolve()

    # Collect all packages from every lock file found
    all_queries: list[dict] = []
    pkg_index: list[dict] = []  # parallel to all_queries

    for lock_name, parser in _LOCK_PARSERS.items():
        ecosystem = _LOCK_FILE_ECOSYSTEM[lock_name]
        # Search for this lock file anywhere in the tree (but not in skip dirs)
        for lock_path in root.rglob(lock_name):
            if any(_should_skip_dir(p) for p in lock_path.parts):
                continue
            for name, version in parser(lock_path):
                if not name or not version:
                    continue
                all_queries.append({
                    "package": {"name": name, "ecosystem": ecosystem},
                    "version": version,
                })
                pkg_index.append({
                    "pkg": name,
                    "version": version,
                    "ecosystem": ecosystem,
                })

    if not all_queries:
        return {
            "ok": True,
            "packages_checked": 0,
            "vulnerabilities": [],
            "error": None,
            "data_source": "osv.dev",
        }

    # Deduplicate
    seen_keys: set[tuple[str, str, str]] = set()
    deduped_queries: list[dict] = []
    deduped_index: list[dict] = []
    for q, p in zip(all_queries, pkg_index):
        key = (p["pkg"], p["version"], p["ecosystem"])
        if key not in seen_keys:
            seen_keys.add(key)
            deduped_queries.append(q)
            deduped_index.append(p)

    # Batch query OSV in groups of 1000
    osv_results: list[dict] = []
    error_msg: str | None = None
    try:
        for i in range(0, len(deduped_queries), _OSV_BATCH_SIZE):
            batch = deduped_queries[i : i + _OSV_BATCH_SIZE]
            results = _osv_batch_query(batch)
            osv_results.extend(results)
            # Pad with empty if OSV returned fewer results
            while len(osv_results) < i + len(batch):
                osv_results.append({})
    except Exception as exc:
        error_msg = str(exc)

    vulnerabilities: list[dict] = []
    for idx, result in enumerate(osv_results):
        if idx >= len(deduped_index):
            break
        pkg_info = deduped_index[idx]
        for vuln in result.get("vulns", []):
            vulnerabilities.append({
                "pkg": pkg_info["pkg"],
                "version": pkg_info["version"],
                "vuln_id": vuln.get("id", ""),
                "severity": _severity_from_osv(vuln),
                "summary": vuln.get("summary", ""),
                "fix_version": _fix_version_from_osv(vuln),
            })

    return {
        "ok": True,
        "packages_checked": len(deduped_queries),
        "vulnerabilities": vulnerabilities,
        "error": error_msg,
        "data_source": "osv.dev",
    }


# ---------------------------------------------------------------------------
# 5A-4  License Audit
# ---------------------------------------------------------------------------

_COPYLEFT_LICENSES = {
    "GPL-2.0", "GPL-3.0", "LGPL-2.0", "LGPL-2.1", "LGPL-3.0",
    "AGPL-3.0", "MPL-2.0", "EUPL-1.2",
    # Also accept common variants
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "AGPL-3.0-only", "AGPL-3.0-or-later",
}
_PERMISSIVE_LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
    "ISC", "CC0-1.0", "Unlicense", "0BSD", "WTFPL",
    "BlueOak-1.0.0", "Artistic-2.0",
}


def _normalize_license(lic: str | None) -> str:
    if not lic:
        return "UNKNOWN"
    lic = lic.strip().strip('"').strip("'")
    # SPDX OR expressions → take the most permissive (first known)
    for part in re.split(r"\s+(?:OR|AND)\s+", lic, flags=re.IGNORECASE):
        part = part.strip().strip("()")
        if part in _PERMISSIVE_LICENSES or part in _COPYLEFT_LICENSES:
            return part
    return lic


def _classify_license(lic: str) -> str:
    if lic in _COPYLEFT_LICENSES:
        return "copyleft"
    if lic in _PERMISSIVE_LICENSES:
        return "permissive"
    return "unknown"


def scan_licenses(project_root: str) -> dict:
    """Audit licenses of project dependencies.

    Returns a dict; see module docstring for schema.
    """
    root = Path(project_root).resolve()

    licenses_found: dict[str, list[str]] = {}
    copyleft_packages: list[dict] = []
    permissive_packages: list[dict] = []
    unknown_packages: list[dict] = []

    def _record(name: str, version: str, lic_raw: str | None):
        lic = _normalize_license(lic_raw)
        licenses_found.setdefault(lic, [])
        if name not in licenses_found[lic]:
            licenses_found[lic].append(name)
        cat = _classify_license(lic)
        entry = {"package": name, "version": version, "license": lic}
        if cat == "copyleft":
            entry["risk"] = "copyleft — may require source disclosure"
            copyleft_packages.append(entry)
        elif cat == "permissive":
            permissive_packages.append(entry)
        else:
            unknown_packages.append(entry)

    # --- package.json ---
    for pkg_json in root.rglob("package.json"):
        if any(_should_skip_dir(p) for p in pkg_json.parts):
            continue
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        # top-level license
        top_lic = data.get("license")
        top_name = data.get("name", pkg_json.parent.name)
        top_version = data.get("version", "")
        if top_lic:
            _record(top_name, top_version, top_lic)

    # --- package-lock.json ---
    for lock_path in root.rglob("package-lock.json"):
        if any(_should_skip_dir(p) for p in lock_path.parts):
            continue
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        def _scan_npm_node(node: dict):
            for key, val in node.items():
                if not isinstance(val, dict):
                    continue
                name = val.get("name") or key.split("node_modules/")[-1].lstrip("/")
                version = val.get("version", "")
                lic = val.get("license")
                if name and version:
                    _record(name, version, lic)
                nested = val.get("packages") or val.get("dependencies") or {}
                _scan_npm_node(nested)

        packages = data.get("packages") or data.get("dependencies") or {}
        _scan_npm_node(packages)

    # --- pyproject.toml ---
    for py_proj in root.rglob("pyproject.toml"):
        if any(_should_skip_dir(p) for p in py_proj.parts):
            continue
        content = _read_lines(py_proj)
        in_project = False
        for line in content:
            if line.strip() == "[project]":
                in_project = True
                continue
            if in_project and line.startswith("[") and line.strip() != "[project]":
                in_project = False
            if in_project:
                m = re.match(r'^\s*license\s*=\s*["\']?([^"\'{\n]+)["\']?', line)
                if m:
                    lic = m.group(1).strip().rstrip(",")
                    # Also try to get name
                    name_m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', "\n".join(content), re.MULTILINE)
                    ver_m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', "\n".join(content), re.MULTILINE)
                    pkg_name = name_m.group(1) if name_m else py_proj.parent.name
                    pkg_ver = ver_m.group(1) if ver_m else ""
                    _record(pkg_name, pkg_ver, lic)

    # --- setup.py (best-effort regex) ---
    for setup_py in root.rglob("setup.py"):
        if any(_should_skip_dir(p) for p in setup_py.parts):
            continue
        content = setup_py.read_text(encoding="utf-8", errors="ignore")
        m_lic = re.search(r'license\s*=\s*["\']([^"\']+)["\']', content)
        m_name = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
        m_ver = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
        if m_lic:
            _record(
                m_name.group(1) if m_name else setup_py.parent.name,
                m_ver.group(1) if m_ver else "",
                m_lic.group(1),
            )

    # --- Cargo.toml ---
    for cargo_toml in root.rglob("Cargo.toml"):
        if any(_should_skip_dir(p) for p in cargo_toml.parts):
            continue
        content = "\n".join(_read_lines(cargo_toml))
        m_lic = re.search(r'^license\s*=\s*"([^"]+)"', content, re.MULTILINE)
        m_name = re.search(r'^name\s*=\s*"([^"]+)"', content, re.MULTILINE)
        m_ver = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if m_lic:
            _record(
                m_name.group(1) if m_name else cargo_toml.parent.name,
                m_ver.group(1) if m_ver else "",
                m_lic.group(1),
            )

    # --- go.mod (no license field — just list module name) ---
    for go_mod in root.rglob("go.mod"):
        if any(_should_skip_dir(p) for p in go_mod.parts):
            continue
        content = "\n".join(_read_lines(go_mod))
        m = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
        if m:
            _record(m.group(1), "", None)

    return {
        "ok": True,
        "licenses_found": licenses_found,
        "copyleft_packages": copyleft_packages,
        "permissive_packages": permissive_packages,
        "unknown_packages": unknown_packages,
    }


# ---------------------------------------------------------------------------
# 5A-5  Environment Parity
# ---------------------------------------------------------------------------

def _parse_docker_compose(path: Path) -> dict:
    """Best-effort parse of docker-compose.yml → {service: {env_vars, ports, image}}."""
    lines = _read_lines(path)
    services: dict[str, dict] = {}
    current_service: str | None = None
    in_services = False
    in_env = False
    in_ports = False
    indent_service = 0

    for line in lines:
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip())
        content = stripped.strip()

        if content == "services:":
            in_services = True
            in_env = False
            in_ports = False
            continue

        if in_services:
            # Service name: 2-space indent, no leading "-"
            if indent == 2 and not content.startswith("-") and content.endswith(":"):
                current_service = content.rstrip(":")
                services[current_service] = {"env_vars": {}, "ports": [], "image": ""}
                indent_service = indent
                in_env = False
                in_ports = False
                continue

            if current_service:
                if indent == 4:
                    in_env = content.startswith("environment:")
                    in_ports = content.startswith("ports:")
                    if content.startswith("image:"):
                        services[current_service]["image"] = content.split(":", 1)[1].strip()
                    continue

                if in_env and indent >= 6:
                    # "- KEY=value" or "KEY: value"
                    if content.startswith("- "):
                        kv = content[2:]
                        if "=" in kv:
                            k, _, v = kv.partition("=")
                            services[current_service]["env_vars"][k.strip()] = v.strip()
                        else:
                            services[current_service]["env_vars"][kv.strip()] = ""
                    elif ":" in content:
                        k, _, v = content.partition(":")
                        services[current_service]["env_vars"][k.strip()] = v.strip()
                    continue

                if in_ports and indent >= 6:
                    if content.startswith("- "):
                        services[current_service]["ports"].append(content[2:].strip().strip('"'))
                    continue

    return services


def _find_k8s_files(root: Path) -> list[Path]:
    """Find Kubernetes YAML manifests (Deployment, StatefulSet) under root."""
    results = []
    for path in root.rglob("*.yaml"):
        if any(_should_skip_dir(p) for p in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^kind:\s*(Deployment|StatefulSet|DaemonSet)", content, re.MULTILINE):
            results.append(path)
    for path in root.rglob("*.yml"):
        if any(_should_skip_dir(p) for p in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^kind:\s*(Deployment|StatefulSet|DaemonSet)", content, re.MULTILINE):
            results.append(path)
    return results


def _parse_k8s_manifest(path: Path) -> dict:
    """Best-effort parse a K8s Deployment YAML → {name: {env_vars, ports, image}}."""
    lines = _read_lines(path)
    deployments: dict[str, dict] = {}

    # Extract name from metadata.name
    content = "\n".join(lines)
    name_m = re.search(r"^\s{2}name:\s*(\S+)", content, re.MULTILINE)
    deploy_name = name_m.group(1) if name_m else path.stem

    env_vars: dict[str, str] = {}
    ports: list[str] = []
    image = ""

    in_env = False
    current_env_name: str | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())

        if "image:" in stripped and indent >= 8:
            image = stripped.split("image:", 1)[1].strip()

        if stripped == "env:":
            in_env = True
            continue

        if in_env:
            if stripped.startswith("- name:"):
                current_env_name = stripped.split("- name:", 1)[1].strip()
            elif stripped.startswith("value:") and current_env_name:
                env_vars[current_env_name] = stripped.split("value:", 1)[1].strip()
                current_env_name = None
            elif stripped.startswith("valueFrom:") and current_env_name:
                env_vars[current_env_name] = "(valueFrom)"
                current_env_name = None
            elif stripped.startswith("- ") and not stripped.startswith("- name:"):
                in_env = False

        if "containerPort:" in stripped:
            m = re.search(r"containerPort:\s*(\d+)", stripped)
            if m:
                ports.append(m.group(1))

    deployments[deploy_name] = {"env_vars": env_vars, "ports": ports, "image": image}
    return deployments


def scan_env_parity(project_root: str) -> dict:
    """Compare Docker Compose vs Kubernetes env vars for parity issues.

    Returns a dict; see module docstring for schema.
    """
    root = Path(project_root).resolve()

    compose_services: dict[str, dict] = {}
    k8s_deployments: dict[str, dict] = {}

    # Find docker-compose files
    for compose_file in root.rglob("docker-compose*.yml"):
        if any(_should_skip_dir(p) for p in compose_file.parts):
            continue
        compose_services.update(_parse_docker_compose(compose_file))
    for compose_file in root.rglob("docker-compose*.yaml"):
        if any(_should_skip_dir(p) for p in compose_file.parts):
            continue
        compose_services.update(_parse_docker_compose(compose_file))

    # Find K8s manifests
    for k8s_file in _find_k8s_files(root):
        k8s_deployments.update(_parse_k8s_manifest(k8s_file))

    missing_in_k8s: list[dict] = []
    missing_in_compose: list[dict] = []
    image_mismatches: list[dict] = []

    # Cross-reference by service/deployment name (loose match)
    for svc_name, svc_data in compose_services.items():
        # Find matching K8s deployment (exact or partial name match)
        matched_k8s = k8s_deployments.get(svc_name)
        if not matched_k8s:
            for k8s_name in k8s_deployments:
                if svc_name in k8s_name or k8s_name in svc_name:
                    matched_k8s = k8s_deployments[k8s_name]
                    break
        if not matched_k8s:
            continue

        compose_envs = set(svc_data.get("env_vars", {}).keys())
        k8s_envs = set(matched_k8s.get("env_vars", {}).keys())

        for env_key in compose_envs - k8s_envs:
            missing_in_k8s.append({
                "service": svc_name,
                "env_var": env_key,
                "compose_value": svc_data["env_vars"].get(env_key, ""),
            })

        for env_key in k8s_envs - compose_envs:
            missing_in_compose.append({
                "deployment": matched_k8s,
                "env_var": env_key,
                "k8s_value": matched_k8s["env_vars"].get(env_key, ""),
            })

        # Image mismatch
        compose_image = svc_data.get("image", "")
        k8s_image = matched_k8s.get("image", "")
        if compose_image and k8s_image and compose_image != k8s_image:
            image_mismatches.append({
                "service": svc_name,
                "compose_image": compose_image,
                "k8s_image": k8s_image,
            })

    all_ok = not missing_in_k8s and not missing_in_compose and not image_mismatches

    return {
        "ok": all_ok,
        "compose_services": compose_services,
        "k8s_deployments": k8s_deployments,
        "missing_in_k8s": missing_in_k8s,
        "missing_in_compose": missing_in_compose,
        "image_mismatches": image_mismatches,
    }


# ---------------------------------------------------------------------------
# 5B  Reporting
# ---------------------------------------------------------------------------

def generate_sbom(project_root: str, format: str = "summary") -> dict:
    """Generate a Software Bill of Materials.

    Args:
        project_root: Absolute path to the project.
        format: "summary" or "cyclonedx"

    Returns a dict with component list or CycloneDX 1.4 BOM.
    """
    root = Path(project_root).resolve()
    components: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add_component(name: str, version: str, ecosystem: str, lic: str = ""):
        key = (name, version)
        if key in seen:
            return
        seen.add(key)
        components.append({
            "name": name,
            "version": version,
            "ecosystem": ecosystem,
            "license": lic or "UNKNOWN",
        })

    # Harvest from lock files
    for lock_name, parser in _LOCK_PARSERS.items():
        ecosystem = _LOCK_FILE_ECOSYSTEM[lock_name]
        for lock_path in root.rglob(lock_name):
            if any(_should_skip_dir(p) for p in lock_path.parts):
                continue
            for name, version in parser(lock_path):
                _add_component(name, version, ecosystem)

    # Try to enrich licenses from package-lock.json
    for lock_path in root.rglob("package-lock.json"):
        if any(_should_skip_dir(p) for p in lock_path.parts):
            continue
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))

            def _enrich(node: dict):
                for key, val in node.items():
                    if isinstance(val, dict):
                        name = val.get("name") or key.split("node_modules/")[-1].lstrip("/")
                        version = val.get("version", "")
                        lic = val.get("license", "")
                        if name and version and lic:
                            for c in components:
                                if c["name"] == name and c["version"] == version:
                                    c["license"] = lic
                        _enrich(val.get("packages") or val.get("dependencies") or {})

            _enrich(data.get("packages") or data.get("dependencies") or {})
        except Exception:
            pass

    if format == "summary":
        return {
            "ok": True,
            "format": "summary",
            "component_count": len(components),
            "components": components,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # CycloneDX 1.4 JSON BOM
    cdx_components = []
    for c in components:
        comp: dict[str, Any] = {
            "type": "library",
            "name": c["name"],
            "version": c["version"],
        }
        if c["license"] and c["license"] != "UNKNOWN":
            comp["licenses"] = [{"license": {"id": c["license"]}}]
        cdx_components.append(comp)

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "version": 1,
        "serialNumber": f"urn:uuid:{_uuid4_str()}",
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"vendor": "GrapeRoot Pro", "name": "security.py", "version": "1.0"}],
        },
        "components": cdx_components,
    }

    return {
        "ok": True,
        "format": "cyclonedx",
        "bom": bom,
        "component_count": len(cdx_components),
        "generated_at": bom["metadata"]["timestamp"],
    }


def _uuid4_str() -> str:
    """Generate a UUID4-like string without uuid module dependency."""
    import os as _os
    raw = _os.urandom(16)
    raw = bytearray(raw)
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    hex_str = raw.hex()
    return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"


def compute_debt_score(project_root: str, file_prefix: str = "") -> dict:
    """Compute a weighted security debt score (0-100, A-F grade).

    Deductions:
        Secrets:         -20/critical, -10/high
        SAST:            -5/critical,  -3/high, -1/medium
        Vulnerabilities: -15/critical, -8/high, -3/medium
        Dead exports:    -0.5 each (capped at -10)
        Copyleft:        -5 per copyleft package
        Start at 100, floor at 0.

    Args:
        project_root: Absolute path to the project.
        file_prefix: Optional path prefix to filter SAST/secret findings.
    """
    # Run all scanners
    secrets = scan_secrets(project_root)
    sast = scan_sast(project_root)
    vuln_report = scan_vulnerabilities(project_root)
    license_report = scan_licenses(project_root)

    if file_prefix:
        secrets = [f for f in secrets if f["file"].startswith(file_prefix)]
        sast = [f for f in sast if f["file"].startswith(file_prefix)]

    # Filter vulnerabilities and licenses (no path filter for these)
    vulns = vuln_report.get("vulnerabilities", [])
    copyleft_pkgs = license_report.get("copyleft_packages", [])

    score = 100.0
    breakdown: dict[str, Any] = {}

    # Secrets deductions
    secret_deduction = 0.0
    secret_counts: dict[str, int] = {}
    for finding in secrets:
        sev = finding.get("severity", "")
        secret_counts[sev] = secret_counts.get(sev, 0) + 1
        if sev == "critical":
            secret_deduction += 20
        elif sev == "high":
            secret_deduction += 10
        elif sev == "medium":
            secret_deduction += 5
    score -= secret_deduction
    breakdown["secrets"] = {
        "findings": len(secrets),
        "by_severity": secret_counts,
        "deduction": -secret_deduction,
    }

    # SAST deductions
    sast_deduction = 0.0
    sast_counts: dict[str, int] = {}
    for finding in sast:
        sev = finding.get("severity", "")
        sast_counts[sev] = sast_counts.get(sev, 0) + 1
        if sev == "critical":
            sast_deduction += 5
        elif sev == "high":
            sast_deduction += 3
        elif sev == "medium":
            sast_deduction += 1
    score -= sast_deduction
    breakdown["sast"] = {
        "findings": len(sast),
        "by_severity": sast_counts,
        "deduction": -sast_deduction,
    }

    # Vulnerability deductions
    vuln_deduction = 0.0
    vuln_counts: dict[str, int] = {}
    for v in vulns:
        sev = v.get("severity", "")
        vuln_counts[sev] = vuln_counts.get(sev, 0) + 1
        if sev == "critical":
            vuln_deduction += 15
        elif sev == "high":
            vuln_deduction += 8
        elif sev == "medium":
            vuln_deduction += 3
    score -= vuln_deduction
    breakdown["vulnerabilities"] = {
        "findings": len(vulns),
        "by_severity": vuln_counts,
        "deduction": -vuln_deduction,
    }

    # Copyleft deductions
    copyleft_deduction = len(copyleft_pkgs) * 5.0
    score -= copyleft_deduction
    breakdown["copyleft"] = {
        "packages": len(copyleft_pkgs),
        "deduction": -copyleft_deduction,
    }

    # Dead exports (optional — try to call graph if available)
    dead_export_deduction = 0.0
    try:
        from graph_builder_v6_2 import find_dead_exports  # type: ignore[import]
        dead = find_dead_exports(project_root) or []
        dead_export_deduction = min(len(dead) * 0.5, 10.0)
        score -= dead_export_deduction
        breakdown["dead_exports"] = {
            "count": len(dead),
            "deduction": -dead_export_deduction,
        }
    except Exception:
        breakdown["dead_exports"] = {"count": 0, "deduction": 0, "note": "not available"}

    score = max(0.0, score)
    score_int = round(score)

    if score_int >= 90:
        grade = "A"
    elif score_int >= 80:
        grade = "B"
    elif score_int >= 70:
        grade = "C"
    elif score_int >= 60:
        grade = "D"
    else:
        grade = "F"

    # Recommendations
    recommendations: list[str] = []
    if secret_counts.get("critical", 0) or secret_counts.get("high", 0):
        recommendations.append("Remove hardcoded secrets immediately; rotate any exposed credentials.")
    if sast_counts.get("critical", 0):
        recommendations.append("Fix critical SAST findings (SQL injection, command injection) before deployment.")
    if vuln_counts.get("critical", 0) or vuln_counts.get("high", 0):
        recommendations.append("Upgrade packages with critical/high CVEs; check OSV advisories.")
    if copyleft_pkgs:
        recommendations.append(
            f"Review {len(copyleft_pkgs)} copyleft package(s) for license compliance obligations."
        )
    if not recommendations:
        recommendations.append("Security posture looks good. Keep dependencies up to date.")

    return {
        "score": score_int,
        "grade": grade,
        "breakdown": breakdown,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Module interface
# ---------------------------------------------------------------------------

def run_all_scanners(project_root: str) -> dict:
    """Run every scanner and return a combined report dict."""
    secrets = scan_secrets(project_root)
    sast = scan_sast(project_root)
    log_leaks = scan_log_secret_leakage(project_root)
    vulns = scan_vulnerabilities(project_root)
    licenses = scan_licenses(project_root)
    env_parity = scan_env_parity(project_root)
    sbom = generate_sbom(project_root, format="summary")
    debt = compute_debt_score(project_root)

    return {
        "ok": True,
        "project_root": project_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "secrets": {
            "count": len(secrets),
            "findings": secrets,
        },
        "sast": {
            "count": len(sast),
            "findings": sast,
        },
        "log_secret_leakage": {
            "count": len(log_leaks),
            "findings": log_leaks,
        },
        "vulnerabilities": vulns,
        "licenses": licenses,
        "env_parity": env_parity,
        "sbom": sbom,
        "debt_score": debt,
    }


def get_security_summary(project_root: str) -> dict:
    """Quick summary — secret count, vuln count, debt score."""
    secrets = scan_secrets(project_root)
    vulns = scan_vulnerabilities(project_root)
    debt = compute_debt_score(project_root)

    critical_secrets = sum(1 for s in secrets if s.get("severity") == "critical")
    critical_vulns = sum(1 for v in vulns.get("vulnerabilities", []) if v.get("severity") == "critical")

    return {
        "ok": True,
        "secrets_total": len(secrets),
        "secrets_critical": critical_secrets,
        "vulnerabilities_total": len(vulns.get("vulnerabilities", [])),
        "vulnerabilities_critical": critical_vulns,
        "packages_checked": vulns.get("packages_checked", 0),
        "debt_score": debt["score"],
        "grade": debt["grade"],
        "top_recommendations": debt["recommendations"][:2],
    }


# ---------------------------------------------------------------------------
# CLI entry point (for manual testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="GrapeRoot Pro Security Scanner")
    parser.add_argument("project_root", nargs="?", default=".", help="Project root directory")
    parser.add_argument(
        "--scanner",
        choices=["secrets", "sast", "vulns", "licenses", "env", "sbom", "debt", "summary", "all"],
        default="summary",
        help="Which scanner to run",
    )
    parser.add_argument("--format", default="summary", help="SBOM format: summary|cyclonedx")
    parser.add_argument("--language", default="auto", help="SAST language: auto|python|javascript|go|java")
    args = parser.parse_args()

    root = str(Path(args.project_root).resolve())

    if args.scanner == "secrets":
        result = scan_secrets(root)
        print(json.dumps(result, indent=2))
    elif args.scanner == "sast":
        result = scan_sast(root, language=args.language)
        print(json.dumps(result, indent=2))
    elif args.scanner == "vulns":
        result = scan_vulnerabilities(root)
        print(json.dumps(result, indent=2))
    elif args.scanner == "licenses":
        result = scan_licenses(root)
        print(json.dumps(result, indent=2))
    elif args.scanner == "env":
        result = scan_env_parity(root)
        print(json.dumps(result, indent=2))
    elif args.scanner == "sbom":
        result = generate_sbom(root, format=args.format)
        print(json.dumps(result, indent=2))
    elif args.scanner == "debt":
        result = compute_debt_score(root)
        print(json.dumps(result, indent=2))
    elif args.scanner == "summary":
        result = get_security_summary(root)
        print(json.dumps(result, indent=2))
    elif args.scanner == "all":
        result = run_all_scanners(root)
        print(json.dumps(result, indent=2))
    else:
        print("Unknown scanner", file=sys.stderr)
        sys.exit(1)
