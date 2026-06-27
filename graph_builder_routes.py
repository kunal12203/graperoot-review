"""
HTTP route/endpoint extractor for GrapeRoot Pro — Phase 2.

Supports 20 framework patterns across TypeScript/JavaScript, Python, Go, Rust, and PHP:

  TS/JS  : Express, Fastify, Hono, NestJS, tRPC, Next.js App Router,
            Next.js Pages Router, Remix, SvelteKit, Nuxt
  Python : FastAPI, Flask, Django
  Go     : Gin, Gorilla Mux, Echo, Chi
  Rust   : Axum, Actix
  PHP    : Laravel

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
    if ext not in ROUTE_EXTS:
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

    # ── PHP ───────────────────────────────────────────────────────────────────
    elif ext == ".php":
        if "Route::" in content:
            routes.extend(_extract_laravel(content, file_path, lines))

    return _dedup_routes(routes)
