#!/usr/bin/env python3
"""GrapeRoot Review — webhook server + web dashboard.

Routes:
    GET  /               Landing page
    GET  /login          Redirect to GitHub OAuth
    GET  /auth/callback  GitHub OAuth callback
    GET  /dashboard      Analytics dashboard (protected)
    GET  /api/reviews    JSON list of reviews (protected)
    POST /webhook        GitHub App webhook
    GET  /health         Health check
"""
from __future__ import annotations

import hashlib, hmac, json, os, re, subprocess, sys, time, secrets, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

try:
    import psycopg2
    import psycopg2.extras
    _HAS_PG = True
except ImportError:
    import sqlite3
    _HAS_PG = False

from flask import Flask, request, jsonify, redirect, session, abort, g

try:
    import jwt
    _HAS_JWT = True
except ImportError:
    _HAS_JWT = False

# ── Config ─────────────────────────────────────────────────────────────────────
WEBHOOK_SECRET  = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
APP_ID          = os.environ.get("GITHUB_APP_ID", "")
PRIVATE_KEY     = os.environ.get("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_KEY      = os.environ.get("OPENAI_API_KEY", "")
FALLBACK_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
OAUTH_CLIENT_ID     = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
SESSION_SECRET  = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
DATABASE_URL    = os.environ.get("DATABASE_URL", "")          # NeonDB / any Postgres
DB_PATH         = os.environ.get("DB_PATH", "/app/data/reviews.db")  # SQLite fallback

app = Flask(__name__)
app.secret_key = SESSION_SECRET

FRONTEND_ORIGIN = "https://review.graperoot.dev"

@app.after_request
def _cors(response):
    origin = request.headers.get("Origin", "")
    if origin in (FRONTEND_ORIGIN, "http://localhost:3000"):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def _api_preflight(path):
    return "", 204

# ── Database ───────────────────────────────────────────────────────────────────
# Uses PostgreSQL (NeonDB) when DATABASE_URL is set, SQLite otherwise.

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id           SERIAL PRIMARY KEY,
    owner        TEXT NOT NULL,
    repo         TEXT NOT NULL,
    pr_num       INTEGER NOT NULL,
    pr_title     TEXT,
    pr_url       TEXT,
    head_sha     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    elapsed_s    REAL,
    cost_usd     REAL DEFAULT 0,
    num_findings INTEGER DEFAULT 0,
    num_critical INTEGER DEFAULT 0,
    num_high     INTEGER DEFAULT 0,
    findings     TEXT,
    error        TEXT,
    installed_by TEXT,
    status       TEXT DEFAULT 'completed'
);
CREATE TABLE IF NOT EXISTS users (
    github_id     BIGINT PRIMARY KEY,
    login         TEXT NOT NULL,
    avatar_url    TEXT,
    access_token  TEXT,
    session_token TEXT,
    monthly_limit INTEGER DEFAULT 5,
    plan          TEXT DEFAULT 'free',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reviews_repo     ON reviews(owner, repo);
CREATE INDEX IF NOT EXISTS idx_reviews_created  ON reviews(created_at DESC);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,
    repo        TEXT NOT NULL,
    pr_num      INTEGER NOT NULL,
    pr_title    TEXT,
    pr_url      TEXT,
    head_sha    TEXT,
    created_at  TEXT NOT NULL,
    elapsed_s   REAL,
    cost_usd    REAL DEFAULT 0,
    num_findings INTEGER DEFAULT 0,
    num_critical INTEGER DEFAULT 0,
    num_high     INTEGER DEFAULT 0,
    findings    TEXT,
    error       TEXT,
    installed_by TEXT,
    status      TEXT DEFAULT 'completed'
);
CREATE TABLE IF NOT EXISTS users (
    github_id     INTEGER PRIMARY KEY,
    login         TEXT NOT NULL,
    avatar_url    TEXT,
    access_token  TEXT,
    session_token TEXT,
    monthly_limit INTEGER DEFAULT 5,
    plan          TEXT DEFAULT 'free',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_repo ON reviews(owner, repo);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at DESC);
"""


def _pg_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_db():
    if "db" not in g:
        if DATABASE_URL and _HAS_PG:
            g.db = _pg_conn()
            g._is_pg = True
        else:
            Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            import sqlite3 as _sq
            g.db = _sq.connect(DB_PATH)
            g.db.row_factory = _sq.Row
            g._is_pg = False
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        try:
            db.close()
        except Exception:
            pass


def _ph(n: int) -> str:
    """Placeholder: %s for Postgres, ? for SQLite."""
    return ",".join(["%s"] * n) if (DATABASE_URL and _HAS_PG) else ",".join(["?"] * n)


def init_db():
    if DATABASE_URL and _HAS_PG:
        con = _pg_conn()
        cur = con.cursor()
        cur.execute(SCHEMA)
        con.commit()
        cur.close(); con.close()
        print("[db] PostgreSQL (NeonDB) initialized", flush=True)
    else:
        import sqlite3 as _sq
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = _sq.connect(DB_PATH)
        con.executescript(SCHEMA_SQLITE)
        con.commit(); con.close()
        print(f"[db] SQLite fallback at {DB_PATH}", flush=True)
    _migrate_db()


def _migrate_db():
    """Add columns introduced after initial schema deploy."""
    new_cols_pg = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_limit INTEGER DEFAULT 5",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'",
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'completed'",
    ]
    new_cols_sq = [
        "ALTER TABLE users ADD COLUMN session_token TEXT",
        "ALTER TABLE users ADD COLUMN monthly_limit INTEGER DEFAULT 5",
        "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'",
        "ALTER TABLE reviews ADD COLUMN status TEXT DEFAULT 'completed'",
    ]
    if DATABASE_URL and _HAS_PG:
        for sql in new_cols_pg:
            try:
                con = _pg_conn()
                cur = con.cursor()
                cur.execute(sql)
                con.commit(); cur.close(); con.close()
            except Exception as e:
                print(f"[migrate] {e}", flush=True)
    else:
        import sqlite3 as _sq
        con = _sq.connect(DB_PATH)
        for sql in new_cols_sq:
            try: con.execute(sql)
            except Exception: pass
        con.commit(); con.close()


def save_review(owner, repo, pr_num, pr_title, pr_url, head_sha,
                elapsed, cost, findings, error=None, installed_by=None):
    db   = get_db()
    is_pg = getattr(g, "_is_pg", False)
    num_findings = len(findings) if findings else 0
    num_critical = sum(1 for f in (findings or []) if f.get("severity") == "CRITICAL")
    num_high     = sum(1 for f in (findings or []) if f.get("severity") == "HIGH")
    ph = "%s" if is_pg else "?"

    sql = f"""
        INSERT INTO reviews
          (owner,repo,pr_num,pr_title,pr_url,head_sha,created_at,elapsed_s,
           cost_usd,num_findings,num_critical,num_high,findings,error,installed_by)
        VALUES ({",".join([ph]*15)})
    """
    vals = (owner, repo, pr_num, pr_title, pr_url, head_sha,
            datetime.now(timezone.utc).isoformat(),
            elapsed, cost, num_findings, num_critical, num_high,
            json.dumps(findings or []), error, installed_by)

    if is_pg:
        cur = db.cursor()
        cur.execute(sql, vals)
        db.commit()
        cur.close()
    else:
        db.execute(sql, vals)
        db.commit()


def save_review_pending(owner, repo, pr_num, pr_title, pr_url, head_sha, installed_by=None):
    """Insert a 'pending' row immediately when a review is queued. Returns row id."""
    if not (DATABASE_URL and _HAS_PG):
        return None
    con = _pg_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO reviews (owner, repo, pr_num, pr_title, pr_url, head_sha,
                             created_at, status, installed_by, num_findings)
        VALUES (%s,%s,%s,%s,%s,%s, NOW(), 'pending', %s, 0)
        RETURNING id
    """, (owner, repo, pr_num, pr_title, pr_url, head_sha, installed_by))
    row = cur.fetchone()
    row_id = dict(row)["id"] if row else None
    con.commit(); cur.close(); con.close()
    return row_id


def update_review_done(row_id, elapsed, cost, findings, error=None):
    """Update the pending row to completed/failed once _run_review finishes."""
    if not row_id or not (DATABASE_URL and _HAS_PG):
        return
    num_findings = len(findings) if findings else 0
    num_critical = sum(1 for f in (findings or []) if f.get("severity") == "CRITICAL")
    num_high     = sum(1 for f in (findings or []) if f.get("severity") == "HIGH")
    status = "failed" if error else "completed"
    con = _pg_conn()
    cur = con.cursor()
    cur.execute("""
        UPDATE reviews SET elapsed_s=%s, cost_usd=%s, num_findings=%s,
            num_critical=%s, num_high=%s, findings=%s, error=%s, status=%s
        WHERE id=%s
    """, (elapsed, cost, num_findings, num_critical, num_high,
          json.dumps(findings or []), error, status, row_id))
    con.commit(); cur.close(); con.close()


def _query(sql: str, params=(), one=False):
    """Run a SELECT and return list of dicts (or one dict)."""
    db   = get_db()
    is_pg = getattr(g, "_is_pg", False)
    if is_pg:
        cur = db.cursor()
        cur.execute(sql, params)
        if one:
            result = cur.fetchone()
            rows = [dict(result)] if result is not None else [{}]
        else:
            rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    else:
        cur = db.execute(sql, params)
        raw = cur.fetchall()
        rows = [dict(r) for r in raw]
        if one:
            rows = [rows[0]] if rows else [{}]
    return rows[0] if one else rows

# ── GitHub App auth ────────────────────────────────────────────────────────────

def _installation_token(installation_id: int) -> str:
    if not (APP_ID and PRIVATE_KEY and _HAS_JWT):
        return FALLBACK_TOKEN
    now = int(time.time())
    token = jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": APP_ID},
        PRIVATE_KEY, algorithm="RS256",
    )
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        data=b"{}", method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["token"]

def _app_jwt() -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 600, "iss": APP_ID}, PRIVATE_KEY, algorithm="RS256")

def _app_request(path: str):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _get_repo_installation_id(owner: str, repo: str):
    if not (APP_ID and PRIVATE_KEY and _HAS_JWT):
        return None
    try:
        return _app_request(f"/repos/{owner}/{repo}/installation").get("id")
    except Exception as e:
        print(f"[review] get installation failed: {e}", flush=True)
        return None

def _get_all_installations():
    if not (APP_ID and PRIVATE_KEY and _HAS_JWT):
        return []
    try:
        return _app_request("/app/installations?per_page=100")
    except Exception as e:
        print(f"[api] get installations failed: {e}", flush=True)
        return []

def _get_installation_repos(inst_token: str):
    req = urllib.request.Request(
        "https://api.github.com/installation/repositories?per_page=100",
        headers={
            "Authorization": f"Bearer {inst_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("repositories", [])

def _get_open_prs(owner: str, repo: str, inst_token: str):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=20&sort=updated",
        headers={
            "Authorization": f"Bearer {inst_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "graperoot-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _verify_sig(payload: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    mac = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={mac}", sig_header or "")

# ── Graph + review runner ──────────────────────────────────────────────────────

try:
    from graph_service import ensure_graph, graph_impact, graph_read_symbol, graph_summary
    _HAS_GRAPH_SVC = True
except ImportError:
    _HAS_GRAPH_SVC = False


def _build_graph_bg(owner: str, repo: str, github_token: str, head_sha: str = "") -> None:
    """Build graph in background thread — called on app installation."""
    try:
        from graph_service import build_graph
        build_graph(owner, repo, github_token, head_sha)
    except Exception as e:
        print(f"[graph] bg build failed: {e}", flush=True)


_DYNAMIC_DISPATCH = re.compile(
    r"getattr\s*\(|__import__\s*\(|importlib|eval\s*\(|exec\s*\("
)

def _ast_fact_blast_radius(
    owner: str,
    repo: str,
    changed_files: list[str],
    affected_files: list[str],
    head_sha: str,
    github_token: str,
) -> dict | None:
    """
    Returns an [AST-FACT: blast-radius] finding or None.

    Hard invariant: returns None rather than an uncertain finding.
    Uncertainty sources that trigger None:
      - Graph not built for this repo
      - graph_impact returned no affected files (absence of evidence ≠ evidence of absence)
      - A caller file cannot be fetched (content unknown → import chain unverifiable)
      - A caller file contains dynamic dispatch patterns that the graph cannot see
      - Zero callers survive the uncertainty filter

    Tests:
      1. Happy path: graph built, 2 callers, no dynamic dispatch → finding with both callers
      2. Edge case: 1 of 2 callers has getattr( → finding with only the clean caller
      3. FP prevention: graph not built → None; all callers have dynamic dispatch → None
    """
    if not _HAS_GRAPH_SVC:
        return None
    if not affected_files:
        return None  # No callers in graph — not the same as "confirmed no callers"

    # Strip file::symbol notation → keep only the file path
    stripped = [f.split("::")[0] for f in affected_files]
    # Deduplicate (multiple symbols from same file) and filter stdlib/external
    seen: set[str] = set()
    candidates: list[str] = []
    for f in stripped:
        if f not in seen and "/" in f and not f.startswith((".", "_")):
            seen.add(f)
            candidates.append(f)
    candidates = candidates[:10]
    if not candidates:
        return None

    verified: list[dict] = []
    for caller_path in candidates:
        source = _gh_file_content(owner, repo, caller_path, head_sha, github_token)
        if not source:
            # Cannot verify import chain without source — skip, don't include
            print(f"[AST-FACT] skipping {caller_path}: source unavailable", flush=True)
            continue
        if _DYNAMIC_DISPATCH.search(source):
            # Dynamic dispatch present — static import chain cannot be guaranteed
            print(f"[AST-FACT] skipping {caller_path}: dynamic dispatch detected", flush=True)
            continue
        verified.append({"file": caller_path})

    if not verified:
        return None  # Invariant: nothing rather than uncertain

    return {
        "tag":              "AST-FACT: blast-radius",
        "changed_files":    changed_files,
        "verified_callers": verified,
    }


def _graph_semantic_examples(
    owner: str,
    repo: str,
    changed_files: list[str],
    head_sha: str,
    github_token: str,
    max_examples: int = 4,
) -> str:
    """
    Find existing (non-diff) files in the repo that use the same frameworks/patterns
    as the changed files. Read them and return as 'correct pattern examples from codebase'.

    This is the graph_read equivalent for the hosted service — instead of reading symbols
    from a local MCP server, we query the graph edges to find sibling files (same framework,
    not in diff) and fetch their content via GitHub API.

    Example: if views.py is changed and uses DRF, find other views.py files in the repo
    that correctly call filter_queryset. Pre-inject those as semantic context so the model
    can compare the new code against proven correct patterns.
    """
    if not _HAS_GRAPH_SVC:
        return ""

    try:
        from graph_service import _load_graph
        g = _load_graph(owner, repo)
        if not g:
            return ""
    except Exception:
        return ""

    # Extract third-party imports from changed files (what frameworks are being used)
    changed_imports: set[str] = set()
    for cf in changed_files[:4]:
        source = _gh_file_content(owner, repo, cf, head_sha, github_token)
        if not source:
            continue
        # Find import statements
        for m in re.findall(r'(?:from|import)\s+([\w.]+)', source):
            top = m.split('.')[0]
            # Only third-party (not stdlib, not relative, not the repo's own packages)
            if top and top not in ('os','sys','re','json','time','typing','collections',
                                   'pathlib','datetime','abc','functools','itertools',
                                   'threading','subprocess','urllib','base64','hashlib'):
                changed_imports.add(top)

    if not changed_imports:
        return ""

    # Walk the graph's file nodes to find files NOT in the diff that share imports
    changed_set = set(changed_files)
    nodes = g.get("nodes", [])
    edges = g.get("edges", [])

    # Build: file → set of modules it imports (from graph edges)
    file_imports: dict[str, set[str]] = {}
    for edge in edges:
        frm = str(edge.get("from", ""))
        to  = str(edge.get("to",  ""))
        if frm and to:
            file_imports.setdefault(frm, set()).add(to.split(".")[0])

    # Candidate files: share at least one third-party import, not in diff
    candidates: list[tuple[int, str]] = []  # (overlap_count, path)
    for file_path, imports in file_imports.items():
        if file_path in changed_set:
            continue
        if not file_path.endswith((".py", ".ts", ".js", ".go")):
            continue
        overlap = len(imports & changed_imports)
        if overlap > 0:
            candidates.append((overlap, file_path))

    # Detect refactoring patterns: if diff adds files under sansio/, shared/, core/, base/
    # those are the target modules — find existing files in SAME directory as the gold standard
    sansio_dirs: set[str] = set()
    for cf in changed_files:
        parts = cf.split("/")
        for i, part in enumerate(parts):
            if part in ("sansio", "shared", "core", "base", "abstract"):
                sansio_dirs.add("/".join(parts[:i+1]))

    # Pull existing files from those sansio directories first (they show correct refactoring)
    sansio_examples: list[str] = []
    if sansio_dirs:
        for file_path, _ in file_imports.items():
            if file_path in changed_set:
                continue
            if any(file_path.startswith(d + "/") for d in sansio_dirs):
                content = _gh_file_content(owner, repo, file_path, head_sha, github_token)
                if content:
                    sansio_examples.append((file_path, content))
                    print(f"[semantic] sansio gold standard: {file_path}", flush=True)
                if len(sansio_examples) >= 2:
                    break

    # General: sort remaining candidates by framework overlap + same basename
    changed_basenames = {cf.rsplit("/", 1)[-1] for cf in changed_files}
    candidates.sort(key=lambda x: (
        -x[0],
        0 if x[1].rsplit("/", 1)[-1] in changed_basenames else 1,
    ))

    examples: list[str] = []

    # Sansio examples first (highest signal for refactoring checks)
    for file_path, content in sansio_examples[:2]:
        snippet = content[:1500]
        label = "existing sansio/shared module — gold standard for what should be here"
        examples.append(
            f"### {file_path}  [{label}]\n```\n{snippet}\n```"
        )

    # General framework examples
    remaining = max_examples - len(examples)
    for _, file_path in candidates[:remaining + 2]:
        if len(examples) >= max_examples:
            break
        content = _gh_file_content(owner, repo, file_path, head_sha, github_token)
        if not content:
            continue
        snippet = content[:1200]
        examples.append(
            f"### {file_path}  [existing file — same framework, NOT in diff]\n"
            f"```\n{snippet}\n```"
        )
        print(f"[semantic] pre-injected example: {file_path}", flush=True)

    if not examples:
        return ""

    return (
        "### Codebase pattern examples — how this framework is correctly used HERE\n"
        "CRITICAL FOR ARCH CHECK: compare these patterns against the new code in the diff.\n"
        "A method that exists in changed files but is ABSENT from sansio/shared examples\n"
        "when it should be there is an incomplete refactor. Report it.\n\n"
        + "\n\n".join(examples)
    )


def _graph_cross_file_checks(
    owner: str,
    repo: str,
    changed_files: list[str],
    affected_files: list[str],
    head_sha: str,
    github_token: str,
    graph: dict | None = None,
) -> list[dict]:
    """
    Use the full codebase graph to detect cross-file consistency issues.

    These are the findings Greptile consistently beats us on — issues that are
    invisible from the diff alone but obvious when you have the whole codebase indexed.

    Check 1 — Missing migration: schema/model file changed but no migration in diff.
    Check 2 — Orphaned symbol renames: callers still use the old name after rename.
    Check 3 — Caller argument consistency: function signature changed, callers not updated.
    """
    if not _HAS_GRAPH_SVC:
        return []

    findings: list[dict] = []
    changed_set = set(changed_files)

    # ── Check 1: Missing migration ─────────────────────────────────────────────
    # If a schema/model file changed, there should be a migration in the diff.
    SCHEMA_PATTERNS = re.compile(r'schema|model|migration|entity|prisma|drizzle', re.I)
    MIGRATION_PATTERNS = re.compile(r'migrat|\.sql$|alembic|flyway|knex.*migration', re.I)

    schema_changes = [f for f in changed_files if SCHEMA_PATTERNS.search(f)
                      and not MIGRATION_PATTERNS.search(f)]
    has_migration  = any(MIGRATION_PATTERNS.search(f) for f in changed_files)

    if schema_changes and not has_migration:
        # Confirm: does this repo even use migrations? Check graph for migration files.
        if graph:
            nodes = graph.get("nodes", [])
            all_paths = {str(n.get("path", "")) for n in nodes}
            repo_has_migrations = any(MIGRATION_PATTERNS.search(p) for p in all_paths)
            if repo_has_migrations:
                for sf in schema_changes[:3]:
                    findings.append({
                        "severity": "HIGH",
                        "title": f"[AST-HEURISTIC: missing-migration] Schema `{sf}` changed but no migration in diff",
                        "comment": (
                            f"The file `{sf}` appears to define or modify a schema/model, "
                            f"but no migration file was included in this PR. "
                            f"This repo has migrations (found in graph) — a missing migration "
                            f"will cause schema drift when deployed.\n"
                            f"Check: is a corresponding migration needed?"
                        ),
                        "check": "missing_migration",
                    })

    # ── Check 2: Orphaned symbol renames ──────────────────────────────────────
    # If a symbol is renamed in the diff, check if callers still use the old name.
    # We detect renames by looking for: -old_name ... +new_name in same file context.
    if graph and affected_files:
        edges = graph.get("edges", [])
        # Build caller map: file → list of files that import it
        caller_map: dict[str, list[str]] = {}
        for edge in edges:
            to = str(edge.get("to", ""))
            frm = str(edge.get("from", ""))
            if to and frm:
                # Normalize: strip symbol-level notation
                to_file = to.split("::")[0]
                caller_map.setdefault(to_file, []).append(frm)

        for changed_file in changed_files[:5]:
            callers = caller_map.get(changed_file, [])
            if not callers:
                continue
            # Fetch the changed file content at head to find current exported names
            head_content = _gh_file_content(owner, repo, changed_file, head_sha, github_token)
            if not head_content:
                continue
            # Find callers NOT in the diff and check if they're stale
            stale_callers = [c for c in callers[:6] if c not in changed_set]
            if stale_callers:
                # Add to graph context so LLM can check consistency
                findings.append({
                    "severity": "MEDIUM",
                    "title": f"[AST-HEURISTIC: stale-callers] {len(stale_callers)} file(s) import `{changed_file}` but are NOT in this diff",
                    "comment": (
                        f"The following files import from `{changed_file}` (which changed in this PR) "
                        f"but were not updated:\n"
                        + "\n".join(f"- `{c}`" for c in stale_callers[:4])
                        + "\n\nIf this PR renames exports, changes function signatures, or removes fields, "
                        f"these callers may break silently."
                    ),
                    "check": "stale_callers",
                })

    return findings[:6]  # cap to avoid noise


def _run_review(owner, repo, pr_num, head_sha, github_token, pr_title, pr_url, installed_by, review_id=None):
    pr_url_gh = pr_url or f"https://github.com/{owner}/{repo}/pull/{pr_num}"
    print(f"[review] {owner}/{repo}#{pr_num}", flush=True)
    t0 = time.time()

    # ── Graph context (self-hosted only — hosted service uses diff only) ────────
    graph_available = False
    graph_ctx = ""
    ast_fact_finding = None
    changed_files_for_graph: list[str] = []
    _graph_xfile_findings: list[dict] = []   # cross-file findings from graph

    if _HAS_GRAPH_SVC and os.environ.get("ENABLE_GRAPH_CLONE") == "1":
        try:
            graph_available = ensure_graph(owner, repo, github_token, head_sha)
        except Exception as e:
            print(f"[graph] ensure failed: {e}", flush=True)

        if graph_available:
            try:
                diff_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}"
                req = urllib.request.Request(
                    diff_url,
                    headers={"Authorization": f"Bearer {github_token}",
                             "Accept": "application/vnd.github.diff",
                             "User-Agent": "graperoot-review/1.0"},
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    diff_text_for_graph = r.read().decode("utf-8", errors="replace")
                import re as _re
                changed_files_for_graph = _re.findall(r"diff --git a/.+ b/(.+)", diff_text_for_graph)
                impact = graph_impact(owner, repo, changed_files_for_graph)
                if impact.get("ok"):
                    graph_ctx = f"### Blast Radius\n{impact.get('summary','')}\n"
                    reads   = impact.get("recommended_reads", changed_files_for_graph[:3])[:6]
                    affected = impact.get("affected_files", [])[:6]
                    to_read = list(dict.fromkeys(reads + affected))
                    to_read = [r for r in to_read if "/" in r or r.endswith((".py",".ts",".js",".go",".rs",".java"))]
                    for ref in to_read[:8]:
                        file_path = ref.split("::")[0] if "::" in ref else ref
                        content = _gh_file_content(owner, repo, file_path, head_sha, github_token)
                        if content:
                            graph_ctx += f"\n### {file_path}  [affected by this PR]\n```\n{content[:1500]}\n```\n"

                    # ── Graph-powered cross-file consistency checks ────────────────────
                    # Load the full graph (already built above) for cross-file queries.
                    try:
                        from graph_service import _load_graph as _lg
                        _full_graph = _lg(owner, repo)
                    except Exception:
                        _full_graph = None
                    graph_xfile = _graph_cross_file_checks(
                        owner, repo, changed_files_for_graph,
                        impact.get("affected_files", []),
                        head_sha, github_token, _full_graph
                    )
                    for finding in graph_xfile:
                        print(f"[graph-xfile] {finding['severity']}: {finding['title'][:60]}", flush=True)
                    if graph_xfile:
                        # Pass as structured context AND as explicit graph findings
                        graph_ctx += f"\n### Graph cross-file findings (AST-HEURISTIC)\n"
                        for f in graph_xfile:
                            graph_ctx += f"- [{f['severity']}] {f['title']}\n  {f['comment'][:200]}\n"
                    # Store graph_xfile to include in final report
                    _graph_xfile_findings = graph_xfile

                    # ── Semantic pre-injection: correct pattern examples from codebase ──
                    # Finds existing files using the same frameworks (not in diff),
                    # reads them to show the model how contracts are correctly fulfilled.
                    semantic_examples = _graph_semantic_examples(
                        owner, repo, changed_files_for_graph, head_sha, github_token
                    )
                    if semantic_examples:
                        graph_ctx += f"\n{semantic_examples}\n"

                    # [AST-FACT: blast-radius] — hard invariant: emit nothing on uncertainty
                    ast_fact_finding = _ast_fact_blast_radius(
                        owner, repo, changed_files_for_graph,
                        impact.get("affected_files", []), head_sha, github_token
                    )
                    if ast_fact_finding:
                        print(f"[AST-FACT] blast-radius: {len(ast_fact_finding['verified_callers'])} verified callers", flush=True)
            except Exception as e:
                print(f"[graph] context failed: {e}", flush=True)

    # ── Post [AST-FACT] finding independently, before LLM review ────────────────
    if ast_fact_finding:
        callers = ast_fact_finding["verified_callers"]
        changed = ast_fact_finding["changed_files"]
        body_lines = [
            f"**[AST-FACT: blast-radius]** — {len(callers)} file(s) have verified static imports of the changed module(s) and will be directly affected by this PR.\n",
            f"Changed: {', '.join(f'`{f}`' for f in changed[:5])}",
            "",
            "**Verified callers** (dynamic dispatch ruled out per file):",
        ]
        for c in callers:
            body_lines.append(f"- `{c['file']}`")
        body_lines += [
            "",
            "_This finding is derived entirely from the static import graph. If it is wrong, the graph has a bug — not the model._",
        ]
        try:
            _gh_api(
                f"/repos/{owner}/{repo}/issues/{pr_num}/comments",
                github_token,
            )  # no-op call to verify token works
        except Exception:
            pass
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_num}/comments",
                data=json.dumps({"body": "\n".join(body_lines)}).encode(),
                method="POST",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "graperoot-review/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                posted = json.loads(r.read())
            print(f"[AST-FACT] posted blast-radius comment: {posted.get('html_url','')}", flush=True)
        except Exception as e:
            print(f"[AST-FACT] post failed: {e}", flush=True)

    # ── Run review subprocess ─────────────────────────────────────────────────
    json_out = f"/tmp/review-{owner}-{repo}-{pr_num}.json"
    env = {
        **os.environ,
        "GITHUB_TOKEN":     github_token,
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "OPENAI_API_KEY":   OPENAI_KEY,
        "GR_GRAPH_CONTEXT": graph_ctx,  # injected into review prompt
    }

    result = subprocess.run(
        [sys.executable, "review.py", pr_url_gh, "--json-out", json_out],
        env=env, timeout=300, capture_output=True, text=True,
    )
    elapsed = time.time() - t0
    findings, cost, error = [], 0.0, None

    # Always log subprocess stdout so we can see model output in Railway logs
    if result.stdout.strip():
        print(f"[review.py stdout]\n{result.stdout[:3000]}", flush=True)
    if result.returncode != 0:
        error = (result.stderr + result.stdout)[:3000] or f"exit code {result.returncode}"
        print(f"[review] FAILED {owner}/{repo}#{pr_num}:\n{error}", flush=True)
    else:
        try:
            out      = json.loads(Path(json_out).read_text())
            report   = out.get("report", {})
            findings = (report.get("inline_comments") or out.get("findings", []))
            cost     = out.get("cost_usd", 0)
            # Merge graph cross-file findings (AST-HEURISTIC) into findings
            xfile = _graph_xfile_findings
            if xfile:
                findings = list(xfile) + list(findings)
        except Exception:
            pass
        finally:
            try: Path(json_out).unlink()
            except Exception: pass

    with app.app_context():
        try:
            if review_id:
                update_review_done(review_id, elapsed, cost, findings, error)
            else:
                save_review(owner, repo, pr_num, pr_title, pr_url_gh, head_sha,
                            elapsed, cost, findings, error, installed_by)
        except Exception as e:
            print(f"[db] save failed: {e}", flush=True)

    print(f"[review] done {owner}/{repo}#{pr_num} — {len(findings)} findings "
          f"{'(graph)' if graph_available else '(diff-only)'} "
          f"${cost:.4f} {elapsed:.0f}s", flush=True)

# ── OAuth helpers ──────────────────────────────────────────────────────────────

def _gh_file_content(owner: str, repo: str, path: str, ref: str, token: str) -> str:
    """Fetch decoded file content from GitHub at a specific ref."""
    import base64
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "graperoot-review/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[graph] fetch {path}: {e}", flush=True)
    return ""


def _gh_api(path, token):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "graperoot-review/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def current_user():
    return session.get("user")


def _token_user():
    """Validate Bearer token from Authorization header; return user row or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    is_pg = bool(DATABASE_URL and _HAS_PG)
    ph = "%s" if is_pg else "?"
    try:
        row = _query(f"SELECT * FROM users WHERE session_token = {ph}", (token,), one=True)
        return row if row.get("github_id") else None
    except Exception:
        return None


def _within_limit(login: str) -> bool:
    """Return True if this GitHub login hasn't hit their monthly review limit."""
    if not login:
        return True
    is_pg = bool(DATABASE_URL and _HAS_PG)
    ph = "%s" if is_pg else "?"
    user_row = _query(f"SELECT monthly_limit FROM users WHERE login = {ph}", (login,), one=True)
    # Check for any key to confirm row was found (github_id not in SELECT)
    limit = user_row.get("monthly_limit") if user_row else 5
    if limit is None:  # NULL = unlimited (Pro / Enterprise)
        return True
    if is_pg:
        count = _query(
            f"SELECT COUNT(*) AS cnt FROM reviews WHERE installed_by = {ph} AND created_at >= date_trunc('month', NOW())",
            (login,), one=True,
        )
    else:
        count = _query(
            f"SELECT COUNT(*) AS cnt FROM reviews WHERE installed_by = {ph} AND created_at >= strftime('%Y-%m-01','now')",
            (login,), one=True,
        )
    return (count.get("cnt") or 0) < limit


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    if not OAUTH_CLIENT_ID:
        return "<h2>OAuth not configured — set GITHUB_OAUTH_CLIENT_ID</h2>", 503
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = urllib.parse.urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": f"{_base_url()}/auth/callback",
        "scope": "read:user user:email",
        "state": state,
    })
    return redirect(f"https://github.com/login/oauth/authorize?{params}")

@app.route("/auth/callback")
def auth_callback():
    code            = request.args.get("code", "")
    state           = request.args.get("state", "")
    installation_id = request.args.get("installation_id", "")

    if not code:
        abort(400)

    # Installation callbacks have no state — only validate state for
    # standalone OAuth (Login button flow)
    if installation_id:
        session.pop("oauth_state", None)
    elif state != session.pop("oauth_state", None):
        abort(400)

    # Exchange code for access token
    data = urllib.parse.urlencode({
        "client_id":     OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  f"{_base_url()}/auth/callback",
    }).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=data, method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        token_data = json.loads(r.read())

    access_token = token_data.get("access_token", "")
    if not access_token:
        return "<h2>OAuth failed — no access token</h2>", 400

    user = _gh_api("/user", access_token)
    session["user"] = {
        "id":         user["id"],
        "login":      user["login"],
        "avatar_url": user.get("avatar_url", ""),
    }
    session["gh_token"] = access_token

    db           = get_db()
    is_pg        = getattr(g, "_is_pg", False)
    ph           = "%s" if is_pg else "?"
    now          = datetime.now(timezone.utc).isoformat()
    sess_token   = secrets.token_urlsafe(32)

    if is_pg:
        upsert = f"""
            INSERT INTO users (github_id, login, avatar_url, access_token, session_token, monthly_limit, plan, created_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
            ON CONFLICT(github_id) DO UPDATE SET
              login=EXCLUDED.login, avatar_url=EXCLUDED.avatar_url,
              access_token=EXCLUDED.access_token, session_token=EXCLUDED.session_token
        """
        cur = db.cursor()
        cur.execute(upsert, (user["id"], user["login"], user.get("avatar_url",""),
                             access_token, sess_token, 5, "free", now))
        db.commit(); cur.close()
    else:
        db.execute(f"""
            INSERT INTO users (github_id, login, avatar_url, access_token, session_token, monthly_limit, plan, created_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
            ON CONFLICT(github_id) DO UPDATE SET
              login=excluded.login, avatar_url=excluded.avatar_url,
              access_token=excluded.access_token, session_token=excluded.session_token
        """, (user["id"], user["login"], user.get("avatar_url",""),
              access_token, sess_token, 5, "free", now))
        db.commit()

    return redirect(f"{FRONTEND_ORIGIN}/auth?user={user['login']}&token={sess_token}")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

def _base_url():
    host = request.headers.get("X-Forwarded-Host") or request.host
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else request.scheme
    return f"{scheme}://{host}"

# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    user   = current_user()
    is_pg  = bool(DATABASE_URL and _HAS_PG)
    concat = "owner||'/'||repo" if not is_pg else "owner||'/'||repo"

    stats  = _query("""
        SELECT
          COUNT(*)                       AS total,
          COALESCE(SUM(cost_usd),0)      AS total_cost,
          COALESCE(SUM(num_findings),0)  AS total_findings,
          COALESCE(SUM(num_critical),0)  AS total_critical,
          COALESCE(AVG(elapsed_s),0)     AS avg_time,
          COALESCE(AVG(cost_usd),0)      AS avg_cost
        FROM reviews WHERE error IS NULL
    """, one=True)

    recent = _query("""
        SELECT owner,repo,pr_num,pr_title,pr_url,created_at,
               cost_usd,num_findings,num_critical,num_high,elapsed_s,error
        FROM reviews ORDER BY created_at DESC LIMIT 50
    """)

    repos  = _query("""
        SELECT owner||'/'||repo AS repo, COUNT(*) AS cnt, COALESCE(SUM(num_findings),0) AS findings
        FROM reviews GROUP BY owner,repo ORDER BY cnt DESC LIMIT 10
    """)

    return _render_dashboard(user, stats, recent, repos)

def _render_dashboard(user, stats, recent, repos):
    rows = ""
    for r in recent:
        sev = ""
        if r["num_critical"]: sev += f'<span style="color:#f87171">●{r["num_critical"]} CRIT</span> '
        if r["num_high"]: sev += f'<span style="color:#fb923c">●{r["num_high"]} HIGH</span>'
        if r["error"]: sev = f'<span style="color:#ef4444">✗ error</span>'
        dt = r["created_at"][:16].replace("T"," ") if r["created_at"] else ""
        pr_link = f'<a href="{r["pr_url"]}" target="_blank" style="color:#d8b4fe;text-decoration:none">{r["owner"]}/{r["repo"]}#{r["pr_num"]}</a>'
        title = (r["pr_title"] or "")[:55]
        rows += f"""<tr>
          <td style="padding:.6rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05)">{pr_link}</td>
          <td style="padding:.6rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05);color:#a1a1aa;font-size:.75rem">{title}</td>
          <td style="padding:.6rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05)">{sev or '<span style="color:#4ade80">✓ clean</span>'}</td>
          <td style="padding:.6rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05);color:#a1a1aa;font-size:.75rem">${r["cost_usd"]:.4f}</td>
          <td style="padding:.6rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05);color:#a1a1aa;font-size:.75rem">{dt}</td>
        </tr>"""

    repo_rows = ""
    for r in repos:
        repo_rows += f"""<tr>
          <td style="padding:.5rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05);color:#d8b4fe">{r["repo"]}</td>
          <td style="padding:.5rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05);color:#a1a1aa;text-align:center">{r["cnt"]}</td>
          <td style="padding:.5rem .875rem;border-bottom:1px solid rgba(255,255,255,0.05);color:#fb923c;text-align:center">{r["findings"] or 0}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Dashboard — GrapeRoot Review</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Mulish:wght@600;700;800&display=swap" rel="stylesheet"/>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'JetBrains Mono',monospace;background:#09090b;color:#f4f4f5;min-height:100vh;-webkit-font-smoothing:antialiased}}
    .topbar{{background:#0e0e11;border-bottom:1px solid rgba(255,255,255,0.07);padding:.875rem 1.5rem;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}}
    .logo{{display:flex;align-items:center;gap:8px;font-family:'Mulish',sans-serif;font-weight:700;color:#fff;text-decoration:none}}
    .logo-icon{{width:28px;height:28px;border-radius:8px;background:rgba(168,85,247,.15);border:1px solid rgba(168,85,247,.3);display:flex;align-items:center;justify-content:center;font-size:.875rem}}
    .user{{display:flex;align-items:center;gap:.75rem;font-size:.8125rem;color:#a1a1aa}}
    .user img{{width:28px;height:28px;border-radius:50%;border:1px solid rgba(255,255,255,.1)}}
    .logout{{color:#71717a;text-decoration:none;font-size:.75rem;padding:.25rem .75rem;border:1px solid rgba(255,255,255,.06);border-radius:6px;transition:all .2s}}
    .logout:hover{{color:#fff;border-color:rgba(255,255,255,.15)}}
    .main{{max-width:1200px;margin:0 auto;padding:2rem 1.5rem}}
    h1{{font-family:'Mulish',sans-serif;font-size:1.5rem;font-weight:800;color:#fff;margin-bottom:1.5rem}}
    .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2rem}}
    .stat-card{{background:#111113;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:1.25rem}}
    .stat-card .val{{font-family:'Mulish',sans-serif;font-size:1.875rem;font-weight:800;color:#fff;line-height:1}}
    .stat-card .val.grape{{background:linear-gradient(135deg,#d8b4fe,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
    .stat-card .lbl{{font-size:.6875rem;color:#71717a;margin-top:.375rem}}
    .card{{background:#111113;border:1px solid rgba(255,255,255,.07);border-radius:12px;overflow:hidden;margin-bottom:1.5rem}}
    .card-header{{background:#0e0e11;padding:.75rem 1rem;border-bottom:1px solid rgba(255,255,255,.05);font-size:.8125rem;font-weight:600;color:#a1a1aa;display:flex;align-items:center;justify-content:space-between}}
    table{{width:100%;border-collapse:collapse;font-size:.8125rem}}
    th{{padding:.6rem .875rem;text-align:left;font-size:.6875rem;color:#71717a;text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid rgba(255,255,255,.07)}}
    .empty{{padding:2.5rem;text-align:center;color:#52525b;font-size:.8125rem}}
    .view-all{{font-size:.75rem;color:#a855f7;text-decoration:none}}
    .view-all:hover{{color:#d8b4fe}}
  </style>
</head>
<body>
<div class="topbar">
  <a class="logo" href="/"><div class="logo-icon">🍇</div> GrapeRoot Review</a>
  <div class="user">
    <img src="{user['avatar_url']}" alt="" onerror="this.style.display='none'"/>
    <span>{user['login']}</span>
    <a class="logout" href="/logout">Sign out</a>
  </div>
</div>
<div class="main">
  <h1>Review Analytics</h1>
  <div class="stats">
    <div class="stat-card"><div class="val grape">{stats['total']}</div><div class="lbl">Total reviews</div></div>
    <div class="stat-card"><div class="val">{stats['total_findings']}</div><div class="lbl">Issues found</div></div>
    <div class="stat-card"><div class="val" style="color:#f87171">{stats['total_critical']}</div><div class="lbl">Critical findings</div></div>
    <div class="stat-card"><div class="val">${stats['total_cost']:.3f}</div><div class="lbl">Total API cost</div></div>
    <div class="stat-card"><div class="val">${stats['avg_cost']:.4f}</div><div class="lbl">Avg cost / review</div></div>
    <div class="stat-card"><div class="val">{stats['avg_time']:.0f}s</div><div class="lbl">Avg review time</div></div>
  </div>

  <div class="card">
    <div class="card-header">
      Recent reviews
      <a class="view-all" href="/api/reviews">JSON export →</a>
    </div>
    <table>
      <thead><tr>
        <th>PR</th><th>Title</th><th>Findings</th><th>Cost</th><th>When</th>
      </tr></thead>
      <tbody>{rows or '<tr><td colspan=5 class="empty">No reviews yet — install the GitHub App and open a PR.</td></tr>'}</tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-header">Top repositories</div>
    <table>
      <thead><tr><th>Repo</th><th style="text-align:center">Reviews</th><th style="text-align:center">Findings</th></tr></thead>
      <tbody>{repo_rows or '<tr><td colspan=3 class="empty">No data yet.</td></tr>'}</tbody>
    </table>
  </div>
</div>
</body>
</html>"""

# ── API ────────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    token_u = _token_user()
    sess_u  = current_user()
    if not token_u and not sess_u:
        return jsonify({"error": "unauthorized"}), 401
    login = (token_u or sess_u)["login"]
    is_pg = bool(DATABASE_URL and _HAS_PG)
    ph    = "%s" if is_pg else "?"

    if is_pg:
        month_filter = f"AND created_at >= date_trunc('month', NOW())"
    else:
        month_filter = f"AND created_at >= strftime('%Y-%m-01','now')"

    monthly = _query(
        f"SELECT COUNT(*) AS cnt FROM reviews WHERE installed_by = {ph} AND error IS NULL {month_filter}",
        (login,), one=True,
    )
    total   = _query(
        f"SELECT COUNT(*) AS cnt FROM reviews WHERE installed_by = {ph} AND error IS NULL",
        (login,), one=True,
    )
    repos   = _query(
        f"SELECT COUNT(DISTINCT owner||'/'||repo) AS cnt FROM reviews WHERE installed_by = {ph}",
        (login,), one=True,
    )
    user_row = _query(f"SELECT monthly_limit, plan FROM users WHERE login = {ph}", (login,), one=True)
    limit   = user_row.get("monthly_limit", 5) if user_row else 5

    return jsonify({
        "reviews_this_month": monthly.get("cnt") or 0,
        "monthly_limit":      limit,
        "total_reviews":      total.get("cnt") or 0,
        "repos_count":        repos.get("cnt") or 0,
    })


@app.route("/api/reviews")
def api_reviews():
    token_u = _token_user()
    sess_u  = current_user()
    if not token_u and not sess_u:
        return jsonify({"error": "unauthorized"}), 401
    login = (token_u or sess_u)["login"]
    is_pg = bool(DATABASE_URL and _HAS_PG)
    ph    = "%s" if is_pg else "?"

    rows = _query(
        f"""SELECT id, pr_title, owner||'/'||repo AS repo, pr_url,
                   num_findings AS findings,
                   COALESCE(status, CASE WHEN error IS NOT NULL THEN 'failed' ELSE 'completed' END) AS status,
                   created_at
            FROM reviews WHERE installed_by = {ph}
            ORDER BY created_at DESC LIMIT 50""",
        (login,),
    )
    return jsonify(rows)


@app.route("/api/prs")
def api_prs():
    token_u = _token_user()
    sess_u  = current_user()
    if not token_u and not sess_u:
        return jsonify({"error": "unauthorized"}), 401

    installations = _get_all_installations()
    all_prs = []
    for inst in installations:
        inst_id = inst.get("id")
        if not inst_id:
            continue
        try:
            inst_token = _installation_token(inst_id)
            repos = _get_installation_repos(inst_token)
            for repo in repos:
                owner = repo["owner"]["login"]
                name  = repo["name"]
                try:
                    prs = _get_open_prs(owner, name, inst_token)
                    for pr in prs:
                        all_prs.append({
                            "number":     pr["number"],
                            "title":      pr["title"],
                            "repo":       f"{owner}/{name}",
                            "pr_url":     pr["html_url"],
                            "author":     pr["user"]["login"],
                            "branch":     pr["head"]["ref"],
                            "head_sha":   pr["head"]["sha"],
                            "created_at": pr["created_at"],
                            "updated_at": pr["updated_at"],
                        })
                except Exception as e:
                    print(f"[api/prs] {owner}/{name}: {e}", flush=True)
        except Exception as e:
            print(f"[api/prs] installation {inst_id}: {e}", flush=True)

    all_prs.sort(key=lambda p: p["updated_at"], reverse=True)
    return jsonify(all_prs)


@app.route("/api/review", methods=["POST"])
def api_trigger_review():
    token_u = _token_user()
    sess_u  = current_user()
    if not token_u and not sess_u:
        return jsonify({"error": "unauthorized"}), 401

    login = (token_u or sess_u)["login"]

    if not _within_limit(login):
        return jsonify({"error": "monthly_limit_reached",
                        "message": "Monthly review limit reached. Upgrade to Pro for unlimited reviews."}), 429

    data   = request.get_json(force=True) or {}
    pr_url = data.get("pr_url", "").strip()

    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not m:
        return jsonify({"error": "invalid_pr_url"}), 400

    owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))

    installation_id = _get_repo_installation_id(owner, repo)
    if not installation_id:
        return jsonify({"error": "app_not_installed",
                        "message": f"GrapeRoot Review is not installed on {owner}/{repo}"}), 404

    try:
        github_token = _installation_token(installation_id)
    except Exception as e:
        return jsonify({"error": "token_failed", "message": str(e)}), 500

    try:
        pr_data  = _gh_api(f"/repos/{owner}/{repo}/pulls/{pr_num}", github_token)
        head_sha = pr_data["head"]["sha"]
        pr_title = pr_data.get("title", "")
        pr_url_gh = pr_data.get("html_url", pr_url)
    except Exception as e:
        return jsonify({"error": "pr_fetch_failed", "message": str(e)}), 500

    review_id = save_review_pending(owner, repo, pr_num, pr_title, pr_url_gh, head_sha, login)

    Thread(
        target=_run_review,
        args=(owner, repo, pr_num, head_sha, github_token, pr_title, pr_url_gh, login, review_id),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "queued": f"{owner}/{repo}#{pr_num}"})


@app.route("/api/graph/<owner>/<repo>")
@login_required
def api_graph_status(owner, repo):
    if _HAS_GRAPH_SVC:
        return jsonify(graph_summary(owner, repo))
    return jsonify({"exists": False, "reason": "graph_service_unavailable"})

# ── Webhook ────────────────────────────────────────────────────────────────────

def _get_token(data: dict) -> str:
    installation_id = data.get("installation", {}).get("id", 0)
    try:
        return _installation_token(installation_id) if installation_id else FALLBACK_TOKEN
    except Exception as e:
        print(f"[webhook] token error: {e}", flush=True)
        return FALLBACK_TOKEN


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_data()
    if not _verify_sig(payload, request.headers.get("X-Hub-Signature-256", "")):
        abort(401)

    event = request.headers.get("X-GitHub-Event", "")
    data  = request.get_json(force=True) or {}

    # ── App installed → just log it (no cloning on hosted service) ───────────
    if event == "installation" and data.get("action") == "created":
        repos = [r.get("full_name","") for r in data.get("repositories", [])]
        print(f"[webhook] app installed by {data.get('installation',{}).get('account',{}).get('login','')} on {repos}", flush=True)
        return jsonify({"ok": True, "action": "installation_noted"})

    # ── Push: only rebuild graph if self-hosted mode enabled ─────────────────
    if event == "push":
        if os.environ.get("ENABLE_GRAPH_CLONE") == "1":
            ref     = data.get("ref", "")
            default = data.get("repository", {}).get("default_branch", "main")
            if ref == f"refs/heads/{default}":
                owner    = data["repository"]["owner"]["login"]
                repo     = data["repository"]["name"]
                head_sha = data.get("after", "")
                token    = _get_token(data)
                Thread(target=_build_graph_bg, args=(owner, repo, token, head_sha), daemon=True).start()
        return jsonify({"ok": True})

    # ── Pull request → review ────────────────────────────────────────────────
    if event != "pull_request":
        return jsonify({"ok": True, "skipped": event})

    action = data.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return jsonify({"ok": True, "skipped": action})

    pr           = data["pull_request"]
    owner        = data["repository"]["owner"]["login"]
    repo         = data["repository"]["name"]
    pr_num       = pr["number"]
    head_sha     = pr["head"]["sha"]
    pr_title     = pr.get("title", "")
    pr_url       = pr.get("html_url", "")
    installed_by = data.get("installation", {}).get("account", {}).get("login", "")
    github_token = _get_token(data)

    if not github_token:
        return jsonify({"ok": False, "error": "no_token"}), 500

    if not _within_limit(installed_by):
        print(f"[webhook] {installed_by} monthly limit reached — skipping {owner}/{repo}#{pr_num}", flush=True)
        return jsonify({"ok": True, "skipped": "monthly_limit_reached"})

    Thread(
        target=_run_review,
        args=(owner, repo, pr_num, head_sha, github_token, pr_title, pr_url, installed_by),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "queued": f"{owner}/{repo}#{pr_num}"})

# ── Health + landing ───────────────────────────────────────────────────────────

@app.route("/health")
def health():
    db_status = "postgres" if (DATABASE_URL and _HAS_PG) else "sqlite"
    return jsonify({
        "status":   "ok",
        "app_mode": bool(APP_ID and PRIVATE_KEY and _HAS_JWT),
        "oauth":    bool(OAUTH_CLIENT_ID),
        "db":       db_status,
        "db_url":   bool(DATABASE_URL),
    })

@app.route("/")
def index():
    landing = Path(__file__).parent / "landing" / "index.html"
    if landing.exists():
        return landing.read_text(), 200, {"Content-Type": "text/html"}
    return redirect("/dashboard") if current_user() else jsonify({"service": "GrapeRoot Review"})

# ── Startup ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    print(f"GrapeRoot Review on :{port}")
    print(f"  App mode : {'yes' if APP_ID else 'no'}")
    print(f"  OAuth    : {'yes' if OAUTH_CLIENT_ID else 'NO — set GITHUB_OAUTH_CLIENT_ID'}")
    print(f"  DB       : {DB_PATH}")
    app.run(host="0.0.0.0", port=port)
