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

import hashlib, hmac, json, os, subprocess, sys, time, secrets, urllib.request, urllib.parse
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
DATABASE_URL    = os.environ.get("DATABASE_URL", "")          # NeonDB — leaderboard / reviews
PRO_DATABASE_URL = os.environ.get("PRO_DATABASE_URL", "")    # NeonDB — Pro telemetry (usage_events)
DB_PATH         = os.environ.get("DB_PATH", "/app/data/reviews.db")  # SQLite fallback

app = Flask(__name__)
app.secret_key = SESSION_SECRET

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "https://graperoot-review-production.up.railway.app")

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

# ── Leaderboard DB schema (reviews + users only) ──────────────────────────────
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
    installed_by TEXT
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
    installed_by TEXT
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

# ── Pro telemetry DB schema (usage_events only) ────────────────────────────────
PRO_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                  SERIAL PRIMARY KEY,
    license_key         TEXT NOT NULL,
    session_id          TEXT,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model               TEXT,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,
    total_cost_usd      REAL DEFAULT 0,
    naive_cost_usd      REAL DEFAULT 0,
    savings_pct         REAL DEFAULT 0,
    tokens_served       INTEGER DEFAULT 0,
    tokens_avoided      INTEGER DEFAULT 0,
    tool_hits           TEXT,
    task_type           TEXT,
    confidence          TEXT,
    project_hash        TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_license ON usage_events(license_key);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON usage_events(timestamp DESC);
"""

PRO_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    license_key         TEXT NOT NULL,
    session_id          TEXT,
    timestamp           TEXT NOT NULL,
    model               TEXT,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,
    total_cost_usd      REAL DEFAULT 0,
    naive_cost_usd      REAL DEFAULT 0,
    savings_pct         REAL DEFAULT 0,
    tokens_served       INTEGER DEFAULT 0,
    tokens_avoided      INTEGER DEFAULT 0,
    tool_hits           TEXT,
    task_type           TEXT,
    confidence          TEXT,
    project_hash        TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_license ON usage_events(license_key);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON usage_events(timestamp DESC);
"""


def _pg_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _pro_pg_conn():
    """Connect to the Pro telemetry database (PRO_DATABASE_URL)."""
    url = PRO_DATABASE_URL or DATABASE_URL  # fall back to main DB if not set
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


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
    # Leaderboard DB (reviews + users)
    if DATABASE_URL and _HAS_PG:
        con = _pg_conn()
        cur = con.cursor()
        cur.execute(SCHEMA)
        con.commit()
        cur.close(); con.close()
        print("[db] PostgreSQL leaderboard DB initialized", flush=True)
    else:
        import sqlite3 as _sq
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = _sq.connect(DB_PATH)
        con.executescript(SCHEMA_SQLITE)
        con.commit(); con.close()
        print(f"[db] SQLite fallback at {DB_PATH}", flush=True)

    # Pro telemetry DB (usage_events) — separate database
    if _pro_is_pg():
        con = _pro_pg_conn()
        cur = con.cursor()
        cur.execute(PRO_SCHEMA)
        con.commit()
        cur.close(); con.close()
        db_label = "PRO_DATABASE_URL" if PRO_DATABASE_URL else "DATABASE_URL (shared)"
        print(f"[db] PostgreSQL Pro telemetry DB initialized ({db_label})", flush=True)
    else:
        import sqlite3 as _sq
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = _sq.connect(DB_PATH)
        con.executescript(PRO_SCHEMA_SQLITE)
        con.commit(); con.close()

    _migrate_db()


def _migrate_db():
    """Add columns introduced after initial schema deploy."""
    # Leaderboard DB migrations (users table only)
    leaderboard_cols_pg = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_limit INTEGER DEFAULT 5",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'",
    ]
    leaderboard_cols_sq = [
        "ALTER TABLE users ADD COLUMN session_token TEXT",
        "ALTER TABLE users ADD COLUMN monthly_limit INTEGER DEFAULT 5",
        "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'",
    ]
    if DATABASE_URL and _HAS_PG:
        con = _pg_conn()
        cur = con.cursor()
        for sql in leaderboard_cols_pg:
            try: cur.execute(sql)
            except Exception: pass
        con.commit(); cur.close(); con.close()
    else:
        import sqlite3 as _sq
        con = _sq.connect(DB_PATH)
        for sql in leaderboard_cols_sq:
            try: con.execute(sql)
            except Exception: pass
        con.commit(); con.close()

    # Pro telemetry DB migrations (usage_events table)
    pro_cols_pg = [
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tokens_served INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tokens_avoided INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tool_hits TEXT",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS device_host TEXT DEFAULT 'unknown'",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tokens_avoided_tar INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tokens_avoided_cross_turn INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS shadow_tokens_avoided INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS graperoot_overhead_tokens INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS token_savings (
            id             SERIAL PRIMARY KEY,
            license_key    TEXT NOT NULL,
            date           DATE NOT NULL,
            tokens_saved   BIGINT DEFAULT 0,
            requests_count INT DEFAULT 0,
            device_host    TEXT DEFAULT 'unknown',
            CONSTRAINT uq_ts_key_date_host UNIQUE (license_key, date, device_host)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ts_license_date ON token_savings(license_key, date)",
    ]
    pro_cols_sq = [
        "ALTER TABLE usage_events ADD COLUMN tokens_served INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN tokens_avoided INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN tool_hits TEXT",
        "ALTER TABLE usage_events ADD COLUMN device_host TEXT DEFAULT 'unknown'",
        "ALTER TABLE usage_events ADD COLUMN tokens_avoided_tar INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN tokens_avoided_cross_turn INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN shadow_tokens_avoided INTEGER DEFAULT 0",
        "ALTER TABLE usage_events ADD COLUMN graperoot_overhead_tokens INTEGER DEFAULT 0",
    ]
    if _pro_is_pg():
        con = _pro_pg_conn()
        cur = con.cursor()
        for sql in pro_cols_pg:
            try: cur.execute(sql)
            except Exception: pass
        con.commit(); cur.close(); con.close()
    else:
        import sqlite3 as _sq
        con = _sq.connect(DB_PATH)
        for sql in pro_cols_sq:
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


def _query(sql: str, params=(), one=False):
    """Run a SELECT and return list of dicts (or one dict)."""
    db   = get_db()
    is_pg = getattr(g, "_is_pg", False)
    if is_pg:
        cur = db.cursor()
        cur.execute(sql, params)
        rows = [dict(r) for r in (cur.fetchall() if not one else [cur.fetchone()])]
        cur.close()
    else:
        cur = db.execute(sql, params)
        raw = cur.fetchall()
        rows = [dict(r) for r in raw]
        if one:
            rows = [rows[0]] if rows else [{}]
    return rows[0] if one else rows


def _query_write(sql: str, params=()):
    """Run an INSERT/UPDATE outside request context (usable from background threads)."""
    if DATABASE_URL and _HAS_PG:
        con = _pg_conn()
        cur = con.cursor()
        cur.execute(sql, params)
        con.commit()
        cur.close(); con.close()
    else:
        import sqlite3 as _sq
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = _sq.connect(DB_PATH)
        con.execute(sql, params)
        con.commit(); con.close()


# ── Pro telemetry DB helpers ───────────────────────────────────────────────────

def _pro_is_pg() -> bool:
    return _HAS_PG and bool(PRO_DATABASE_URL or DATABASE_URL)


def _pro_ph(n: int) -> str:
    return ",".join(["%s"] * n) if _pro_is_pg() else ",".join(["?"] * n)


def _pro_query(sql: str, params=(), one=False):
    """SELECT from the Pro telemetry database."""
    if _pro_is_pg():
        con = _pro_pg_conn()
        cur = con.cursor()
        cur.execute(sql, params)
        rows = [dict(r) for r in (cur.fetchall() if not one else [cur.fetchone()])]
        cur.close(); con.close()
    else:
        import sqlite3 as _sq
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = _sq.connect(DB_PATH)
        con.row_factory = _sq.Row
        cur = con.execute(sql, params)
        raw = cur.fetchall()
        rows = [dict(r) for r in raw]
        if one:
            rows = [rows[0]] if rows else [{}]
        con.close()
    return rows[0] if one else rows


def _pro_query_write(sql: str, params=()):
    """INSERT/UPDATE into the Pro telemetry database."""
    if _pro_is_pg():
        con = _pro_pg_conn()
        cur = con.cursor()
        cur.execute(sql, params)
        con.commit()
        cur.close(); con.close()
    else:
        import sqlite3 as _sq
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = _sq.connect(DB_PATH)
        con.execute(sql, params)
        con.commit(); con.close()


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


def _run_review(owner, repo, pr_num, head_sha, github_token, pr_title, pr_url, installed_by):
    pr_url_gh = pr_url or f"https://github.com/{owner}/{repo}/pull/{pr_num}"
    print(f"[review] {owner}/{repo}#{pr_num}", flush=True)
    t0 = time.time()

    # ── Graph context (self-hosted only — hosted service uses diff only) ────────
    # On the hosted service we never clone repos. Code stays on GitHub.
    # Full graph analysis is available for self-hosted deployments (BYOK + Docker).
    graph_available = False
    graph_ctx = ""
    if _HAS_GRAPH_SVC and os.environ.get("ENABLE_GRAPH_CLONE") == "1":
        # Only enabled when operator explicitly opts in (self-hosted deployments)
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
                    diff_text = r.read().decode("utf-8", errors="replace")
                import re as _re
                changed_files = _re.findall(r"diff --git a/.+ b/(.+)", diff_text)
                impact = graph_impact(owner, repo, changed_files)
                if impact.get("ok"):
                    graph_ctx = f"### Blast Radius\n{impact.get('summary','')}\n"
                    for ref in impact.get("recommended_reads", changed_files[:3])[:4]:
                        text = graph_read_symbol(owner, repo, ref)
                        if text:
                            graph_ctx += f"\n### {ref}\n{text[:600]}"
            except Exception as e:
                print(f"[graph] context failed: {e}", flush=True)

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

    if result.returncode != 0:
        error = result.stderr[:500] or f"exit code {result.returncode}"
        print(f"[review] FAILED {owner}/{repo}#{pr_num}: {error[:100]}", flush=True)
    else:
        try:
            out      = json.loads(Path(json_out).read_text())
            findings = out.get("findings", [])
            cost     = out.get("cost_usd", 0)
        except Exception:
            pass
        finally:
            try: Path(json_out).unlink()
            except Exception: pass

    with app.app_context():
        try:
            save_review(owner, repo, pr_num, pr_title, pr_url_gh, head_sha,
                        elapsed, cost, findings, error, installed_by)
        except Exception as e:
            print(f"[db] save failed: {e}", flush=True)

    print(f"[review] done {owner}/{repo}#{pr_num} — {len(findings)} findings "
          f"{'(graph)' if graph_available else '(diff-only)'} "
          f"${cost:.4f} {elapsed:.0f}s", flush=True)

# ── OAuth helpers ──────────────────────────────────────────────────────────────

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
    row = _query(f"SELECT * FROM users WHERE session_token = {ph}", (token,), one=True)
    return row if row.get("github_id") else None


def _within_limit(login: str) -> bool:
    """Return True if this GitHub login hasn't hit their monthly review limit."""
    if not login:
        return True
    is_pg = bool(DATABASE_URL and _HAS_PG)
    ph = "%s" if is_pg else "?"
    user_row = _query(f"SELECT monthly_limit FROM users WHERE login = {ph}", (login,), one=True)
    limit = user_row.get("monthly_limit") if user_row.get("github_id") else 5
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
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code or state != session.pop("oauth_state", None):
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
    limit   = user_row.get("monthly_limit", 5) if user_row.get("github_id") else 5

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
                   CASE WHEN error IS NOT NULL THEN 'failed' ELSE 'completed' END AS status,
                   created_at
            FROM reviews WHERE installed_by = {ph}
            ORDER BY created_at DESC LIMIT 50""",
        (login,),
    )
    return jsonify(rows)


@app.route("/api/graph/<owner>/<repo>")
@login_required
def api_graph_status(owner, repo):
    if _HAS_GRAPH_SVC:
        return jsonify(graph_summary(owner, repo))
    return jsonify({"exists": False, "reason": "graph_service_unavailable"})

# ── Usage telemetry ────────────────────────────────────────────────────────────

def _calc_cost(model: str, inp: int, out: int, cr: int, cw: int,
               tokens_avoided: int = 0, tokens_avoided_compounding: int = 0):
    """Return (total_cost_usd, naive_cost_usd, savings_pct) for the given token counts.

    tokens_avoided: one-time tokens never sent to context (priced at input_price)
    tokens_avoided_compounding: cache re-bill savings on subsequent turns (priced at cache_read_price)

    The formula: vanilla_cost = actual_cost + (avoided × input) + (compounding × cache_read)
    This measures what the user WOULD have paid without GrapeRoot's excerpting.
    """
    m = (model or "").lower()
    if "haiku" in m:
        pr = (1.0,  1.25,  0.1,  5.0)   # Haiku 4.5
    elif "fable" in m:
        pr = (10.0, 12.5,  1.0,  50.0)  # Fable 5
    elif "opus" in m and ("4-8" in m or "4-7" in m):
        pr = (5.0,  6.25,  0.5,  25.0)  # Opus 4.8 / 4.7
    elif "opus" in m:
        pr = (15.0, 18.75, 1.5,  75.0)  # Opus 4 / 4.1
    else:
        pr = (3.0,  3.75,  0.3,  15.0)  # Sonnet 4.x / default
    tc = (inp * pr[0] + cw * pr[1] + cr * pr[2] + out * pr[3]) / 1e6
    if tokens_avoided > 0 or tokens_avoided_compounding > 0:
        one_time_cost = tokens_avoided * pr[0] / 1e6
        compounding_cost = tokens_avoided_compounding * pr[2] / 1e6
        nc = tc + one_time_cost + compounding_cost
    else:
        nc = ((inp + cw + cr) * pr[0] + out * pr[3]) / 1e6
    sp = (nc - tc) / nc if nc > 0 else 0.0
    return round(tc, 6), round(nc, 6), round(sp, 4)


@app.route("/api/usage", methods=["POST"])
def api_usage_ingest():
    data = request.get_json(silent=True) or {}
    key  = data.get("license_key", "")
    if not key or not key.startswith("GRP-"):
        return jsonify({"error": "invalid license_key"}), 400

    inp = int(data.get("input_tokens", 0))
    out = int(data.get("output_tokens", 0))
    cr  = int(data.get("cache_read_tokens", 0))
    cw  = int(data.get("cache_write_tokens", 0))
    model = data.get("model") or ""
    tokens_avoided = int(data.get("tokens_avoided", 0))
    tokens_avoided_compounding = int(data.get("tokens_avoided_compounding", 0))
    tokens_avoided_tar = int(data.get("tokens_avoided_tar", 0))
    tokens_avoided_cross_turn = int(data.get("tokens_avoided_cross_turn", 0))
    shadow_tokens_avoided = int(data.get("shadow_tokens_avoided", 0))
    graperoot_overhead_tokens = int(data.get("graperoot_overhead_tokens", 0))

    tc, nc, sp = _calc_cost(model, inp, out, cr, cw, tokens_avoided, tokens_avoided_compounding)

    device_host = (data.get("device_host") or "unknown")[:128]
    ph = _pro_ph(22)
    ts = data.get("timestamp") or datetime.now(timezone.utc).isoformat()
    _pro_query_write(
        f"INSERT INTO usage_events (license_key, session_id, timestamp, model, "
        f"input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        f"total_cost_usd, naive_cost_usd, savings_pct, "
        f"tokens_served, tokens_avoided, tool_hits, "
        f"task_type, confidence, project_hash, device_host, "
        f"tokens_avoided_tar, tokens_avoided_cross_turn, shadow_tokens_avoided, graperoot_overhead_tokens) "
        f"VALUES ({ph})",
        (key, data.get("session_id"), ts, model,
         inp, out, cr, cw,
         tc, nc, sp,
         int(data.get("tokens_served", inp + cw + cr + out)),
         tokens_avoided + tokens_avoided_compounding,
         data.get("tool_hits"),
         data.get("task_type"), data.get("confidence"), data.get("project_hash"),
         device_host,
         tokens_avoided_tar, tokens_avoided_cross_turn, shadow_tokens_avoided, graperoot_overhead_tokens)
    )

    # Write to token_savings for the Pro dashboard savings chart
    total_saved = tokens_avoided + tokens_avoided_compounding
    if total_saved > 0 or cr > 0:
        ts_date = ts[:10]  # YYYY-MM-DD
        saved_count = total_saved if total_saved > 0 else cr
        _pro_query_write(
            f"INSERT INTO token_savings (license_key, date, tokens_saved, requests_count, device_host) "
            f"VALUES ({_pro_ph(5)}) "
            f"ON CONFLICT (license_key, date, device_host) DO UPDATE SET "
            f"tokens_saved = token_savings.tokens_saved + EXCLUDED.tokens_saved, "
            f"requests_count = token_savings.requests_count + 1",
            (key, ts_date, saved_count, 1, device_host)
        )

    return jsonify({"ok": True})


@app.route("/api/usage/stats")
def api_usage_stats():
    key   = request.args.get("license_key", "")
    month = request.args.get("month", datetime.now(timezone.utc).strftime("%Y-%m"))
    if not key or not key.startswith("GRP-"):
        return jsonify({"error": "invalid license_key"}), 400

    is_pg = _pro_is_pg()
    ph1   = "%s" if is_pg else "?"

    # Date window
    start = f"{month}-01"
    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    if mon == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{mon + 1:02d}-01"

    window = f"timestamp >= {ph1} AND timestamp < {ph1}"

    summary_row = _pro_query(
        f"SELECT COUNT(*) AS turns, "
        f"SUM(input_tokens) AS total_input, SUM(output_tokens) AS total_output, "
        f"SUM(cache_read_tokens) AS total_cache_read, SUM(cache_write_tokens) AS total_cache_write, "
        f"SUM(total_cost_usd) AS total_cost, SUM(naive_cost_usd) AS total_naive, "
        f"AVG(savings_pct) AS avg_savings, "
        f"SUM(tokens_served) AS total_tokens_served, SUM(tokens_avoided) AS total_tokens_avoided, "
        f"SUM(tokens_avoided_tar) AS total_avoided_tar, "
        f"SUM(tokens_avoided_cross_turn) AS total_avoided_cross_turn, "
        f"SUM(shadow_tokens_avoided) AS total_shadow_avoided, "
        f"SUM(graperoot_overhead_tokens) AS total_overhead "
        f"FROM usage_events WHERE license_key = {ph1} AND {window}",
        (key, start, end), one=True
    )

    by_day = _pro_query(
        f"SELECT "
        f"{'DATE(timestamp)' if is_pg else 'substr(timestamp,1,10)'} AS date, "
        f"COUNT(*) AS turns, SUM(input_tokens) AS input_tokens, "
        f"SUM(cache_read_tokens) AS cache_read_tokens, "
        f"SUM(total_cost_usd) AS total_cost_usd, SUM(naive_cost_usd) AS naive_cost_usd, "
        f"AVG(savings_pct) AS savings_pct, "
        f"SUM(tokens_served) AS tokens_served, SUM(tokens_avoided) AS tokens_avoided, "
        f"SUM(tokens_avoided_tar) AS tokens_avoided_tar, "
        f"SUM(tokens_avoided_cross_turn) AS tokens_avoided_cross_turn, "
        f"SUM(shadow_tokens_avoided) AS shadow_tokens_avoided "
        f"FROM usage_events WHERE license_key = {ph1} AND {window} "
        f"GROUP BY {'DATE(timestamp)' if is_pg else 'substr(timestamp,1,10)'} "
        f"ORDER BY date DESC",
        (key, start, end)
    )

    recent = _pro_query(
        f"SELECT id, timestamp, model, input_tokens, output_tokens, "
        f"cache_read_tokens, cache_write_tokens, total_cost_usd, naive_cost_usd, savings_pct, "
        f"tokens_served, tokens_avoided, tokens_avoided_tar, tokens_avoided_cross_turn, "
        f"shadow_tokens_avoided, graperoot_overhead_tokens, tool_hits, "
        f"task_type, confidence, project_hash, session_id "
        f"FROM usage_events WHERE license_key = {ph1} AND {window} "
        f"ORDER BY timestamp DESC LIMIT 50",
        (key, start, end)
    )

    total_cost  = float(summary_row.get("total_cost")  or 0)
    total_naive = float(summary_row.get("total_naive") or 0)
    total_served  = int(summary_row.get("total_tokens_served")  or 0)
    total_avoided = int(summary_row.get("total_tokens_avoided") or 0)
    total_avoided_tar         = int(summary_row.get("total_avoided_tar") or 0)
    total_avoided_cross_turn  = int(summary_row.get("total_avoided_cross_turn") or 0)
    total_shadow_avoided      = int(summary_row.get("total_shadow_avoided") or 0)
    total_overhead            = int(summary_row.get("total_overhead") or 0)
    return jsonify({
        "license_key": key,
        "month": month,
        "summary": {
            "total_turns":              summary_row.get("turns") or 0,
            "total_input_tokens":       summary_row.get("total_input") or 0,
            "total_output_tokens":      summary_row.get("total_output") or 0,
            "total_cache_read_tokens":  summary_row.get("total_cache_read") or 0,
            "total_cache_write_tokens": summary_row.get("total_cache_write") or 0,
            "total_cost_usd":           round(total_cost, 6),
            "total_naive_cost_usd":     round(total_naive, 6),
            "total_saved_usd":          round(total_naive - total_cost, 6),
            "avg_savings_pct":          round(float(summary_row.get("avg_savings") or 0) * 100, 2),
            "total_tokens_served":      total_served,
            "total_tokens_avoided":     total_avoided,
            "tool_savings_pct":         round(total_avoided / (total_served + total_avoided) * 100, 2)
                                        if (total_served + total_avoided) > 0 else 0,
            "tokens_avoided_tar":         total_avoided_tar,
            "tokens_avoided_cross_turn":  total_avoided_cross_turn,
            "shadow_tokens_avoided":      total_shadow_avoided,
            "graperoot_overhead_tokens":  total_overhead,
        },
        "by_day":  by_day,
        "recent":  recent,
    })


@app.route("/api/usage/export")
def api_usage_export():
    key   = request.args.get("license_key", "")
    month = request.args.get("month", datetime.now(timezone.utc).strftime("%Y-%m"))
    if not key or not key.startswith("GRP-"):
        return jsonify({"error": "invalid license_key"}), 400

    ph1   = "%s" if _pro_is_pg() else "?"
    start = f"{month}-01"
    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    end   = f"{year + 1}-01-01" if mon == 12 else f"{year}-{mon + 1:02d}-01"

    rows = _pro_query(
        f"SELECT timestamp, model, input_tokens, output_tokens, cache_read_tokens, "
        f"cache_write_tokens, total_cost_usd, savings_pct, "
        f"tokens_served, tokens_avoided, task_type, confidence, session_id "
        f"FROM usage_events WHERE license_key = {ph1} AND timestamp >= {ph1} AND timestamp < {ph1} "
        f"ORDER BY timestamp DESC",
        (key, start, end)
    )

    def _csv():
        cols = ["timestamp", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "total_cost_usd",
                "savings_pct", "tokens_served", "tokens_avoided",
                "task_type", "confidence", "session_id"]
        yield ",".join(cols) + "\n"
        for r in rows:
            yield ",".join(str(r.get(c, "")) for c in cols) + "\n"

    return app.response_class(
        _csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=graperoot-usage-{month}.csv"}
    )


@app.route("/api/usage/savings-chart")
def api_savings_chart():
    """Daily savings chart data from token_savings table.

    Returns last N days of tokens_saved and requests_count aggregated per day.
    Used for the savings trend chart on the Pro dashboard.
    """
    key  = request.args.get("license_key", "")
    days = min(int(request.args.get("days", 30)), 90)
    if not key or not key.startswith("GRP-"):
        return jsonify({"error": "invalid license_key"}), 400

    is_pg = _pro_is_pg()
    ph1   = "%s" if is_pg else "?"

    if is_pg:
        rows = _pro_query(
            f"SELECT date::text AS date, "
            f"SUM(tokens_saved) AS tokens_saved, SUM(requests_count) AS requests_count "
            f"FROM token_savings WHERE license_key = {ph1} "
            f"AND date >= CURRENT_DATE - INTERVAL '{days} days' "
            f"GROUP BY date ORDER BY date ASC",
            (key,)
        )
    else:
        rows = _pro_query(
            f"SELECT date, SUM(tokens_saved) AS tokens_saved, SUM(requests_count) AS requests_count "
            f"FROM token_savings WHERE license_key = {ph1} "
            f"AND date >= date('now', '-{days} days') "
            f"GROUP BY date ORDER BY date ASC",
            (key,)
        )

    total_tokens_saved = sum(int(r.get("tokens_saved") or 0) for r in rows)
    total_requests     = sum(int(r.get("requests_count") or 0) for r in rows)

    # Estimate USD saved: tokens_saved are vanilla-equivalent tokens at $15/M (Opus rate)
    # Frontend can override with user's actual model price if known
    tokens_saved_usd = round(total_tokens_saved * 15.0 / 1_000_000, 4)

    return jsonify({
        "license_key":       key,
        "days":              days,
        "total_tokens_saved": total_tokens_saved,
        "total_requests":    total_requests,
        "estimated_saved_usd": tokens_saved_usd,
        "chart": [
            {
                "date":           r["date"],
                "tokens_saved":   int(r.get("tokens_saved") or 0),
                "requests_count": int(r.get("requests_count") or 0),
            }
            for r in rows
        ],
    })


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
