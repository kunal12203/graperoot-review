"""
HTTP route/endpoint extractor for GrapeRoot Pro — Phase 2.

Supports 28 framework patterns across TypeScript/JavaScript, Python, Go, Rust, PHP,
Kotlin, and Ruby:

  TS/JS  : Express, Fastify, Hono, NestJS, tRPC, Next.js App Router,
            Next.js Pages Router, Remix, SvelteKit, Nuxt, Koa
  Python : FastAPI, Flask, Django
  Go     : Gin, Gorilla Mux, Echo, Chi, Fiber, Beego
  Rust   : Axum, Actix, Rocket
  PHP    : Laravel
  Kotlin : Ktor
  Java   : Quarkus / JAX-RS (Jersey, Micronaut)
  Ruby   : Sinatra, Grape

Entry points
------------
  extract_routes_for_file(content, file_path, ext) -> list[dict]
      Main dispatcher — runs all applicable extractors for the extension,
      merges and deduplicates results.

  extract_file_based_route(file_path, root_path) -> list[dict]
      File-path inference for Next.js / Remix / SvelteKit / Nuxt.

Symbol dict format (standard GrapeRoot schema, symbol_type always "api_route"):
  {
      "id":           "<file_path>::<METHOD> <path>",
      "name":         "<METHOD> <path>",
      "symbol_type":  "api_route",
      "line_start":   int,   # 0-indexed
      "line_end":     int,   # 0-indexed
      "body_hash":    str,   # md5[:8] of lines[start:end+1]
      "confidence":   "high"|"medium"|"low",
      "exported":     True,
      "keywords":     list[str],
      "route_method": str,   # "GET", "POST", "PUT", "DELETE", "PATCH", "ANY", …
      "route_path":   str,   # normalised path e.g. "/users/:id"
  }
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# EXTENSION REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

ROUTE_EXTS: set[str] = {
    # TypeScript / JavaScript
    ".ts", ".tsx", ".js", ".jsx", ".mjs",
    # Python
    ".py",
    # Go
    ".go",
    # Rust
    ".rs",
    # PHP
    ".php",
    # Kotlin
    ".kt",
    # Java
    ".java",
    # Ruby
    ".rb",
    # GraphQL
    ".graphql", ".gql",
    # Scala (Play Framework)
    ".scala",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _body_hash(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start : end + 1])
    return hashlib.md5(body.encode()).hexdigest()[:8]


def _normalize_path(path: str) -> str:
    """Normalise path parameter syntax to Express-style :param.

    Converts:
      {id}    -> :id     (FastAPI, Django, Laravel, Axum)
      [id]    -> :id     (Next.js)
      (id)    -> :id     (Remix optional segments)
      <id>    -> :id     (some custom patterns)
      [[id]]  -> :id?    (Next.js optional catch-all, simplified)
      [...id] -> :id*    (Next.js catch-all)
      [id]    -> :id     (standard bracket)
    """
    # Next.js optional catch-all [[...slug]]
    path = re.sub(r"\[\[\.\.\.(\w+)\]\]", r":\1*", path)
    # Next.js catch-all [...slug]
    path = re.sub(r"\[\.\.\.(\w+)\]", r":\1*", path)
    # Standard bracket params [id]
    path = re.sub(r"\[(\w+)\]", r":\1", path)
    # Curly brace params {id}
    path = re.sub(r"\{(\w+)\}", r":\1", path)
    # Angle bracket params — Flask/Werkzeug: <int:id> -> :id, <id> -> :id
    # Capture type converter if present (group after colon is the real param name)
    path = re.sub(r"<(?:\w+:)(\w+)>", r":\1", path)   # <int:id> -> :id
    path = re.sub(r"<(\w+)>", r":\1", path)            # <id> -> :id
    # Remix/React Router optional segments (id)  — only if surrounded by /
    path = re.sub(r"/\((\w+)\)(?=/|$)", r"/:\1", path)
    # Collapse double slashes
    path = re.sub(r"/{2,}", "/", path)
    # Remove trailing slash unless it is the root
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _path_keywords(method: str, path: str, handler: str) -> list[str]:
    """Extract search-relevant keywords from method, path, and handler name."""
    seen: set[str] = set()
    out: list[str] = []

    def add(w: str) -> None:
        w = w.lower().strip("_:/")
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            out.append(w)

    add(method.lower())

    # Path segments (skip :param tokens, keep literal words)
    for seg in path.split("/"):
        seg = seg.lstrip(":")
        if seg and not seg.startswith(":"):
            add(seg)

    # Handler name — split camelCase and underscores
    if handler:
        add(handler)
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", handler)
        parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
        for p in parts.split():
            add(p)
        for p in handler.split("_"):
            add(p)

    return out[:12]


def _find_block_end_brace(lines: list[str], start: int, limit: int = 100) -> int:
    """Return the line index where the block that opens at/after `start` closes.

    Uses simple brace counting (no comment/string awareness — fast enough for
    the patterns we match).
    """
    depth = 0
    found_open = False
    end = min(start + limit, len(lines))
    for i in range(start, end):
        line = lines[i]
        opens  = line.count("{") - line.count("\\{")
        closes = line.count("}") - line.count("\\}")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + limit - 1, len(lines) - 1)


def _find_block_end_paren(lines: list[str], start: int, limit: int = 60) -> int:
    """Return line index where the parenthesised call that opens at `start` closes."""
    depth = 0
    found_open = False
    end = min(start + limit, len(lines))
    for i in range(start, end):
        line = lines[i]
        opens  = line.count("(")
        closes = line.count(")")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + limit - 1, len(lines) - 1)


def _make_route(
    file_path: str,
    lines: list[str],
    line_start: int,
    line_end: int,
    method: str,
    path: str,
    handler: str = "",
    confidence: str = "high",
) -> dict:
    method = method.upper()
    route_path = _normalize_path(path)
    name = f"{method} {route_path}"
    return {
        "id":           f"{file_path}::{name}",
        "name":         name,
        "symbol_type":  "api_route",
        "line_start":   line_start,
        "line_end":     line_end,
        "body_hash":    _body_hash(lines, line_start, line_end),
        "confidence":   confidence,
        "exported":     True,
        "keywords":     _path_keywords(method, route_path, handler),
        "route_method": method,
        "route_path":   route_path,
    }


def _dedup_routes(routes: list[dict]) -> list[dict]:
    """Remove duplicates.

    Two routes are considered the same if they share the same normalised
    (method, path) name AND the same line_start.  This prevents multiple
    framework extractors from emitting the same hit for patterns like
    ``app.get('/path', handler)`` that match both Express and Fastify regexes.
    """
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    for r in routes:
        key = (r["name"], r["line_start"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# COMPILED REGEXES — module-level for performance
# ═══════════════════════════════════════════════════════════════════════════════

# ── Express / Fastify / Hono ──────────────────────────────────────────────────

# app.get('/path', ...) / router.post('/path', ...) / server.get(...)
# Note: 'all' is intentionally absent here — covered by _EXPRESS_ALL_RE below.
# Receivers include common Express, Fastify, and Hono variable names.
_EXPRESS_VERB_RE = re.compile(
    r"""(?:^|[^\w])                    # not inside a longer identifier
    (?:app|router|server|api|r|v\d+|fastify|instance|hono)\s*\.\s*
    (get|post|put|patch|delete|del|head|options)\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)

# app.all('/path', ...) — Express/Hono/Fastify any-method handler
_EXPRESS_ALL_RE = re.compile(
    r"""(?:^|[^\w])
    (?:app|router|server|api|r|v\d+|fastify|instance|hono)\s*\.\s*all\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)

# app.use('/prefix', router) — mount middleware/routers
_EXPRESS_USE_RE = re.compile(
    r"""(?:app|router|server|api)\s*\.\s*use\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)

# fastify.route({ method: 'GET', url: '/path', ... })
_FASTIFY_ROUTE_OBJ_RE = re.compile(
    r"""(?:fastify|server|app)\s*\.\s*route\s*\(\s*\{""",
    re.MULTILINE | re.VERBOSE,
)
_FASTIFY_METHOD_RE = re.compile(
    r"""method\s*:\s*['"`]([A-Z]+)['"`]""",
)
_FASTIFY_URL_RE = re.compile(
    r"""url\s*:\s*['"`]([^'"`]+)['"`]""",
)

# Hono app.all('/path', ...)
_HONO_ALL_RE = re.compile(
    r"""(?:app|hono)\s*\.\s*all\s*\(\s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)

# ── NestJS decorators ─────────────────────────────────────────────────────────

_NEST_CONTROLLER_RE = re.compile(
    r"""@Controller\s*\(\s*(?:['"`]([^'"`]*)['"`])?\s*\)""",
    re.MULTILINE,
)
_NEST_METHOD_RE = re.compile(
    r"""@(Get|Post|Put|Delete|Patch|Options|Head|All)\s*\(\s*(?:['"`]([^'"`]*)['"`])?\s*\)\s*\n
    (?:[ \t]*(?:@\w+[^)]*\)\s*\n))*   # optional other decorators
    [ \t]*(?:async\s+)?(\w+)\s*\(""",
    re.MULTILINE | re.VERBOSE,
)

# ── tRPC ──────────────────────────────────────────────────────────────────────

# old-style router.query('name', ...) / router.mutation('name', ...)
_TRPC_OLD_RE = re.compile(
    r"""(?:router|t\.router)\s*\.\s*(query|mutation|subscription)\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)

# new-style t.router({ name: t.procedure.query(...) })
_TRPC_PROC_RE = re.compile(
    r"""(\w+)\s*:\s*(?:\w+\.)*(?:procedure)\s*\.\s*(query|mutation|subscription)""",
    re.MULTILINE,
)

# ── SvelteKit exported HTTP handlers ─────────────────────────────────────────

_SVELTE_EXPORT_RE = re.compile(
    r"""^export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b""",
    re.MULTILINE,
)

# ── FastAPI ───────────────────────────────────────────────────────────────────

_FASTAPI_VERB_RE = re.compile(
    r"""@(?:app|router|api_router)\s*\.\s*
    (get|post|put|patch|delete|head|options)\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)
_FASTAPI_API_ROUTE_RE = re.compile(
    r"""@(?:app|router|api_router)\s*\.\s*api_route\s*\(
    \s*['"`]([^'"`]+)['"`]\s*,\s*methods\s*=\s*\[([^\]]+)\]""",
    re.MULTILINE | re.VERBOSE,
)

# ── Flask ─────────────────────────────────────────────────────────────────────

_FLASK_ROUTE_RE = re.compile(
    r"""@(?:app|bp|blueprint|[\w]+)\s*\.\s*route\s*\(
    \s*['"`]([^'"`]+)['"`]
    (?:[^)]*?methods\s*=\s*\[([^\]]+)\])?""",
    re.MULTILINE | re.VERBOSE,
)

# ── Django urls ───────────────────────────────────────────────────────────────

_DJANGO_PATH_RE = re.compile(
    r"""(?:^|\s)(?:path|re_path|url)\s*\(\s*[rR]?['"`]([^'"`]+)['"`]\s*,\s*(\w[\w.]*)""",
    re.MULTILINE,
)

# ── Gin (Go) ─────────────────────────────────────────────────────────────────

_GIN_VERB_RE = re.compile(
    r"""(?:r|router|g|v\d+|engine|e)\s*\.\s*
    (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any)\s*\(
    \s*"([^"]+)"""  ,
    re.MULTILINE | re.VERBOSE,
)
_GIN_GROUP_RE = re.compile(
    r"""(?:r|router|g|v\d+|engine)\s*\.\s*Group\s*\(\s*"([^"]+)"\s*\)""",
    re.MULTILINE,
)

# ── Gorilla Mux (Go) ─────────────────────────────────────────────────────────

_GORILLA_HANDLEFUNC_RE = re.compile(
    r"""(?:r|router|mux)\s*\.\s*HandleFunc\s*\(
    \s*"([^"]+)"\s*,\s*(\w+)""",
    re.MULTILINE | re.VERBOSE,
)
_GORILLA_METHODS_RE = re.compile(
    r"""\.Methods\s*\(\s*"([A-Z]+(?:"\s*,\s*"[A-Z]+)*)"\s*\)""",
)
_GORILLA_PREFIX_RE = re.compile(
    r"""(?:r|router|mux)\s*\.\s*PathPrefix\s*\(\s*"([^"]+)"\s*\)""",
    re.MULTILINE,
)

# ── Echo (Go) ────────────────────────────────────────────────────────────────

_ECHO_VERB_RE = re.compile(
    r"""(?:e|echo|g|grp|v\d+)\s*\.\s*
    (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any)\s*\(
    \s*"([^"]+)"""  ,
    re.MULTILINE | re.VERBOSE,
)

# ── Chi (Go) ─────────────────────────────────────────────────────────────────

_CHI_VERB_RE = re.compile(
    r"""r\s*\.\s*(Get|Post|Put|Patch|Delete|Head|Options|Handle)\s*\(
    \s*"([^"]+)"""  ,
    re.MULTILINE | re.VERBOSE,
)
_CHI_ROUTE_RE = re.compile(
    r"""r\s*\.\s*Route\s*\(\s*"([^"]+)"\s*,""",
    re.MULTILINE,
)

# ── Axum (Rust) ──────────────────────────────────────────────────────────────

# Captures path and start of handler expr; full handler is extracted by _extract_axum
_AXUM_ROUTE_RE = re.compile(
    r"""\.route\s*\(\s*"([^"]+)"\s*,\s*""",
    re.MULTILINE,
)
_AXUM_METHOD_RE = re.compile(
    r"""\b(get|post|put|patch|delete|head|options|any)\s*\(""",
)

# ── Actix (Rust) ─────────────────────────────────────────────────────────────

_ACTIX_RESOURCE_RE = re.compile(
    r"""web::resource\s*\(\s*"([^"]+)"\s*\)""",
    re.MULTILINE,
)
_ACTIX_ROUTE_RE = re.compile(
    r"""\.route\s*\(\s*web::(get|post|put|patch|delete|head|options)\s*\(\s*\)\s*\.to\s*\((\w+)\)""",
)
_ACTIX_GUARD_RE = re.compile(
    r"""#\[(?:web::)?(get|post|put|patch|delete|head|options)\s*\(\s*"([^"]+)"\s*\)\]""",
    re.MULTILINE,
)
# #[get("/path")] / #[post("/path")] procedural macros above async fn
_ACTIX_PROC_RE = re.compile(
    r'#\[\s*(get|post|put|patch|delete|head|options)\s*\(\s*"([^"]+)"\s*\)\s*\]'
    r'(?:\s*\n(?:[ \t]*#\[[^\]]+\]\s*\n)*)'
    r'[ \t]*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)',
    re.MULTILINE,
)

# ── Koa (Node.js) ────────────────────────────────────────────────────────────

# router.get('/path', ...) / router.post('/path', ...) on a Router instance
# Deliberately excludes 'app' to avoid double-matching Express.
_KOA_VERB_RE = re.compile(
    r"""(?:^|[^\w])
    (?:router|koaRouter|Router)\s*\.\s*
    (get|post|put|patch|delete|del|head|options)\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)
_KOA_ALL_RE = re.compile(
    r"""(?:^|[^\w])
    (?:router|koaRouter|Router)\s*\.\s*all\s*\(
    \s*['"`]([^'"`]+)['"`]""",
    re.MULTILINE | re.VERBOSE,
)

# ── Fiber (Go) ───────────────────────────────────────────────────────────────

_FIBER_VERB_RE = re.compile(
    r"""(?:app|api|grp|v\d+|fiber)\s*\.\s*
    (Get|Post|Put|Patch|Delete|Head|Options|All)\s*\(
    \s*"([^"]+)"""  ,
    re.MULTILINE | re.VERBOSE,
)
_FIBER_GROUP_RE = re.compile(
    r"""(?:app|api|grp|v\d+|fiber)\s*\.\s*Group\s*\(\s*"([^"]+)"\s*\)""",
    re.MULTILINE,
)

# ── Beego (Go) ───────────────────────────────────────────────────────────────

_BEEGO_ROUTER_RE = re.compile(
    r"""beego\s*\.\s*Router\s*\(\s*"([^"]+)"\s*,""",
    re.MULTILINE,
)
_BEEGO_NAMESPACE_RE = re.compile(
    r"""beego\s*\.\s*NewNamespace\s*\(\s*"([^"]+)"""  ,
    re.MULTILINE,
)
_BEEGO_NS_ROUTER_RE = re.compile(
    r"""beego\s*\.\s*NSRouter\s*\(\s*"([^"]+)""",
    re.MULTILINE,
)

# ── Ktor (Kotlin) ────────────────────────────────────────────────────────────

# get("/path") { ... } / post("/path") { ... }
_KTOR_VERB_RE = re.compile(
    r"""(?:^|[^\w])
    (get|post|put|patch|delete|head|options|route)\s*\(
    \s*"([^"]+)""",
    re.MULTILINE | re.VERBOSE,
)
# route("/prefix") { ... } — outer prefix block
_KTOR_ROUTE_RE = re.compile(
    r"""(?:^|[^\w])route\s*\(\s*"([^"]+)"\s*\)""",
    re.MULTILINE,
)

# ── Quarkus / JAX-RS (Java) ──────────────────────────────────────────────────

# Class-level @Path
_JAXRS_CLASS_PATH_RE = re.compile(
    r"""@Path\s*\(\s*"([^"]+)"\s*\)
    (?:[^{]{0,200}?)          # optional other annotations / modifiers
    class\s+\w+""",
    re.MULTILINE | re.VERBOSE | re.DOTALL,
)
# Method-level HTTP verb annotation (may come before or after @Path)
_JAXRS_METHOD_RE = re.compile(
    r"""(@GET|@POST|@PUT|@DELETE|@PATCH|@HEAD|@OPTIONS)\s*\n
    (?:[ \t]*(?:@[^\n]+\n))*    # any number of other annotations
    [ \t]*(?:public\s+|private\s+|protected\s+)?
    (?:static\s+)?
    \S+\s+(\w+)\s*\(""",        # return-type methodName(
    re.MULTILINE | re.VERBOSE,
)
# Optional sub-path on the method itself: @Path("/{id}")
_JAXRS_SUB_PATH_RE = re.compile(
    r"""@Path\s*\(\s*"([^"]+)"\s*\)""",
    re.MULTILINE,
)

# ── Rocket (Rust) ────────────────────────────────────────────────────────────

_ROCKET_ATTR_RE = re.compile(
    r'#\[\s*(get|post|put|patch|delete|head|options)\s*\(\s*"([^"]+)"[^)]*\)\s*\]'
    r'(?:\s*\n(?:[ \t]*#\[[^\]]+\]\s*\n)*)'
    r'[ \t]*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)',
    re.MULTILINE,
)

# ── Sinatra (Ruby) ───────────────────────────────────────────────────────────

_SINATRA_VERB_RE = re.compile(
    r"""^[ \t]*(get|post|put|patch|delete|options|head)\s+
    ['"]([^'"]+)['"]\s+do\b""",
    re.MULTILINE | re.VERBOSE,
)

# ── Grape (Ruby) ─────────────────────────────────────────────────────────────

_GRAPE_RESOURCE_RE = re.compile(
    r"""^[ \t]*(?:resource|namespace)\s+
    (?::(\w+)|'([^']+)'|"([^"]+)")\s+do\b""",
    re.MULTILINE | re.VERBOSE,
)
_GRAPE_VERB_RE = re.compile(
    r"""^[ \t]*(get|post|put|patch|delete)\s+
    (?::(\w+)|'([^']+)'|"([^"]+)")?\s*(?:do\b|$)""",
    re.MULTILINE | re.VERBOSE,
)

# ── Laravel (PHP) ────────────────────────────────────────────────────────────

_LARAVEL_VERB_RE = re.compile(
    r"""Route\s*::\s*(get|post|put|patch|delete|options|any|match)\s*\(
    \s*['"]([^'"]+)['"]""",
    re.MULTILINE | re.VERBOSE,
)
_LARAVEL_RESOURCE_RE = re.compile(
    r"""Route\s*::\s*resource\s*\(\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_LARAVEL_PREFIX_RE = re.compile(
    r"""Route\s*::\s*prefix\s*\(\s*['"]([^'"]+)['"]\s*\)\s*->group""",
    re.MULTILINE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# TYPESCRIPT / JAVASCRIPT EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_express(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Express / Fastify / Hono verb-based route registrations.

    This is the shared extractor for all JS/TS frameworks that use the pattern:
      receiver.METHOD('/path', handler)
    where receiver is: app, router, server, api, r, v1, fastify, hono, etc.
    """
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "del": "DELETE", "head": "HEAD", "options": "OPTIONS",
    }
    for m in _EXPRESS_VERB_RE.finditer(content):
        verb = _METHOD_MAP.get(m.group(1).lower(), m.group(1).upper())
        path = m.group(2)
        ln = content[: m.start()].count("\n")
        end = _find_block_end_paren(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))

    # app.all('/path', ...) — any-method handler (Express, Hono, Fastify)
    for m in _EXPRESS_ALL_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_paren(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, "ANY", path, confidence="high"))

    # app.use('/prefix', subrouter) — treat as ANY with wildcard prefix
    for m in _EXPRESS_USE_RE.finditer(content):
        path = m.group(1)
        if "{" not in path and not path.endswith(".js"):
            ln = content[: m.start()].count("\n")
            routes.append(_make_route(file_path, lines, ln, ln, "ANY", path + "/*", confidence="medium"))
    return routes


def _extract_fastify(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Fastify-specific route patterns not covered by the Express extractor.

    The verb-based calls (fastify.get, server.post …) share the same syntax as
    Express and are already caught by _extract_express.  Here we only handle the
    Fastify-unique `fastify.route({ method, url, … })` object form.
    """
    routes: list[dict] = []
    # fastify.route({ method, url, handler })
    for m in _FASTIFY_ROUTE_OBJ_RE.finditer(content):
        ln = content[: m.start()].count("\n")
        end = _find_block_end_brace(lines, ln, limit=30)
        block = "\n".join(lines[ln : end + 1])
        method_m = _FASTIFY_METHOD_RE.search(block)
        url_m    = _FASTIFY_URL_RE.search(block)
        if method_m and url_m:
            routes.append(_make_route(
                file_path, lines, ln, end,
                method_m.group(1).upper(), url_m.group(1),
                confidence="high",
            ))
    return routes


def _extract_hono(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Hono-specific patterns not covered by the Express extractor.

    app.get/post/etc and app.all() are already caught by _extract_express
    (which handles all JS/TS verb-based receivers including hono).
    This function is a no-op placeholder; Hono is fully covered by _extract_express.
    Keeping it as a named entry point for clarity and future Hono-specific patterns.
    """
    return []


def _extract_koa(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Koa router route registrations (koa-router / @koa/router).

    Matches ``router.get('/path', ...)`` style calls on a Router instance.
    Deliberately avoids matching ``app.get(...)`` to prevent double-counting
    with the Express extractor.
    """
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "del": "DELETE", "head": "HEAD", "options": "OPTIONS",
    }
    for m in _KOA_VERB_RE.finditer(content):
        verb = _METHOD_MAP.get(m.group(1).lower(), m.group(1).upper())
        path = m.group(2)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_paren(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))

    for m in _KOA_ALL_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_paren(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, "ANY", path, confidence="high"))

    return routes


def _extract_nestjs(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []

    # Collect @Controller prefix(es) — a file may have multiple controllers
    controller_prefixes: list[str] = []
    for m in _NEST_CONTROLLER_RE.finditer(content):
        prefix = (m.group(1) or "").strip("/")
        controller_prefixes.append("/" + prefix if prefix else "")

    if not controller_prefixes:
        return routes

    _NEST_HTTP_MAP = {
        "Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE",
        "Patch": "PATCH", "Options": "OPTIONS", "Head": "HEAD", "All": "ANY",
    }

    for m in _NEST_METHOD_RE.finditer(content):
        http_verb   = _NEST_HTTP_MAP.get(m.group(1), m.group(1).upper())
        sub_path    = (m.group(2) or "").strip("/")
        handler     = m.group(3) or ""
        ln          = content[: m.start()].count("\n")
        end         = _find_block_end_brace(lines, ln, limit=80)

        for prefix in controller_prefixes:
            full_path = prefix + ("/" + sub_path if sub_path else "")
            full_path = full_path or "/"
            routes.append(_make_route(file_path, lines, ln, end, http_verb, full_path, handler, "high"))

    return routes


def _extract_trpc(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    _PROC_MAP = {"query": "GET", "mutation": "POST", "subscription": "GET"}

    # Old-style
    for m in _TRPC_OLD_RE.finditer(content):
        proc_type = m.group(1)
        name      = m.group(2)
        verb      = _PROC_MAP.get(proc_type, "POST")
        ln        = content[: m.start()].count("\n")
        end       = _find_block_end_brace(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, f"/trpc/{name}", name, "high"))

    # New-style t.router({ name: t.procedure.query(...) })
    for m in _TRPC_PROC_RE.finditer(content):
        name      = m.group(1)
        proc_type = m.group(2)
        verb      = _PROC_MAP.get(proc_type, "POST")
        ln        = content[: m.start()].count("\n")
        end       = _find_block_end_brace(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, f"/trpc/{name}", name, "medium"))

    return routes


def _path_from_next_app_segment(segment: str) -> str:
    """Convert a Next.js app-router path segment to a URL segment.

    Rules:
      [id]        -> :id
      [...id]     -> :id*
      [[...id]]   -> :id?
      (group)     -> <empty — route groups are invisible in URL>
      @slot       -> <skip — parallel routes>
    """
    if re.fullmatch(r"\([^)]+\)", segment):  # (group) — invisible
        return ""
    if segment.startswith("@"):              # @slot — parallel route
        return ""
    return _normalize_path(segment)


def _extract_nextjs_app_router_from_content(
    content: str, file_path: str, lines: list[str]
) -> list[dict]:
    """Detect exported HTTP method handlers in Next.js App Router route files."""
    routes: list[dict] = []
    if not (file_path.endswith("/route.ts") or file_path.endswith("/route.js")
            or "/route." in file_path):
        return routes

    _NEXT_EXPORT_RE = re.compile(
        r"""^export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b""",
        re.MULTILINE,
    )
    # Derive path from file location — done in file-based detector; here we just
    # emit one route per exported handler with confidence=medium (path unknown
    # without root context).
    for m in _NEXT_EXPORT_RE.finditer(content):
        verb = m.group(1)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_brace(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, "/", confidence="medium"))
    return routes


def _extract_sveltekit(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Detect exported HTTP method handlers in SvelteKit +server files."""
    routes: list[dict] = []
    if "+server." not in file_path and "+server.ts" not in file_path:
        if not (file_path.endswith("+server.ts") or file_path.endswith("+server.js")):
            # still try to find exports — caller may be passing just content
            pass

    for m in _SVELTE_EXPORT_RE.finditer(content):
        verb = m.group(1)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_brace(lines, ln, limit=80)
        routes.append(_make_route(file_path, lines, ln, end, verb, "/", confidence="medium"))
    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# PYTHON EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _find_block_end_py(lines: list[str], start: int) -> int:
    """Find end of a Python def block by indentation."""
    if start + 1 >= len(lines):
        return start
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    last_content = start
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent and re.match(r"\s*(?:async\s+)?def\s+|class\s+|@", line):
            return last_content
        last_content = i
    return last_content


def _extract_fastapi(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "head": "HEAD", "options": "OPTIONS",
    }

    for m in _FASTAPI_VERB_RE.finditer(content):
        verb  = _METHOD_MAP.get(m.group(1).lower(), m.group(1).upper())
        path  = m.group(2)
        ln    = content[: m.start()].count("\n")
        # Find the actual def line
        fn_ln = ln
        for i in range(ln, min(ln + 5, len(lines))):
            if re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[i]):
                fn_m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[i])
                handler = fn_m.group(1) if fn_m else ""
                fn_ln   = i
                end     = _find_block_end_py(lines, fn_ln)
                routes.append(_make_route(file_path, lines, fn_ln, end, verb, path, handler, "high"))
                break
        else:
            routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    for m in _FASTAPI_API_ROUTE_RE.finditer(content):
        path    = m.group(1)
        methods = [x.strip().strip("'\"") for x in m.group(2).split(",")]
        ln      = content[: m.start()].count("\n")
        for fn_i in range(ln, min(ln + 5, len(lines))):
            if re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[fn_i]):
                fn_m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[fn_i])
                handler = fn_m.group(1) if fn_m else ""
                end     = _find_block_end_py(lines, fn_i)
                for verb in methods:
                    routes.append(_make_route(file_path, lines, fn_i, end, verb.upper(), path, handler, "high"))
                break
    return routes


def _extract_flask(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    for m in _FLASK_ROUTE_RE.finditer(content):
        path    = m.group(1)
        methods_raw = m.group(2)
        if methods_raw:
            methods = [x.strip().strip("'\"") for x in methods_raw.split(",")]
        else:
            methods = ["GET"]  # Flask default when methods not specified
        ln = content[: m.start()].count("\n")
        # Find the def/async def right after the decorator
        fn_ln   = ln
        handler = ""
        for i in range(ln, min(ln + 6, len(lines))):
            fn_m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[i])
            if fn_m:
                handler = fn_m.group(1)
                fn_ln   = i
                break
        end = _find_block_end_py(lines, fn_ln)
        for verb in methods:
            routes.append(_make_route(file_path, lines, fn_ln, end, verb.upper(), path, handler, "high"))
    return routes


def _extract_django(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract routes from Django urls.py patterns."""
    routes: list[dict] = []
    # Only process files that look like url configurations
    basename = os.path.basename(file_path)
    if basename not in ("urls.py",) and "url" not in basename.lower():
        return routes

    for m in _DJANGO_PATH_RE.finditer(content):
        raw_path  = m.group(1).rstrip("$^")  # strip Django regex anchors
        view_name = m.group(2)
        ln        = content[: m.start()].count("\n")
        # Convert Django path converters and regex named groups to normalised form
        # Strip Django regex anchors ^ $
        raw_path = raw_path.lstrip("^").rstrip("$")
        # Django regex named groups: (?P<year>[0-9]{4}) -> {year}
        # Use a non-greedy match that handles nested brackets in the pattern
        raw_path = re.sub(r"\(\?P<(\w+)>[^)]*(?:\([^)]*\)[^)]*)*\)", r"{\1}", raw_path)
        # Django path converters: <int:pk>, <str:slug>, <uuid:id> -> :pk, :slug, :id
        # (these appear after path() not re_path())
        raw_path = re.sub(r"<(?:\w+:)(\w+)>", r"{\1}", raw_path)
        raw_path = re.sub(r"<(\w+)>", r"{\1}", raw_path)
        raw_path = "/" + raw_path.lstrip("/")
        routes.append(_make_route(file_path, lines, ln, ln, "ANY", raw_path, view_name, "high"))
    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# GO EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_gin(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    _GIN_METHOD_MAP = {
        "GET": "GET", "POST": "POST", "PUT": "PUT", "PATCH": "PATCH",
        "DELETE": "DELETE", "HEAD": "HEAD", "OPTIONS": "OPTIONS", "Any": "ANY",
    }
    # Collect Group prefixes and their variable names
    group_prefixes: dict[str, str] = {}  # var_name -> prefix
    for m in _GIN_GROUP_RE.finditer(content):
        prefix = m.group(1)
        # Extract variable name from `v1 := r.Group(...)` or `v1 = r.Group(...)`
        line_start_pos = content.rfind("\n", 0, m.start()) + 1
        line = content[line_start_pos: content.find("\n", m.start())]
        var_m = re.match(r"\s*(\w+)\s*(?::=|=)", line)
        if var_m:
            group_prefixes[var_m.group(1)] = prefix

    for m in _GIN_VERB_RE.finditer(content):
        verb = _GIN_METHOD_MAP.get(m.group(1), m.group(1).upper())
        path = m.group(2)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_brace(lines, ln, limit=60)

        # Check if called on a group variable
        line_start_pos = content.rfind("\n", 0, m.start()) + 1
        line_prefix    = content[line_start_pos: m.start()]
        recv_m         = re.search(r"(\w+)\s*\.\s*$", line_prefix)
        if recv_m and recv_m.group(1) in group_prefixes:
            path = group_prefixes[recv_m.group(1)].rstrip("/") + "/" + path.lstrip("/")

        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))
    return routes


def _extract_gorilla(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    for m in _GORILLA_HANDLEFUNC_RE.finditer(content):
        path    = m.group(1)
        handler = m.group(2)
        ln      = content[: m.start()].count("\n")
        # Look for .Methods() chained on same or next line
        rest_of_stmt = content[m.end(): m.end() + 200]
        methods_m = _GORILLA_METHODS_RE.search(rest_of_stmt)
        if methods_m:
            raw_methods = re.findall(r'"([A-Z]+)"', methods_m.group(0))
            for verb in raw_methods:
                routes.append(_make_route(file_path, lines, ln, ln, verb, path, handler, "high"))
        else:
            routes.append(_make_route(file_path, lines, ln, ln, "ANY", path, handler, "high"))

    for m in _GORILLA_PREFIX_RE.finditer(content):
        prefix = m.group(1)
        ln     = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, "ANY", prefix + "/*", confidence="medium"))
    return routes


def _extract_echo(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    _ECHO_METHOD_MAP = {
        "GET": "GET", "POST": "POST", "PUT": "PUT", "PATCH": "PATCH",
        "DELETE": "DELETE", "HEAD": "HEAD", "OPTIONS": "OPTIONS", "Any": "ANY",
    }
    for m in _ECHO_VERB_RE.finditer(content):
        verb = _ECHO_METHOD_MAP.get(m.group(1), m.group(1).upper())
        path = m.group(2)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_brace(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))
    return routes


def _extract_chi(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    _CHI_METHOD_MAP = {
        "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
        "Delete": "DELETE", "Head": "HEAD", "Options": "OPTIONS", "Handle": "ANY",
    }

    # Collect r.Route("/prefix", ...) groups
    route_groups: list[str] = []
    for m in _CHI_ROUTE_RE.finditer(content):
        route_groups.append(m.group(1))

    for m in _CHI_VERB_RE.finditer(content):
        verb_raw = m.group(1)
        path     = m.group(2)
        verb     = _CHI_METHOD_MAP.get(verb_raw, verb_raw.upper())
        ln       = content[: m.start()].count("\n")
        end      = _find_block_end_brace(lines, ln, limit=60)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))

    for prefix in route_groups:
        ln = 0
        routes.append(_make_route(file_path, lines, ln, ln, "ANY", prefix + "/*", confidence="medium"))

    return routes


def _extract_fiber(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Fiber (Go) route registrations.

    Handles both direct ``app.Get("/path", handler)`` calls and group-prefixed
    calls like ``api := app.Group("/api"); api.Get("/users", handler)``.
    """
    routes: list[dict] = []
    _FIBER_METHOD_MAP = {
        "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
        "Delete": "DELETE", "Head": "HEAD", "Options": "OPTIONS", "All": "ANY",
    }

    # Collect Group prefixes: `api := app.Group("/api")`
    group_prefixes: dict[str, str] = {}
    for m in _FIBER_GROUP_RE.finditer(content):
        prefix = m.group(1)
        line_start_pos = content.rfind("\n", 0, m.start()) + 1
        line = content[line_start_pos: content.find("\n", m.start())]
        var_m = re.match(r"\s*(\w+)\s*(?::=|=)", line)
        if var_m:
            group_prefixes[var_m.group(1)] = prefix

    for m in _FIBER_VERB_RE.finditer(content):
        verb = _FIBER_METHOD_MAP.get(m.group(1), m.group(1).upper())
        path = m.group(2)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_brace(lines, ln, limit=60)

        # Check if called on a group variable
        line_start_pos = content.rfind("\n", 0, m.start()) + 1
        line_prefix    = content[line_start_pos: m.start()]
        recv_m         = re.search(r"(\w+)\s*\.\s*$", line_prefix)
        if recv_m and recv_m.group(1) in group_prefixes:
            path = group_prefixes[recv_m.group(1)].rstrip("/") + "/" + path.lstrip("/")

        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))
    return routes


def _extract_beego(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Beego (Go) route registrations.

    Handles ``beego.Router("/path", &Controller{})`` and
    ``beego.NSRouter("/path", &Controller{})`` inside a namespace.
    """
    routes: list[dict] = []

    # Collect namespace prefixes
    ns_prefixes: list[str] = []
    for m in _BEEGO_NAMESPACE_RE.finditer(content):
        ns_prefixes.append(m.group(1))

    for m in _BEEGO_ROUTER_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, "ANY", path, confidence="high"))

    for m in _BEEGO_NS_ROUTER_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        # Apply first namespace prefix if available
        if ns_prefixes:
            path = ns_prefixes[0].rstrip("/") + "/" + path.lstrip("/")
        routes.append(_make_route(file_path, lines, ln, ln, "ANY", path, confidence="high"))

    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# RUST EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_axum(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    for m in _AXUM_ROUTE_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        end  = _find_block_end_paren(lines, ln, limit=20)

        # Extract the full handler expression by paren-balanced scanning
        # starting from where the regex match ends (after the path + comma + space)
        pos = m.end()
        depth = 1  # we are inside .route(
        scan_end = min(pos + 300, len(content))
        handler_start = pos
        i = pos
        while i < scan_end and depth > 0:
            if content[i] == '(':
                depth += 1
            elif content[i] == ')':
                depth -= 1
            i += 1
        handler_part = content[handler_start: i - 1]  # exclude final )

        # Each verb call like get(h), post(h2), put(h3) in the expression
        verbs = _AXUM_METHOD_RE.findall(handler_part)
        if verbs:
            for verb in verbs:
                routes.append(_make_route(file_path, lines, ln, end, verb.upper(), path, confidence="high"))
        else:
            routes.append(_make_route(file_path, lines, ln, end, "ANY", path, confidence="medium"))
    return routes


def _extract_actix(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []

    # Procedural macros: #[get("/path")]  async fn handler_name
    for m in _ACTIX_PROC_RE.finditer(content):
        verb    = m.group(1).upper()
        path    = m.group(2)
        handler = m.group(3)
        ln      = content[: m.start()].count("\n")
        end     = _find_block_end_brace(lines, ln, limit=80)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, handler, "high"))

    # web::resource("/path").route(web::get().to(handler))
    for m in _ACTIX_RESOURCE_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        # scan forward for chained .route(web::METHOD().to(...))
        rest = content[m.end(): m.end() + 400]
        for rm in _ACTIX_ROUTE_RE.finditer(rest):
            verb    = rm.group(1).upper()
            handler = rm.group(2)
            routes.append(_make_route(file_path, lines, ln, ln, verb, path, handler, "high"))
        if not _ACTIX_ROUTE_RE.search(rest[:200]):
            routes.append(_make_route(file_path, lines, ln, ln, "ANY", path, confidence="medium"))

    return routes


def _extract_rocket(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Rocket (Rust) attribute-macro route definitions.

    Handles ``#[get("/path")] fn handler()`` and the data/guard variants.
    Rocket uses ``<id>`` for path params — ``_normalize_path`` already converts
    those to ``:id``.
    """
    routes: list[dict] = []
    for m in _ROCKET_ATTR_RE.finditer(content):
        verb    = m.group(1).upper()
        path    = m.group(2)
        handler = m.group(3)
        ln      = content[: m.start()].count("\n")
        end     = _find_block_end_brace(lines, ln, limit=80)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, handler, "high"))
    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# KOTLIN EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_ktor(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Ktor (Kotlin) routing DSL route definitions.

    Handles the ``routing { get("/path") { ... } }`` DSL, including nested
    ``route("/prefix") { get { ... } }`` blocks, and ``authenticate { ... }``
    wrappers (ignored for path purposes — inner routes are still extracted).
    """
    routes: list[dict] = []
    _KTOR_METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "head": "HEAD", "options": "OPTIONS",
    }

    # Collect route("prefix") blocks and their prefix strings
    route_prefixes: dict[int, str] = {}  # line_no -> prefix
    for m in _KTOR_ROUTE_RE.finditer(content):
        ln = content[: m.start()].count("\n")
        route_prefixes[ln] = m.group(1)

    for m in _KTOR_VERB_RE.finditer(content):
        verb_raw = m.group(1).lower()
        if verb_raw == "route":
            continue  # handled separately as prefix blocks
        path = m.group(2)
        ln   = content[: m.start()].count("\n")
        verb = _KTOR_METHOD_MAP.get(verb_raw, verb_raw.upper())
        end  = _find_block_end_brace(lines, ln, limit=60)

        # Check whether this verb call is inside a route("prefix") block by
        # scanning backwards for the nearest route_prefix whose block contains ln
        best_prefix = ""
        for prefix_ln, prefix_val in route_prefixes.items():
            if prefix_ln < ln:
                # Approximate: find the brace block end for this prefix line
                block_end = _find_block_end_brace(lines, prefix_ln, limit=200)
                if ln <= block_end:
                    # Longer prefix wins (innermost nesting)
                    if len(prefix_val) >= len(best_prefix):
                        best_prefix = prefix_val

        if best_prefix:
            path = best_prefix.rstrip("/") + "/" + path.lstrip("/")

        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))

    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# JAVA EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_jaxrs(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract JAX-RS annotated routes (Quarkus, Micronaut, Jersey).

    Combines class-level ``@Path`` with method-level ``@GET/@POST/...``.
    Also handles an optional method-level ``@Path`` sub-path.
    """
    routes: list[dict] = []
    _JAXRS_VERB_MAP = {
        "@GET": "GET", "@POST": "POST", "@PUT": "PUT",
        "@DELETE": "DELETE", "@PATCH": "PATCH",
        "@HEAD": "HEAD", "@OPTIONS": "OPTIONS",
    }

    # Find class-level @Path annotations and their line ranges
    class_paths: list[tuple[str, int, int]] = []  # (path, start_line, end_line)
    for m in _JAXRS_CLASS_PATH_RE.finditer(content):
        class_path = m.group(1)
        start_ln   = content[: m.start()].count("\n")
        end_ln     = _find_block_end_brace(lines, start_ln, limit=500)
        class_paths.append((class_path, start_ln, end_ln))

    if not class_paths:
        return routes

    for m in _JAXRS_METHOD_RE.finditer(content):
        verb_ann = m.group(1)
        handler  = m.group(2)
        verb     = _JAXRS_VERB_MAP.get(verb_ann, "ANY")
        ln       = content[: m.start()].count("\n")
        end      = _find_block_end_brace(lines, ln, limit=60)

        # Look for a @Path on the method itself.
        # Strategy: collect all consecutive annotation lines that form the
        # annotation cluster around the verb annotation (no blank lines, no '}'
        # separator).  Search only that cluster, excluding the class-level paths.
        cluster_start = ln
        for back in range(ln - 1, max(-1, ln - 8), -1):
            back_line = lines[back].strip() if back < len(lines) else ""
            if not back_line:
                break  # blank line separates clusters
            if back_line.startswith("}") or back_line.startswith("{"):
                break  # block boundary
            if re.match(r"(public|private|protected|static|final|abstract)\b", back_line):
                break
            cluster_start = back

        cluster_end = ln
        for fwd in range(ln + 1, min(ln + 5, len(lines))):
            fwd_line = lines[fwd].strip() if fwd < len(lines) else ""
            if not fwd_line:
                break
            if not fwd_line.startswith("@") and not fwd_line.startswith("public") \
                    and not fwd_line.startswith("private") and not fwd_line.startswith("protected"):
                break
            cluster_end = fwd

        annotation_cluster = "\n".join(lines[cluster_start: cluster_end + 1])
        sub_path = ""
        for pm in _JAXRS_SUB_PATH_RE.finditer(annotation_cluster):
            candidate = pm.group(1)
            already_class = any(cp == candidate for cp, _, _ in class_paths)
            if not already_class:
                sub_path = candidate  # last non-class match wins

        # Which class does this method belong to?
        base_class_path = ""
        for cp, cp_start, cp_end in class_paths:
            if cp_start <= ln <= cp_end:
                base_class_path = cp
                break

        full_path = ("/" + base_class_path.strip("/") + "/" + sub_path.strip("/")).replace("//", "/")
        if full_path != "/" and full_path.endswith("/"):
            full_path = full_path[:-1]

        routes.append(_make_route(file_path, lines, ln, end, verb, full_path, handler, "high"))

    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# RUBY EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_sinatra(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Sinatra (Ruby) route definitions.

    Handles ``get '/path' do ... end`` style declarations.
    """
    routes: list[dict] = []
    _SINATRA_METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "options": "OPTIONS", "head": "HEAD",
    }
    for m in _SINATRA_VERB_RE.finditer(content):
        verb = _SINATRA_METHOD_MAP.get(m.group(1).lower(), m.group(1).upper())
        path = m.group(2)
        ln   = content[: m.start()].count("\n")
        # Find matching 'end' by scanning forward
        end = ln
        for i in range(ln + 1, min(ln + 100, len(lines))):
            stripped = lines[i].strip()
            if stripped == "end":
                end = i
                break
        routes.append(_make_route(file_path, lines, ln, end, verb, path, confidence="high"))
    return routes


def _extract_grape(content: str, file_path: str, lines: list[str]) -> list[dict]:
    """Extract Grape (Ruby API framework) route definitions.

    Handles ``resource :users do`` blocks with ``get``, ``post``, etc. inside,
    and ``namespace :admin do`` prefixes.  Path parameters use ``:name`` syntax
    which ``_normalize_path`` already handles for the ``:param`` form.
    """
    routes: list[dict] = []
    _GRAPE_METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT",
        "patch": "PATCH", "delete": "DELETE",
    }

    # Build a list of (prefix, block_start_line, block_end_line) from resource/namespace
    resource_blocks: list[tuple[str, int, int]] = []
    for m in _GRAPE_RESOURCE_RE.finditer(content):
        # Group 1: :symbol, Group 2: 'string', Group 3: "string"
        name = m.group(1) or m.group(2) or m.group(3) or ""
        prefix = "/" + name.strip("/") if name else "/"
        ln = content[: m.start()].count("\n")
        # Find matching 'end'
        depth = 0
        end_ln = ln
        for i in range(ln, min(ln + 300, len(lines))):
            stripped = lines[i].strip()
            if stripped.endswith(" do") or stripped == "do":
                depth += 1
            if stripped == "end":
                depth -= 1
                if depth <= 0:
                    end_ln = i
                    break
        resource_blocks.append((prefix, ln, end_ln))

    for m in _GRAPE_VERB_RE.finditer(content):
        verb_raw  = m.group(1).lower()
        # Sub-path: group 2 (:symbol), group 3 ('str'), group 4 ("str")
        sub_raw   = m.group(2) or m.group(3) or m.group(4) or ""
        sub_path  = ("/" + sub_raw.strip("/")) if sub_raw else ""
        verb      = _GRAPE_METHOD_MAP.get(verb_raw, verb_raw.upper())
        ln        = content[: m.start()].count("\n")
        end       = ln + 5  # approximate

        # Find which resource block contains this line
        best_prefix = ""
        for prefix, blk_start, blk_end in resource_blocks:
            if blk_start < ln <= blk_end:
                if len(prefix) >= len(best_prefix):
                    best_prefix = prefix

        full_path = (best_prefix.rstrip("/") + sub_path) or "/"
        if not full_path.startswith("/"):
            full_path = "/" + full_path

        routes.append(_make_route(file_path, lines, ln, end, verb, full_path, confidence="high"))

    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# PHP / LARAVEL EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

_LARAVEL_RESOURCE_VERBS = [
    ("GET",    ""),
    ("GET",    "/create"),
    ("POST",   ""),
    ("GET",    "/:id"),
    ("GET",    "/:id/edit"),
    ("PUT",    "/:id"),
    ("DELETE", "/:id"),
]


def _extract_laravel(content: str, file_path: str, lines: list[str]) -> list[dict]:
    routes: list[dict] = []
    _LAR_METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "options": "OPTIONS", "any": "ANY",
    }
    _MATCH_METHODS_RE = re.compile(
        r"""Route\s*::\s*match\s*\(\s*\[([^\]]+)\]\s*,\s*['"]([^'"]+)['"]""",
        re.MULTILINE,
    )

    # Collect prefix groups
    prefix_stack: list[str] = []
    for m in _LARAVEL_PREFIX_RE.finditer(content):
        prefix_stack.append(m.group(1))

    prefix = prefix_stack[0] if prefix_stack else ""

    for m in _LARAVEL_VERB_RE.finditer(content):
        raw_verb = m.group(1).lower()
        path     = ("/" + prefix.strip("/") + "/" + m.group(2).lstrip("/")).replace("//", "/")
        ln       = content[: m.start()].count("\n")

        if raw_verb == "match":
            # Route::match(['get','post'], '/path', ...)
            match_m = _MATCH_METHODS_RE.search(content[m.start(): m.start() + 200])
            if match_m:
                methods = [x.strip().strip("'\"") for x in match_m.group(1).split(",")]
                path2   = ("/" + prefix.strip("/") + "/" + match_m.group(2).lstrip("/")).replace("//", "/")
                for verb in methods:
                    routes.append(_make_route(file_path, lines, ln, ln, verb.upper(), path2, confidence="high"))
            continue

        verb = _LAR_METHOD_MAP.get(raw_verb, raw_verb.upper())
        routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    # Route::resource('/users', UserController::class) — expand to 7 CRUD routes
    for m in _LARAVEL_RESOURCE_RE.finditer(content):
        base_path = ("/" + prefix.strip("/") + "/" + m.group(1).lstrip("/")).replace("//", "/")
        ln        = content[: m.start()].count("\n")
        for verb, suffix in _LARAVEL_RESOURCE_VERBS:
            routes.append(_make_route(
                file_path, lines, ln, ln, verb,
                base_path.rstrip("/") + suffix,
                confidence="high",
            ))
    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# NEW FRAMEWORK REGEXES
# ═══════════════════════════════════════════════════════════════════════════════

# ── Starlette ─────────────────────────────────────────────────────────────────

_STARLETTE_ROUTE_RE = re.compile(
    r"""Route\s*\(\s*["']([^"']+)["']\s*,\s*(?:endpoint\s*=\s*)?(\w[\w.]*)(?:[^)]*?methods\s*=\s*\[([^\]]*)\])?""",
    re.MULTILINE,
)
_STARLETTE_WS_RE = re.compile(
    r"""WebSocketRoute\s*\(\s*["']([^"']+)["']""",
    re.MULTILINE,
)
_STARLETTE_MOUNT_RE = re.compile(
    r"""Mount\s*\(\s*["']([^"']+)["']""",
    re.MULTILINE,
)

# ── Litestar / Starlite ───────────────────────────────────────────────────────

_LITESTAR_VERB_RE = re.compile(
    r"""@(?:\w+\.)?(?P<http_method>get|post|put|patch|delete|head|options|route)\s*\(\s*(?:path\s*=\s*)?["'](?P<path>[^"']+)["']""",
    re.MULTILINE | re.IGNORECASE,
)
_LITESTAR_DEF_RE = re.compile(
    r"""(?:async\s+)?def\s+(\w+)""",
)
_LITESTAR_CONTROLLER_RE = re.compile(
    r"""class\s+(\w+)\s*\(\s*Controller\s*\)""",
    re.MULTILINE,
)
_LITESTAR_CTRL_PATH_RE = re.compile(
    r"""path\s*=\s*["']([^"']+)["']""",
    re.MULTILINE,
)

# ── aiohttp ───────────────────────────────────────────────────────────────────

_AIOHTTP_ADD_VERB_RE = re.compile(
    r"""\.add_(?P<method>get|post|put|patch|delete|head|options)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_AIOHTTP_ADD_ROUTE_RE = re.compile(
    r"""\.add_route\s*\(\s*["'](?P<method>[A-Z]+)["']\s*,\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_AIOHTTP_DECORATOR_RE = re.compile(
    r"""@\w+\.(?P<method>get|post|put|patch|delete|head|options)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_AIOHTTP_WEB_VERB_RE = re.compile(
    r"""web\.(?P<method>get|post|put|patch|delete|head|options)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_AIOHTTP_WEB_ROUTE_RE = re.compile(
    r"""web\.route\s*\(\s*["'](?P<method>[A-Z]+)["']\s*,\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)

# ── Django Ninja ──────────────────────────────────────────────────────────────

_NINJA_VERB_RE = re.compile(
    r"""@\w+\.(?P<method>get|post|put|patch|delete)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_NINJA_API_OP_RE = re.compile(
    r"""@\w+\.api_operation\s*\(\s*\[(?P<methods>[^\]]+)\]\s*,\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)

# ── Sanic ─────────────────────────────────────────────────────────────────────

_SANIC_VERB_RE = re.compile(
    r"""@\w+\.(?P<method>get|post|put|patch|delete|head|options)\s*\(\s*["'](?P<path>[^"'<>]+)["']""",
    re.MULTILINE,
)
_SANIC_ROUTE_RE = re.compile(
    r"""@\w+\.route\s*\(\s*["'](?P<path>[^"']+)["'][^)]*?methods\s*=\s*\[(?P<methods>[^\]]+)\]""",
    re.MULTILINE,
)
_SANIC_ADD_ROUTE_RE = re.compile(
    r"""\w+\.add_route\s*\(\s*(\w+)\s*,\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_SANIC_PARAM_RE = re.compile(r"<(\w+)(?::\w+)?>")

# ── Tornado ───────────────────────────────────────────────────────────────────

_TORNADO_APP_RE = re.compile(
    r"""Application\s*\(\s*\[""",
    re.MULTILINE,
)
_TORNADO_TUPLE_RE = re.compile(
    r"""\(\s*(r?["'][^"']+["'])\s*,\s*(\w+)\s*\)""",
)
_TORNADO_HANDLER_METHOD_RE = re.compile(
    r"""def\s+(get|post|put|delete|patch|head)\s*\(self""",
    re.MULTILINE,
)
_TORNADO_NAMED_GROUP_RE = re.compile(r"\(\?P<(\w+)>[^)]*\)")
_TORNADO_ANON_GROUP_RE  = re.compile(r"\(\\d\+\)|\(\[.*?\]\+\)")

# ── GraphQL SDL ───────────────────────────────────────────────────────────────

_GQL_TYPE_BLOCK_RE = re.compile(
    r"""type\s+(Query|Mutation|Subscription)\s*\{([^}]+)\}""",
    re.MULTILINE | re.DOTALL,
)
_GQL_FIELD_RE = re.compile(
    r"""^\s+(\w+)(?:\([^)]*\))?\s*:""",
    re.MULTILINE,
)

# ── Apollo Server / GraphQL Yoga ──────────────────────────────────────────────

_APOLLO_TYPEDEF_RE = re.compile(
    r"""(?:const|let|var)\s+\w+\s*=\s*(?:gql\s*)?`([^`]+)`""",
    re.MULTILINE | re.DOTALL,
)
_APOLLO_GQL_TAG_RE = re.compile(
    r"""gql\s*`([^`]+)`""",
    re.MULTILINE | re.DOTALL,
)

# ── Strawberry ────────────────────────────────────────────────────────────────

_STRAWBERRY_OP_RE = re.compile(
    r"""@strawberry\.(?P<op_type>mutation|subscription)\s*(?:\([^)]*\))?\s*\n\s+(?:async\s+)?def\s+(?P<name>\w+)""",
    re.MULTILINE,
)
_STRAWBERRY_FIELD_RE = re.compile(
    r"""@strawberry\.field\s*\n\s+(?:async\s+)?def\s+(?P<name>\w+)""",
    re.MULTILINE,
)
_STRAWBERRY_TYPE_RE = re.compile(
    r"""@strawberry\.type\s*\nclass\s+(Query|Mutation|Subscription)""",
    re.MULTILINE,
)

# ── Graphene ──────────────────────────────────────────────────────────────────

_GRAPHENE_CLASS_RE = re.compile(
    r"""class\s+(Query|Mutation|Subscription)\s*\(\s*graphene\.ObjectType\s*\)""",
    re.MULTILINE,
)
_GRAPHENE_FIELD_RE = re.compile(
    r"""(\w+)\s*=\s*graphene\.(?:List|Field|String|Int|Float|Boolean|ID)\s*\(""",
    re.MULTILINE,
)
_GRAPHENE_RESOLVER_RE = re.compile(
    r"""def\s+resolve_(\w+)\s*\(self,\s*info""",
    re.MULTILINE,
)

# ── Feathers.js ───────────────────────────────────────────────────────────────

_FEATHERS_USE_RE = re.compile(
    r"""app\.use\s*\(\s*['"`]([^'"`]+)['"`]\s*,\s*(?:new\s+)?(\w+)""",
    re.MULTILINE,
)
_FEATHERS_SERVICE_METHOD_RE = re.compile(
    r"""async\s+(?:find|get|create|update|patch|remove)\s*\(""",
    re.MULTILINE,
)

# Feathers method -> (HTTP method, path suffix)
_FEATHERS_METHOD_MAP = {
    "find":   [("GET",    "")],
    "get":    [("GET",    "/:id")],
    "create": [("POST",   "")],
    "update": [("PUT",    "/:id")],
    "patch":  [("PATCH",  "/:id")],
    "remove": [("DELETE", "/:id")],
}

# ── AdonisJS ──────────────────────────────────────────────────────────────────

_ADONIS_ROUTE_RE = re.compile(
    r"""(?:Route|router)\s*\.(?P<method>get|post|put|patch|delete)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_ADONIS_DECO_RE = re.compile(
    r"""@route\.(?P<method>get|post|put|patch|delete)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)

# ── Elysia ────────────────────────────────────────────────────────────────────

_ELYSIA_VERB_RE = re.compile(
    r"""\.(?P<method>get|post|put|patch|delete|options|head|all)\s*\(\s*['"`](?P<path>[^'"`]+)['"`]""",
    re.MULTILINE,
)

# ── Symfony (PHP) ─────────────────────────────────────────────────────────────

_SYMFONY_ATTR_RE = re.compile(
    r"""#\[Route\s*\(\s*['"](?P<path>[^'"]+)['"]\s*(?:,\s*name:\s*\w+)?\s*(?:,\s*methods:\s*\[(?P<methods>[^\]]+)\])?\s*\)\]""",
    re.MULTILINE,
)
_SYMFONY_ANN_RE = re.compile(
    r"""@Route\s*\(\s*["'](?P<path>[^"']+)["'][^)]*?(?:methods\s*=\s*\{(?P<methods>[^}]+)\})?""",
    re.MULTILINE,
)
_SYMFONY_YAML_RE = re.compile(
    r"""path:\s*(?P<path>[^\n]+)\n\s+controller:.*?\n\s+methods:\s*\[?(?P<methods>[^\]}\n]+)""",
    re.MULTILINE | re.DOTALL,
)

# ── Play Framework (Scala routes file) ────────────────────────────────────────

_PLAY_ROUTE_RE = re.compile(
    r"""^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/[^\s]*)\s+([\w.]+(?:\([^)]*\))?)""",
    re.MULTILINE,
)

# ── Buffalo (Go) ──────────────────────────────────────────────────────────────

_BUFFALO_VERB_RE = re.compile(
    r"""app\.(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\(\s*["'](?P<path>[^"']+)["']""",
    re.MULTILINE,
)
_BUFFALO_RESOURCE_RE = re.compile(
    r"""app\.Resource\s*\(\s*["'](?P<path>[^"']+)["']\s*,\s*(?P<resource>\w+)""",
    re.MULTILINE,
)

_BUFFALO_RESOURCE_VERBS = [
    ("GET",    ""),
    ("POST",   ""),
    ("GET",    "/{id}"),
    ("PUT",    "/{id}"),
    ("DELETE", "/{id}"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# NEW FRAMEWORK EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_starlette(content: str, file_path: str) -> list[dict]:
    """Extract Starlette Route/WebSocketRoute/Mount declarations."""
    lines   = content.splitlines()
    routes: list[dict] = []

    for m in _STARLETTE_ROUTE_RE.finditer(content):
        path    = m.group(1)
        handler = m.group(2) or ""
        methods_raw = m.group(3)
        if methods_raw:
            methods = [x.strip().strip("'\"") for x in methods_raw.split(",")]
        else:
            methods = ["GET"]
        ln = content[: m.start()].count("\n")
        for verb in methods:
            v = verb.upper().strip()
            if v:
                routes.append(_make_route(file_path, lines, ln, ln, v, path, handler, "high"))

    for m in _STARLETTE_WS_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, "WS", path, confidence="high"))

    for m in _STARLETTE_MOUNT_RE.finditer(content):
        path = m.group(1)
        ln   = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, "MOUNT", path, confidence="high"))

    return routes


def _extract_litestar(content: str, file_path: str) -> list[dict]:
    """Extract Litestar (v2) / Starlite (v1) decorator-based routes."""
    lines   = content.splitlines()
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "head": "HEAD", "options": "OPTIONS", "route": "ANY",
    }

    for m in _LITESTAR_VERB_RE.finditer(content):
        http_method = m.group("http_method").lower()
        path        = m.group("path")
        verb        = _METHOD_MAP.get(http_method, http_method.upper())
        ln          = content[: m.start()].count("\n")
        # Find the def line immediately after the decorator
        handler = ""
        for i in range(ln, min(ln + 5, len(lines))):
            def_m = _LITESTAR_DEF_RE.match(lines[i].strip())
            if def_m:
                handler = def_m.group(1)
                break
        end = _find_block_end_py(lines, ln)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, handler, "high"))

    return routes


def _extract_aiohttp(content: str, file_path: str) -> list[dict]:
    """Extract aiohttp route registrations (5 forms)."""
    lines   = content.splitlines()
    routes: list[dict] = []

    for pattern, method_group, path_group in [
        (_AIOHTTP_ADD_VERB_RE,    "method", "path"),
        (_AIOHTTP_DECORATOR_RE,   "method", "path"),
        (_AIOHTTP_WEB_VERB_RE,    "method", "path"),
    ]:
        for m in pattern.finditer(content):
            verb = m.group(method_group).upper()
            path = m.group(path_group)
            ln   = content[: m.start()].count("\n")
            routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    for pattern in (_AIOHTTP_ADD_ROUTE_RE, _AIOHTTP_WEB_ROUTE_RE):
        for m in pattern.finditer(content):
            verb = m.group("method").upper()
            path = m.group("path")
            ln   = content[: m.start()].count("\n")
            routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    return routes


def _extract_django_ninja(content: str, file_path: str) -> list[dict]:
    """Extract Django Ninja @api.get / @api.api_operation routes."""
    lines   = content.splitlines()
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT",
        "patch": "PATCH", "delete": "DELETE",
    }

    for m in _NINJA_VERB_RE.finditer(content):
        verb    = _METHOD_MAP.get(m.group("method").lower(), m.group("method").upper())
        path    = m.group("path")
        ln      = content[: m.start()].count("\n")
        handler = ""
        for i in range(ln, min(ln + 5, len(lines))):
            def_m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[i])
            if def_m:
                handler = def_m.group(1)
                break
        end = _find_block_end_py(lines, ln)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, handler, "high"))

    for m in _NINJA_API_OP_RE.finditer(content):
        methods_raw = m.group("methods")
        path        = m.group("path")
        ln          = content[: m.start()].count("\n")
        methods     = [x.strip().strip("'\"") for x in methods_raw.split(",")]
        for verb in methods:
            v = verb.upper().strip()
            if v:
                routes.append(_make_route(file_path, lines, ln, ln, v, path, confidence="high"))

    return routes


def _extract_sanic(content: str, file_path: str) -> list[dict]:
    """Extract Sanic route decorators and add_route calls.

    Sanic uses <param> angle-bracket syntax; we normalize to {param} first,
    then _normalize_path converts those to :param.
    """
    lines   = content.splitlines()
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "head": "HEAD", "options": "OPTIONS",
    }

    def _sanic_norm(path: str) -> str:
        return _SANIC_PARAM_RE.sub(r"{\1}", path)

    for m in _SANIC_VERB_RE.finditer(content):
        verb = _METHOD_MAP.get(m.group("method").lower(), m.group("method").upper())
        path = _sanic_norm(m.group("path"))
        ln   = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    for m in _SANIC_ROUTE_RE.finditer(content):
        path        = _sanic_norm(m.group("path"))
        methods_raw = m.group("methods")
        ln          = content[: m.start()].count("\n")
        for verb in [x.strip().strip("'\"") for x in methods_raw.split(",")]:
            v = verb.upper().strip()
            if v:
                routes.append(_make_route(file_path, lines, ln, ln, v, path, confidence="high"))

    for m in _SANIC_ADD_ROUTE_RE.finditer(content):
        path    = _sanic_norm(m.group("path"))
        handler = m.group(1)
        ln      = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, "ANY", path, handler, "high"))

    return routes


def _extract_tornado(content: str, file_path: str) -> list[dict]:
    """Extract Tornado routes from Application([...]) tuple lists."""
    lines   = content.splitlines()
    routes: list[dict] = []

    def _norm_tornado_path(raw: str) -> str:
        """Convert Tornado regex path to readable form."""
        # Strip surrounding quotes and leading r prefix
        path = raw.strip()
        if path.startswith(("r\"", "r'")):
            path = path[2:-1]
        elif path.startswith(('"', "'")):
            path = path[1:-1]
        # Named groups: (?P<name>...) -> {name}
        path = _TORNADO_NAMED_GROUP_RE.sub(r"{\1}", path)
        # Anonymous groups like (\d+) -> {id}
        path = re.sub(r"\([^)]+\)", "{id}", path)
        # Strip anchors
        path = path.lstrip("^").rstrip("$")
        return path or "/"

    # Find each Application([...]) block
    for app_m in _TORNADO_APP_RE.finditer(content):
        ln_app = content[: app_m.start()].count("\n")
        # Find the closing ] of the routes list
        bracket_start = content.find("[", app_m.end() - 1)
        if bracket_start == -1:
            continue
        depth = 0
        bracket_end = bracket_start
        for i, ch in enumerate(content[bracket_start:], bracket_start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    bracket_end = i
                    break
        routes_block = content[bracket_start: bracket_end + 1]

        for t in _TORNADO_TUPLE_RE.finditer(routes_block):
            raw_path    = t.group(1)
            handler_cls = t.group(2)
            path        = _norm_tornado_path(raw_path)
            ln          = content[: app_m.start() + (t.start())].count("\n")

            # Look for method definitions in the handler class
            cls_re  = re.compile(
                rf"""class\s+{re.escape(handler_cls)}\s*\(.*?\):(.+?)(?=\nclass\s|\Z)""",
                re.DOTALL,
            )
            cls_m = cls_re.search(content)
            if cls_m:
                methods = _TORNADO_HANDLER_METHOD_RE.findall(cls_m.group(1))
                for verb in methods:
                    routes.append(_make_route(file_path, lines, ln, ln, verb.upper(), path, handler_cls, "high"))
            if not cls_m or not _TORNADO_HANDLER_METHOD_RE.search(content[cls_m.start(): cls_m.end()] if cls_m else ""):
                # Emit at least an ANY route if we can't inspect the class
                if not (cls_m and _TORNADO_HANDLER_METHOD_RE.search(cls_m.group(0))):
                    routes.append(_make_route(file_path, lines, ln, ln, "ANY", path, handler_cls, "medium"))

    return routes


def _extract_graphql_sdl(content: str, file_path: str) -> list[dict]:
    """Extract Query/Mutation/Subscription fields from a GraphQL SDL schema."""
    lines   = content.splitlines()
    routes: list[dict] = []
    _OP_MAP = {"Query": "QUERY", "Mutation": "MUTATION", "Subscription": "SUBSCRIPTION"}

    for type_m in _GQL_TYPE_BLOCK_RE.finditer(content):
        op_type  = type_m.group(1)
        body     = type_m.group(2)
        verb     = _OP_MAP.get(op_type, op_type.upper())
        type_ln  = content[: type_m.start()].count("\n")

        # Find fields in the block
        for field_m in _GQL_FIELD_RE.finditer(body):
            field_name = field_m.group(1)
            path       = f"/graphql/{field_name}"
            # Compute approximate line number relative to file
            field_ln   = type_ln + body[: field_m.start()].count("\n") + 1
            field_ln   = min(field_ln, len(lines) - 1)
            routes.append(_make_route(file_path, lines, field_ln, field_ln, verb, path, field_name, "high"))

    return routes


def _extract_apollo_graphql(content: str, file_path: str) -> list[dict]:
    """Extract GraphQL operations from Apollo Server / GraphQL Yoga typeDefs."""
    lines  = content.splitlines()
    routes: list[dict] = []

    # Collect all SDL bodies from gql`` or const typeDefs = `...`
    sdl_bodies: list[tuple[str, int]] = []  # (body, start_line)
    for m in _APOLLO_TYPEDEF_RE.finditer(content):
        ln = content[: m.start()].count("\n")
        sdl_bodies.append((m.group(1), ln))
    for m in _APOLLO_GQL_TAG_RE.finditer(content):
        ln = content[: m.start()].count("\n")
        # Avoid duplicates already captured by TYPEDEF_RE
        already = any(abs(ln - existing_ln) <= 2 for _, existing_ln in sdl_bodies)
        if not already:
            sdl_bodies.append((m.group(1), ln))

    for sdl_body, base_ln in sdl_bodies:
        # Reuse the SDL extractor logic
        _OP_MAP = {"Query": "QUERY", "Mutation": "MUTATION", "Subscription": "SUBSCRIPTION"}
        for type_m in _GQL_TYPE_BLOCK_RE.finditer(sdl_body):
            op_type = type_m.group(1)
            body    = type_m.group(2)
            verb    = _OP_MAP.get(op_type, op_type.upper())
            for field_m in _GQL_FIELD_RE.finditer(body):
                field_name = field_m.group(1)
                path       = f"/graphql/{field_name}"
                field_ln   = base_ln + sdl_body[: type_m.start() + field_m.start()].count("\n")
                field_ln   = min(field_ln, len(lines) - 1)
                routes.append(_make_route(file_path, lines, field_ln, field_ln, verb, path, field_name, "high"))

    return routes


def _extract_strawberry(content: str, file_path: str) -> list[dict]:
    """Extract Strawberry GraphQL mutation/subscription/field operations."""
    lines  = content.splitlines()
    routes: list[dict] = []
    _OP_MAP = {"mutation": "MUTATION", "subscription": "SUBSCRIPTION"}

    for m in _STRAWBERRY_OP_RE.finditer(content):
        op_type    = m.group("op_type")
        field_name = m.group("name")
        verb       = _OP_MAP.get(op_type, op_type.upper())
        path       = f"/graphql/{field_name}"
        ln         = content[: m.start()].count("\n")
        end        = _find_block_end_py(lines, ln)
        routes.append(_make_route(file_path, lines, ln, end, verb, path, field_name, "high"))

    for m in _STRAWBERRY_FIELD_RE.finditer(content):
        field_name = m.group("name")
        path       = f"/graphql/{field_name}"
        ln         = content[: m.start()].count("\n")
        end        = _find_block_end_py(lines, ln)
        routes.append(_make_route(file_path, lines, ln, end, "QUERY", path, field_name, "high"))

    return routes


def _extract_graphene(content: str, file_path: str) -> list[dict]:
    """Extract Graphene Query/Mutation/Subscription fields and resolvers."""
    lines  = content.splitlines()
    routes: list[dict] = []

    # Find each class inheriting from graphene.ObjectType with a known op name
    for class_m in _GRAPHENE_CLASS_RE.finditer(content):
        op_type  = class_m.group(1)
        verb     = op_type.upper()  # QUERY, MUTATION, SUBSCRIPTION
        class_ln = content[: class_m.start()].count("\n")
        class_end = _find_block_end_py(lines, class_ln)
        class_body = "\n".join(lines[class_ln: class_end + 1])

        # Fields: foo = graphene.Field(...)
        for field_m in _GRAPHENE_FIELD_RE.finditer(class_body):
            field_name = field_m.group(1)
            path       = f"/graphql/{field_name}"
            field_ln   = class_ln + class_body[: field_m.start()].count("\n")
            field_ln   = min(field_ln, len(lines) - 1)
            routes.append(_make_route(file_path, lines, field_ln, field_ln, verb, path, field_name, "high"))

        # Resolvers: def resolve_foo(self, info)
        for res_m in _GRAPHENE_RESOLVER_RE.finditer(class_body):
            field_name = res_m.group(1)
            path       = f"/graphql/{field_name}"
            res_ln     = class_ln + class_body[: res_m.start()].count("\n")
            res_ln     = min(res_ln, len(lines) - 1)
            # Only add if not already present from field detection
            already = any(r["route_path"] == path for r in routes)
            if not already:
                routes.append(_make_route(file_path, lines, res_ln, res_ln, verb, path, field_name, "medium"))

    return routes


def _extract_feathers(content: str, file_path: str) -> list[dict]:
    """Extract Feathers.js service registrations and expand to HTTP routes."""
    lines  = content.splitlines()
    routes: list[dict] = []

    for m in _FEATHERS_USE_RE.finditer(content):
        service_path = m.group(1)
        service_cls  = m.group(2)
        ln           = content[: m.start()].count("\n")

        # Try to find which Feathers methods the service implements
        # by looking for the class definition
        cls_re = re.compile(
            rf"""class\s+{re.escape(service_cls)}\b.*?(?=\nclass\s|\Z)""",
            re.DOTALL,
        )
        cls_m = cls_re.search(content)
        service_methods: list[str] = []
        if cls_m:
            service_methods = re.findall(
                r"""async\s+(find|get|create|update|patch|remove)\s*\(""",
                cls_m.group(0),
            )

        if not service_methods:
            # Default: assume full CRUD
            service_methods = list(_FEATHERS_METHOD_MAP.keys())

        seen_paths: set[str] = set()
        for svc_method in service_methods:
            for http_verb, suffix in _FEATHERS_METHOD_MAP.get(svc_method, []):
                full_path = service_path.rstrip("/") + suffix
                key = f"{http_verb} {full_path}"
                if key not in seen_paths:
                    seen_paths.add(key)
                    routes.append(_make_route(
                        file_path, lines, ln, ln, http_verb, full_path, service_cls, "high",
                    ))

    return routes


def _extract_adonisjs(content: str, file_path: str) -> list[dict]:
    """Extract AdonisJS Route.get/post and router.get/post calls."""
    lines  = content.splitlines()
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT",
        "patch": "PATCH", "delete": "DELETE",
    }

    for pattern in (_ADONIS_ROUTE_RE, _ADONIS_DECO_RE):
        for m in pattern.finditer(content):
            verb = _METHOD_MAP.get(m.group("method").lower(), m.group("method").upper())
            path = m.group("path")
            ln   = content[: m.start()].count("\n")
            routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    return routes


def _extract_elysia(content: str, file_path: str) -> list[dict]:
    """Extract Elysia (Bun) HTTP method chains."""
    lines  = content.splitlines()
    routes: list[dict] = []
    _METHOD_MAP = {
        "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH",
        "delete": "DELETE", "options": "OPTIONS", "head": "HEAD", "all": "ANY",
    }

    for m in _ELYSIA_VERB_RE.finditer(content):
        verb = _METHOD_MAP.get(m.group("method").lower(), m.group("method").upper())
        path = m.group("path")
        ln   = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    return routes


def _extract_symfony(content: str, file_path: str) -> list[dict]:
    """Extract Symfony routes from PHP8 attributes, annotations, and YAML."""
    lines  = content.splitlines()
    routes: list[dict] = []

    def _parse_methods(raw: Optional[str]) -> list[str]:
        if not raw:
            return ["GET"]
        return [m.strip().strip("'\"") for m in re.split(r"[,\s]+", raw.strip()) if m.strip().strip("'\"")] or ["GET"]

    for m in _SYMFONY_ATTR_RE.finditer(content):
        path    = m.group("path")
        methods = _parse_methods(m.group("methods"))
        ln      = content[: m.start()].count("\n")
        for verb in methods:
            routes.append(_make_route(file_path, lines, ln, ln, verb.upper(), path, confidence="high"))

    for m in _SYMFONY_ANN_RE.finditer(content):
        path    = m.group("path")
        methods = _parse_methods(m.group("methods"))
        ln      = content[: m.start()].count("\n")
        for verb in methods:
            routes.append(_make_route(file_path, lines, ln, ln, verb.upper(), path, confidence="high"))

    for m in _SYMFONY_YAML_RE.finditer(content):
        path    = m.group("path").strip()
        methods = _parse_methods(m.group("methods"))
        ln      = content[: m.start()].count("\n")
        for verb in methods:
            routes.append(_make_route(file_path, lines, ln, ln, verb.upper(), path, confidence="medium"))

    return routes


def _extract_play(content: str, file_path: str) -> list[dict]:
    """Extract Play Framework (Scala) routes from a routes file.

    The routes file format is: METHOD  /path  controller.action(args)
    """
    lines  = content.splitlines()
    routes: list[dict] = []

    for m in _PLAY_ROUTE_RE.finditer(content):
        verb    = m.group(1).upper()
        path    = m.group(2)
        handler = m.group(3).strip()
        ln      = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, verb, path, handler, "high"))

    return routes


def _extract_buffalo(content: str, file_path: str) -> list[dict]:
    """Extract Buffalo (Go) route registrations and resource expansions."""
    lines  = content.splitlines()
    routes: list[dict] = []

    for m in _BUFFALO_VERB_RE.finditer(content):
        verb = m.group("method").upper()
        path = m.group("path")
        ln   = content[: m.start()].count("\n")
        routes.append(_make_route(file_path, lines, ln, ln, verb, path, confidence="high"))

    for m in _BUFFALO_RESOURCE_RE.finditer(content):
        base_path = m.group("path").rstrip("/")
        resource  = m.group("resource")
        ln        = content[: m.start()].count("\n")
        for verb, suffix in _BUFFALO_RESOURCE_VERBS:
            routes.append(_make_route(
                file_path, lines, ln, ln, verb,
                base_path + suffix, resource, "high",
            ))

    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# FILE-BASED ROUTE DETECTION (Next.js / Remix / SvelteKit / Nuxt)
# ═══════════════════════════════════════════════════════════════════════════════

def _rel_path(file_path: str, root_path: str) -> str:
    """Return file_path relative to root_path, with forward slashes."""
    try:
        rel = os.path.relpath(file_path, root_path)
    except ValueError:
        rel = file_path
    return rel.replace("\\", "/")


def _segments_to_url(segments: list[str]) -> str:
    """Convert path segments (already normalised) to a URL path."""
    parts: list[str] = []
    for seg in segments:
        norm = _path_from_next_app_segment(seg)
        if norm:
            parts.append(norm)
    url = "/" + "/".join(parts)
    return re.sub(r"/{2,}", "/", url)


def detect_file_based_routes(file_path: str, root_path: str) -> list[dict]:
    """Detect routes from file structure for Next.js / Remix / SvelteKit / Nuxt."""
    routes: list[dict] = []
    rel = _rel_path(file_path, root_path)
    lines: list[str] = []  # no content available — use dummy single line
    basename = os.path.basename(file_path)
    name_no_ext = re.sub(r"\.[^.]+$", "", basename)

    # ── Next.js App Router: app/.../**/route.{ts,js} ─────────────────────────
    _nextapp_m = re.match(r"app/(.*?)/route\.[jt]sx?$", rel)
    if _nextapp_m:
        segment_path = _nextapp_m.group(1)
        segments     = segment_path.split("/") if segment_path else []
        url_parts: list[str] = []
        for seg in segments:
            norm = _path_from_next_app_segment(seg)
            if norm:
                url_parts.append(norm)
        url = "/" + "/".join(url_parts) if url_parts else "/"
        url = re.sub(r"/{2,}", "/", url)
        # Emit a single ANY route; actual method splits come from content extraction
        lines = [""]
        routes.append(_make_route(file_path, lines, 0, 0, "ANY", url, confidence="medium"))
        return routes

    # ── Next.js Pages Router: pages/api/**/*.{ts,js} ─────────────────────────
    _nextpages_m = re.match(r"pages/api/(.*?)\.[jt]sx?$", rel)
    if _nextpages_m:
        segment_path = _nextpages_m.group(1)
        # index -> empty
        if segment_path == "index":
            url = "/api"
        else:
            parts = []
            for seg in segment_path.split("/"):
                if seg == "index":
                    continue
                norm = _normalize_path(seg)
                parts.append(norm)
            url = "/api/" + "/".join(parts)
        url = re.sub(r"/{2,}", "/", url)
        lines = [""]
        routes.append(_make_route(file_path, lines, 0, 0, "ANY", url, confidence="medium"))
        return routes

    # ── Remix: app/routes/**/*.{tsx,jsx,ts,js} ───────────────────────────────
    _remix_m = re.match(r"app/routes/(.*?)\.[jt]sx?$", rel)
    if _remix_m:
        route_id = _remix_m.group(1)
        # Remix flat-file convention: dots are path separators, $ is param prefix
        # e.g. api.users.$id  -> /api/users/:id
        # e.g. api/users/$id  -> /api/users/:id  (folder convention)
        if "/" in route_id:
            # Folder convention — already slash-separated
            segments = route_id.split("/")
        else:
            # Flat-file convention — dots as separators
            segments = route_id.split(".")

        parts: list[str] = []
        for seg in segments:
            if seg == "index" or seg == "_index":
                continue
            if seg.startswith("_"):
                # _layout files are not URL segments
                continue
            if seg.startswith("$"):
                parts.append(":" + seg[1:])
            else:
                parts.append(seg)

        url = "/" + "/".join(parts) if parts else "/"
        url = re.sub(r"/{2,}", "/", url)
        lines = [""]
        routes.append(_make_route(file_path, lines, 0, 0, "ANY", url, confidence="medium"))
        return routes

    # ── SvelteKit: src/routes/**/*+server.{ts,js} ────────────────────────────
    _svelte_m = re.match(r"src/routes/(.*?)\+server\.[jt]s$", rel)
    if _svelte_m:
        segment_path = _svelte_m.group(1).rstrip("/")
        parts = []
        for seg in segment_path.split("/"):
            if not seg:
                continue
            # SvelteKit: (group) -> transparent, [param] -> :param
            if re.fullmatch(r"\([^)]+\)", seg):
                continue
            norm = _normalize_path(seg)
            parts.append(norm)
        url = "/" + "/".join(parts) if parts else "/"
        url = re.sub(r"/{2,}", "/", url)
        # Actual methods come from content; here emit ANY
        lines = [""]
        routes.append(_make_route(file_path, lines, 0, 0, "ANY", url, confidence="medium"))
        return routes

    # ── Nuxt: server/api/**/*.{ts,js} ────────────────────────────────────────
    _nuxt_m = re.match(r"server/api/(.*?)(?:\.(get|post|put|patch|delete|options))?\.(?:ts|js)$", rel)
    if _nuxt_m:
        route_path_raw = _nuxt_m.group(1)
        method_suffix  = _nuxt_m.group(2)  # may be None
        parts: list[str] = []
        for seg in route_path_raw.split("/"):
            if not seg or seg == "index":
                continue
            norm = _normalize_path(seg)
            parts.append(norm)
        url = "/api/" + "/".join(parts) if parts else "/api"
        url = re.sub(r"/{2,}", "/", url)
        verb  = method_suffix.upper() if method_suffix else "ANY"
        lines = [""]
        routes.append(_make_route(file_path, lines, 0, 0, verb, url, confidence="medium"))
        return routes

    return routes


# Alias for backward compatibility / spec compliance
def extract_file_based_route(file_path: str, root_path: str) -> list[dict]:
    """For Next.js/Remix/SvelteKit/Nuxt — derive routes purely from file path."""
    return detect_file_based_routes(file_path, root_path)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

def extract_routes_for_file(content: str, file_path: str, ext: str) -> list[dict]:
    """Main entry point — dispatches to ALL applicable framework detectors for the
    extension and merges + deduplicates the results.

    A single file may contain patterns from multiple frameworks (e.g. a Fastify
    plugin with NestJS-style decorators in a monorepo), so all applicable
    extractors are run and the results merged.
    """
    import os as _os
    _is_play_routes_file = (
        _os.path.basename(file_path) == "routes"
        or file_path.endswith("/conf/routes")
    )
    if ext not in ROUTE_EXTS and not _is_play_routes_file:
        return []

    lines   = content.splitlines()
    routes: list[dict] = []

    # ── TypeScript / JavaScript ───────────────────────────────────────────────
    if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
        # Express (also covers Fastify/Hono verb calls with common receiver names)
        routes.extend(_extract_express(content, file_path, lines))
        # Fastify-specific: fastify.route({}) + fastify-named receivers
        routes.extend(_extract_fastify(content, file_path, lines))
        # Hono app.all()
        routes.extend(_extract_hono(content, file_path, lines))
        # Koa router (koa-router / @koa/router)
        if ("new Router" in content or "koa-router" in content
                or "koa/router" in content or "router.get(" in content
                or "router.post(" in content):
            routes.extend(_extract_koa(content, file_path, lines))
        # NestJS — only if @Controller is present
        if "@Controller" in content:
            routes.extend(_extract_nestjs(content, file_path, lines))
        # tRPC
        if "procedure" in content or ".query(" in content or ".mutation(" in content:
            routes.extend(_extract_trpc(content, file_path, lines))
        # Next.js App Router (route.ts / route.js)
        if "/route." in file_path or file_path.endswith("route.ts") or file_path.endswith("route.js"):
            routes.extend(_extract_nextjs_app_router_from_content(content, file_path, lines))
        # SvelteKit +server files
        if "+server." in file_path:
            routes.extend(_extract_sveltekit(content, file_path, lines))
        # Apollo Server / GraphQL Yoga (typeDefs with gql`...`)
        if "ApolloServer" in content or "createYoga" in content or "gql`" in content:
            routes.extend(_extract_apollo_graphql(content, file_path))
        # Feathers.js
        if "require('@feathersjs" in content or "from '@feathersjs" in content:
            routes.extend(_extract_feathers(content, file_path))
        # AdonisJS
        if "'@adonisjs" in content or 'from "@adonisjs' in content or "start/routes" in file_path:
            routes.extend(_extract_adonisjs(content, file_path))
        # Elysia (Bun)
        if "from 'elysia'" in content or 'from "elysia"' in content or "new Elysia" in content:
            routes.extend(_extract_elysia(content, file_path))

    # ── Python ───────────────────────────────────────────────────────────────
    elif ext == ".py":
        # FastAPI
        if ("fastapi" in content.lower() or "@app." in content or "@router." in content
                or "@api_router." in content):
            routes.extend(_extract_fastapi(content, file_path, lines))
        # Flask
        if ".route(" in content or "@app.route" in content or "@bp.route" in content:
            routes.extend(_extract_flask(content, file_path, lines))
        # Django
        if ("urlpatterns" in content or "path(" in content
                or "re_path(" in content or "url(" in content):
            routes.extend(_extract_django(content, file_path, lines))
        # Starlette
        if "from starlette" in content or "import starlette" in content:
            routes.extend(_extract_starlette(content, file_path))
        # Litestar (v2) / Starlite (v1)
        if "from litestar" in content or "from starlite" in content:
            routes.extend(_extract_litestar(content, file_path))
        # aiohttp
        if "from aiohttp" in content or "import aiohttp" in content or "web.Application" in content:
            routes.extend(_extract_aiohttp(content, file_path))
        # Django Ninja
        if "NinjaAPI" in content or "from ninja import" in content:
            routes.extend(_extract_django_ninja(content, file_path))
        # Sanic
        if "from sanic" in content or "Sanic(" in content:
            routes.extend(_extract_sanic(content, file_path))
        # Tornado
        if "tornado.web" in content or "RequestHandler" in content:
            routes.extend(_extract_tornado(content, file_path))
        # Strawberry GraphQL
        if "import strawberry" in content:
            routes.extend(_extract_strawberry(content, file_path))
        # Graphene
        if "import graphene" in content or "from graphene import" in content:
            routes.extend(_extract_graphene(content, file_path))

    # ── Go ────────────────────────────────────────────────────────────────────
    elif ext == ".go":
        # Gin
        if ("gin." in content or ".GET(" in content or ".POST(" in content
                or ".Group(" in content):
            routes.extend(_extract_gin(content, file_path, lines))
        # Gorilla Mux
        if "HandleFunc(" in content or "PathPrefix(" in content:
            routes.extend(_extract_gorilla(content, file_path, lines))
        # Echo
        if "echo.New" in content or "echo.Echo" in content or "e.GET(" in content:
            routes.extend(_extract_echo(content, file_path, lines))
        # Chi
        if "chi.NewRouter" in content or "chi.Router" in content:
            routes.extend(_extract_chi(content, file_path, lines))
        # Fiber
        if ("fiber.New" in content or "fiber.App" in content
                or "app.Get(" in content or "app.Post(" in content
                or ".Group(" in content and "fiber" in content):
            routes.extend(_extract_fiber(content, file_path, lines))
        # Beego
        if "beego.Router" in content or "beego.NewNamespace" in content:
            routes.extend(_extract_beego(content, file_path, lines))
        # Buffalo
        if "gobuffalo/buffalo" in content or "buffalo.New()" in content:
            routes.extend(_extract_buffalo(content, file_path))

    # ── Rust ─────────────────────────────────────────────────────────────────
    elif ext == ".rs":
        # Axum — .route() combined with any of the common method combinators
        if ".route(" in content:
            routes.extend(_extract_axum(content, file_path, lines))
        # Actix — procedural macros (#[get(...)]) or web:: API
        _ACTIX_INDICATORS = ("actix", "web::resource", "web::get", "web::post",
                             "web::put", "web::delete", "#[get(", "#[post(",
                             "#[put(", "#[patch(", "#[delete(")
        if any(ind in content for ind in _ACTIX_INDICATORS):
            routes.extend(_extract_actix(content, file_path, lines))
        # Rocket — attribute macros, but not also matched by Actix
        _ROCKET_INDICATORS = ("#[get(", "#[post(", "#[put(", "#[patch(", "#[delete(")
        if any(ind in content for ind in _ROCKET_INDICATORS):
            routes.extend(_extract_rocket(content, file_path, lines))

    # ── PHP ───────────────────────────────────────────────────────────────────
    elif ext == ".php":
        if "Route::" in content:
            routes.extend(_extract_laravel(content, file_path, lines))
        if "#[Route(" in content or "@Route(" in content or "Symfony" in content:
            routes.extend(_extract_symfony(content, file_path))

    # ── Kotlin ────────────────────────────────────────────────────────────────
    elif ext == ".kt":
        if ("routing {" in content or "install(Routing)" in content
                or 'get("/' in content or 'post("/' in content):
            routes.extend(_extract_ktor(content, file_path, lines))

    # ── Java ─────────────────────────────────────────────────────────────────
    elif ext == ".java":
        _JAXRS_INDICATORS = ("@Path(", "@GET", "@POST", "@PUT", "@DELETE", "@PATCH")
        if any(ind in content for ind in _JAXRS_INDICATORS):
            routes.extend(_extract_jaxrs(content, file_path, lines))

    # ── Ruby ─────────────────────────────────────────────────────────────────
    elif ext == ".rb":
        # Grape API framework (check first — Grape files also contain get/post)
        if "Grape::API" in content or "resource :" in content or "namespace :" in content:
            routes.extend(_extract_grape(content, file_path, lines))
        # Sinatra-style: get '/path' do — paths must start with '/' to avoid
        # matching Grape's get ':id' do style (which uses a bare symbol, not /path)
        _SINATRA_INDICATORS = ("get '/", "post '/", "put '/", "delete '/",
                               'get "/', 'post "/', 'put "/', 'delete "/')
        if any(ind in content for ind in _SINATRA_INDICATORS):
            routes.extend(_extract_sinatra(content, file_path, lines))

    # ── GraphQL SDL ───────────────────────────────────────────────────────────
    elif ext in (".graphql", ".gql"):
        routes.extend(_extract_graphql_sdl(content, file_path))

    # ── Scala / Play Framework routes file ───────────────────────────────────
    _is_play_routes = (
        ext == ".scala"
        or file_path.endswith("/conf/routes")
        or file_path.endswith("/routes")
        or os.path.basename(file_path) == "routes"
    )
    if _is_play_routes:
        routes.extend(_extract_play(content, file_path))

    return _dedup_routes(routes)


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS FOR NEW EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════

def _test_new_extractors() -> None:
    # ── Starlette ────────────────────────────────────────────────────────────
    code = '''
from starlette.routing import Route, WebSocketRoute
routes = [Route("/users", list_users, methods=["GET"]), Route("/users/{user_id:int}", get_user, methods=["GET", "PUT"])]
'''
    r = _extract_starlette(code, "app.py")
    assert any("GET /users" == x["name"] for x in r), f"Starlette GET /users: {[x['name'] for x in r]}"
    assert any("PUT" in x["name"] for x in r), f"Starlette PUT missing: {[x['name'] for x in r]}"
    print("[PASS] Starlette")

    # ── Litestar ─────────────────────────────────────────────────────────────
    code2 = '''
from litestar import get, post
@get("/items")
async def list_items() -> list: ...
@post("/items")
async def create_item() -> None: ...
'''
    r2 = _extract_litestar(code2, "app.py")
    assert len(r2) == 2, f"Litestar: expected 2 got {len(r2)}: {r2}"
    print("[PASS] Litestar")

    # ── aiohttp ───────────────────────────────────────────────────────────────
    code_aio = '''
from aiohttp import web
routes = web.RouteTableDef()
@routes.get("/users")
async def list_users(request): ...
app = web.Application()
app.add_routes([web.get("/health", health_handler)])
'''
    r_aio = _extract_aiohttp(code_aio, "app.py")
    assert len(r_aio) >= 1, f"aiohttp: {r_aio}"
    print("[PASS] aiohttp")

    # ── Django Ninja ──────────────────────────────────────────────────────────
    code_ninja = '''
from ninja import NinjaAPI
api = NinjaAPI()
@api.get("/users")
def list_users(request): ...
@api.post("/users")
def create_user(request): ...
'''
    r_ninja = _extract_django_ninja(code_ninja, "api.py")
    assert len(r_ninja) == 2, f"Django Ninja: {r_ninja}"
    print("[PASS] Django Ninja")

    # ── Sanic ─────────────────────────────────────────────────────────────────
    code_sanic = '''
from sanic import Sanic
app = Sanic("MyApp")
@app.get("/users")
async def list_users(request): ...
@app.post("/users")
async def create_user(request): ...
'''
    r_sanic = _extract_sanic(code_sanic, "app.py")
    assert len(r_sanic) == 2, f"Sanic: {r_sanic}"
    print("[PASS] Sanic")

    # ── Tornado ───────────────────────────────────────────────────────────────
    code3 = (
        "import tornado.web\n"
        "app = tornado.web.Application([\n"
        '    (r"/users", UserListHandler),\n'
        '    (r"/users/(\\d+)", UserHandler),\n'
        "])\n"
        "class UserListHandler(tornado.web.RequestHandler):\n"
        "    def get(self): pass\n"
        "    def post(self): pass\n"
        "class UserHandler(tornado.web.RequestHandler):\n"
        "    def get(self): pass\n"
    )
    r3 = _extract_tornado(code3, "app.py")
    assert len(r3) >= 2, f"Tornado: {r3}"
    print("[PASS] Tornado")

    # ── GraphQL SDL ───────────────────────────────────────────────────────────
    code4 = '''
type Query {
  users: [User]
  user(id: ID!): User
}
type Mutation {
  createUser(name: String!): User
}
'''
    r4 = _extract_graphql_sdl(code4, "schema.graphql")
    assert any("users" in x["name"] for x in r4), f"GraphQL SDL queries: {r4}"
    assert any("createUser" in x["name"] for x in r4), f"GraphQL SDL mutations: {r4}"
    print("[PASS] GraphQL SDL")

    # ── Apollo Server / GraphQL Yoga ──────────────────────────────────────────
    code_apollo = '''
const { ApolloServer, gql } = require('apollo-server');
const typeDefs = gql`
  type Query {
    books: [Book]
    book(id: ID!): Book
  }
  type Mutation {
    addBook(title: String): Book
  }
`;
'''
    r_apollo = _extract_apollo_graphql(code_apollo, "server.js")
    assert any("books" in x["name"] for x in r_apollo), f"Apollo: {r_apollo}"
    assert any("addBook" in x["name"] for x in r_apollo), f"Apollo mutation: {r_apollo}"
    print("[PASS] Apollo GraphQL")

    # ── Strawberry ────────────────────────────────────────────────────────────
    code_strawb = '''
import strawberry

@strawberry.type
class Query:
    @strawberry.field
    def users(self) -> list[str]:
        return []

@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_user(self, name: str) -> str:
        return name
'''
    r_strawb = _extract_strawberry(code_strawb, "schema.py")
    assert any("create_user" in x["name"] for x in r_strawb), f"Strawberry mutation: {r_strawb}"
    assert any("users" in x["name"] for x in r_strawb), f"Strawberry field: {r_strawb}"
    print("[PASS] Strawberry")

    # ── Graphene ──────────────────────────────────────────────────────────────
    code_graphene = '''
import graphene

class Query(graphene.ObjectType):
    users = graphene.List(lambda: UserType)
    user = graphene.Field(UserType, id=graphene.Int())

    def resolve_users(self, info):
        return []

class Mutation(graphene.ObjectType):
    create_user = graphene.Field(UserType)
'''
    r_graphene = _extract_graphene(code_graphene, "schema.py")
    assert any("users" in x["name"] for x in r_graphene), f"Graphene fields: {r_graphene}"
    print("[PASS] Graphene")

    # ── Feathers.js ───────────────────────────────────────────────────────────
    code_feathers = '''
const feathers = require('@feathersjs/feathers');
const app = feathers();
app.use('/messages', new MessageService());
class MessageService {
  async find(params) { return []; }
  async create(data) { return data; }
}
'''
    r_feathers = _extract_feathers(code_feathers, "app.js")
    assert len(r_feathers) >= 1, f"Feathers: {r_feathers}"
    print("[PASS] Feathers.js")

    # ── AdonisJS ──────────────────────────────────────────────────────────────
    code_adonis = '''
import Route from '@ioc:Adonis/Core/Route'
Route.get('/users', 'UsersController.index')
Route.post('/users', 'UsersController.store')
Route.delete('/users/:id', 'UsersController.destroy')
'''
    r_adonis = _extract_adonisjs(code_adonis, "start/routes.ts")
    assert len(r_adonis) == 3, f"AdonisJS: expected 3 got {len(r_adonis)}: {r_adonis}"
    print("[PASS] AdonisJS")

    # ── Elysia ────────────────────────────────────────────────────────────────
    code5 = '''
import { Elysia } from 'elysia'
const app = new Elysia()
  .get('/users', () => users)
  .post('/users', ({ body }) => create(body))
  .get('/users/:id', ({ params: { id } }) => findOne(id))
'''
    r5 = _extract_elysia(code5, "src/index.ts")
    assert len(r5) >= 3, f"Elysia: {r5}"
    print("[PASS] Elysia")

    # ── Symfony ───────────────────────────────────────────────────────────────
    code_symfony = '''
<?php
namespace App\\Controller;
use Symfony\\Component\\HttpFoundation\\Response;

class UserController {
    #[Route('/users', name: 'user_list', methods: ['GET'])]
    public function index(): Response {}

    #[Route('/users/{id}', methods: ['GET', 'PUT'])]
    public function show(int $id): Response {}
}
'''
    r_symfony = _extract_symfony(code_symfony, "src/Controller/UserController.php")
    assert any("/users" in x["route_path"] for x in r_symfony), f"Symfony: {r_symfony}"
    print("[PASS] Symfony")

    # ── Play Framework ────────────────────────────────────────────────────────
    code6 = '''GET     /users                  controllers.UserController.index()
POST    /users                  controllers.UserController.create()
GET     /users/:id              controllers.UserController.show(id: Long)
'''
    r6 = _extract_play(code6, "conf/routes")
    assert len(r6) == 3, f"Play: expected 3 got {len(r6)}: {r6}"
    print("[PASS] Play Framework")

    # ── Buffalo ───────────────────────────────────────────────────────────────
    code_buffalo = '''
package main
import "github.com/gobuffalo/buffalo"
func main() {
    app := buffalo.New(buffalo.Options{})
    app.GET("/users", UsersHandler)
    app.POST("/users", CreateUserHandler)
    app.Resource("/articles", ArticlesResource{})
}
'''
    r_buffalo = _extract_buffalo(code_buffalo, "actions/app.go")
    assert len(r_buffalo) >= 2, f"Buffalo verbs: {r_buffalo}"
    resource_routes = [x for x in r_buffalo if "articles" in x["route_path"]]
    assert len(resource_routes) >= 3, f"Buffalo resource: {resource_routes}"
    print("[PASS] Buffalo")

    print("\n=== All new route extractor tests PASSED ===")


if __name__ == "__main__":
    _test_new_extractors()
