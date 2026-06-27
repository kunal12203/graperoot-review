"""
Extended language symbol extractor for graph_builder (Phase 21).

Adds symbol extraction and route detection for:
  1. Elixir / Phoenix  (.ex, .exs)
  2. Swift / Vapor     (.swift)
  3. Dart / Flutter    (.dart)
  4. Groovy / Gradle   (.gradle, .groovy, .gradle.kts)

Public API
----------
LANG_EXT_EXTS              : set[str]   — all file extensions handled
ELIXIR_EXTS                : set[str]
SWIFT_EXTS                 : set[str]
DART_EXTS                  : set[str]
GROOVY_EXTS                : set[str]

supports_lang_extended(content, file_path, ext) -> bool
extract_lang_extended_symbols(content, file_path, ext) -> list[dict]
parse_lang_extended_imports(content, file_id, ext) -> list[dict]
get_lang_extended_summary(project_root) -> dict
"""

from __future__ import annotations

try:
    import hashlib
    import os
    import re
    from pathlib import Path
    from typing import Optional
except ImportError as _e:
    raise ImportError(f"graph_builder_lang_extended: stdlib import failed: {_e}")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ELIXIR_EXTS: set[str] = {".ex", ".exs"}
SWIFT_EXTS: set[str] = {".swift"}
DART_EXTS: set[str] = {".dart"}
GROOVY_EXTS: set[str] = {".gradle", ".groovy"}

LANG_EXT_EXTS: set[str] = ELIXIR_EXTS | SWIFT_EXTS | DART_EXTS | GROOVY_EXTS

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _body_hash(text: str) -> str:
    try:
        return hashlib.md5(text.encode()).hexdigest()[:8]
    except Exception:
        return "00000000"


def _name_keywords(name: str, extras: Optional[list] = None) -> list:
    try:
        tokens: list = []
        seen: set = set()

        def _add(w: str) -> None:
            w = w.lower().strip("_")
            if len(w) >= 2 and w not in seen:
                seen.add(w)
                tokens.append(w)

        _add(name)
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
        parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
        for p in parts.split():
            _add(p)
        for p in re.split(r"[_.\-:]", name):
            _add(p)
        if extras:
            for e in extras:
                _add(str(e))
        return tokens[:12]
    except Exception:
        return []


def _mk_sym(
    file_path: str,
    name: str,
    symbol_type: str,
    line_start: int,
    content_chunk: str,
    language: str,
    exported: bool = True,
    keywords: Optional[list] = None,
    extra: Optional[dict] = None,
) -> dict:
    sym: dict = {
        "id": f"{file_path}::{name}",
        "name": name,
        "symbol_type": symbol_type,
        "line_start": line_start + 1,  # 1-indexed for display
        "line_end": line_start + 2,
        "body_hash": _body_hash(content_chunk),
        "confidence": 0.85,
        "exported": exported,
        "keywords": keywords if keywords is not None else _name_keywords(name),
        "language": language,
    }
    if extra:
        sym.update(extra)
    return sym


def _strip_line_comments(line: str, comment_char: str) -> str:
    """Remove single-line comment suffixes, respecting strings naively."""
    idx = line.find(comment_char)
    if idx == -1:
        return line
    # If the comment char is inside a string, keep it — simple heuristic
    before = line[:idx]
    q_count = before.count('"') + before.count("'")
    if q_count % 2 == 0:
        return before
    return line


def _strip_block_comments(content: str) -> str:
    """Remove /* ... */ style block comments."""
    try:
        return re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    except Exception:
        return content


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def supports_lang_extended(content: str, file_path: str, ext: str) -> bool:
    try:
        if ext in ELIXIR_EXTS:
            return bool(re.search(r"\bdefmodule\b", content))
        if ext in SWIFT_EXTS:
            return bool(
                re.search(r"\bimport\s+(?:Foundation|Swift|Vapor|UIKit|SwiftUI)\b", content)
                or re.search(r"\bclass\s+\w+\s*[:{]", content)
                or re.search(r"\bstruct\s+\w+\s*[:{]", content)
            )
        if ext in DART_EXTS:
            return bool(
                re.search(r"import\s+'package:", content)
                or re.search(r"\bvoid\s+main\s*\(", content)
                or re.search(r"\bclass\s+\w+\s+extends\s+\w*Widget\b", content)
            )
        if ext in GROOVY_EXTS or file_path.endswith(".gradle.kts"):
            fp_lower = file_path.lower()
            return (
                "build.gradle" in fp_lower
                or "settings.gradle" in fp_lower
                or bool(re.search(r"\bplugins\s*\{", content))
                or bool(re.search(r"\bdependencies\s*\{", content))
                or ext == ".groovy"
            )
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Part 1: Elixir / Phoenix
# ---------------------------------------------------------------------------

# Patterns compiled once
_EX_MODULE = re.compile(
    r"^[ \t]*defmodule\s+([A-Z][A-Za-z0-9_.]*)\s+do\s*$", re.MULTILINE
)
_EX_DEF_PUB = re.compile(
    r"^[ \t]*def\s+([a-z_][a-zA-Z0-9_?!]*)\s*(?:\([^)]*\))?\s*(?:when\s+[^,\n]+)?\s*do\s*$",
    re.MULTILINE,
)
_EX_DEF_PUB_INLINE = re.compile(
    r"^[ \t]*def\s+([a-z_][a-zA-Z0-9_?!]*)\s*(?:\([^)]*\))?\s*(?:when\s+[^,\n]+)?\s*,\s*do:",
    re.MULTILINE,
)
_EX_DEF_PRIV = re.compile(
    r"^[ \t]*defp\s+([a-z_][a-zA-Z0-9_?!]*)\s*", re.MULTILINE
)
_EX_SCHEMA = re.compile(
    r'^[ \t]*schema\s+"([^"]+)"\s+do\s*$', re.MULTILINE
)
_EX_ASSOC = re.compile(
    r'^[ \t]*(belongs_to|has_many|has_one|many_to_many)\s+:([a-z_]+)\s*,\s*([A-Z][A-Za-z0-9_.]*)\b',
    re.MULTILINE,
)

# Phoenix router patterns
_EX_SCOPE = re.compile(r'^[ \t]*scope\s+"([^"]+)"', re.MULTILINE)
_EX_ROUTE = re.compile(
    r'^[ \t]*(get|post|put|patch|delete|options|head)\s+"([^"]+)"\s*,\s*([A-Z][A-Za-z0-9_.]*)\s*,\s*:([a-z_]+)',
    re.MULTILINE,
)
_EX_RESOURCES = re.compile(
    r'^[ \t]*resources\s+"([^"]+)"\s*,\s*([A-Z][A-Za-z0-9_.]*)',
    re.MULTILINE,
)
_EX_LIVE = re.compile(
    r'^[ \t]*live\s+"([^"]+)"\s*,\s*([A-Z][A-Za-z0-9_.]*)(?:\s*,\s*:([a-z_]+))?',
    re.MULTILINE,
)
_EX_USE_ROUTER = re.compile(r'\buse\b.*\bRouter\b', re.IGNORECASE)


def _strip_elixir_comments(lines: list) -> list:
    cleaned = []
    for line in lines:
        stripped = _strip_line_comments(line, "#")
        cleaned.append(stripped)
    return cleaned


def _extract_elixir(content: str, file_path: str) -> list:
    try:
        symbols: list = []
        lines = content.splitlines()
        clean_lines = _strip_elixir_comments(lines)
        clean_content = "\n".join(clean_lines)

        is_router = (
            "router.ex" in file_path.lower()
            or bool(_EX_USE_ROUTER.search(clean_content))
        )

        # --- Module definitions ---
        for m in _EX_MODULE.finditer(clean_content):
            mod_name = m.group(1)
            line_no = clean_content[: m.start()].count("\n")
            kws = _name_keywords(mod_name)
            if mod_name.endswith("Controller"):
                kws = list(dict.fromkeys(kws + ["controller"]))
            elif mod_name.endswith(("Repo", "Repository")):
                kws = list(dict.fromkeys(kws + ["repository"]))
            elif ".MixProject" in mod_name:
                kws = list(dict.fromkeys(kws + ["mix_project"]))
            elif "Live" in mod_name or mod_name.endswith("LiveView"):
                kws = list(dict.fromkeys(kws + ["live_view"]))
            elif mod_name.endswith(".Schema") or bool(
                re.search(r'\bschema\s+"', clean_content)
            ):
                kws = list(dict.fromkeys(kws + ["schema"]))
            symbols.append(
                _mk_sym(
                    file_path, mod_name, "use_case", line_no,
                    lines[line_no] if line_no < len(lines) else mod_name,
                    "elixir", exported=True, keywords=kws,
                )
            )

        # --- Ecto schema ---
        for m in _EX_SCHEMA.finditer(clean_content):
            table_name = m.group(1)
            line_no = clean_content[: m.start()].count("\n")
            symbols.append(
                _mk_sym(
                    file_path, table_name, "model", line_no,
                    lines[line_no] if line_no < len(lines) else table_name,
                    "elixir", exported=True,
                    keywords=_name_keywords(table_name, ["schema", "ecto"]),
                )
            )

        # --- Ecto associations ---
        for m in _EX_ASSOC.finditer(clean_content):
            relation, field_name, _related_mod = m.group(1), m.group(2), m.group(3)
            assoc_name = f"{relation}:{field_name}"
            line_no = clean_content[: m.start()].count("\n")
            symbols.append(
                _mk_sym(
                    file_path, assoc_name, "hook", line_no,
                    lines[line_no] if line_no < len(lines) else assoc_name,
                    "elixir", exported=False,
                    keywords=_name_keywords(assoc_name, [relation]),
                )
            )

        # --- Public functions ---
        seen_fns: set = set()
        for pat in (_EX_DEF_PUB, _EX_DEF_PUB_INLINE):
            for m in pat.finditer(clean_content):
                fn_name = m.group(1)
                if fn_name in seen_fns:
                    continue
                seen_fns.add(fn_name)
                line_no = clean_content[: m.start()].count("\n")
                symbols.append(
                    _mk_sym(
                        file_path, fn_name, "utility", line_no,
                        lines[line_no] if line_no < len(lines) else fn_name,
                        "elixir", exported=True,
                        keywords=_name_keywords(fn_name),
                    )
                )

        # --- Private functions ---
        for m in _EX_DEF_PRIV.finditer(clean_content):
            fn_name = m.group(1)
            if fn_name in seen_fns:
                continue
            seen_fns.add(fn_name)
            line_no = clean_content[: m.start()].count("\n")
            symbols.append(
                _mk_sym(
                    file_path, fn_name, "utility", line_no,
                    lines[line_no] if line_no < len(lines) else fn_name,
                    "elixir", exported=False,
                    keywords=_name_keywords(fn_name),
                )
            )

        # --- Phoenix routes ---
        if is_router:
            # Track scope prefixes by finding scope blocks
            scope_prefix = ""
            # Simple approach: find the last scope before each route
            scope_positions: list = []
            for sm in _EX_SCOPE.finditer(clean_content):
                scope_positions.append((sm.start(), sm.group(1).rstrip("/")))

            def _scope_at(pos: int) -> str:
                prefix = ""
                for spos, spath in scope_positions:
                    if spos < pos:
                        prefix = spath
                    else:
                        break
                return prefix

            # HTTP verb routes
            for m in _EX_ROUTE.finditer(clean_content):
                method, path, controller, action = (
                    m.group(1).upper(), m.group(2), m.group(3), m.group(4)
                )
                prefix = _scope_at(m.start())
                full_path = prefix + path
                route_name = f"{method} {full_path}"
                line_no = clean_content[: m.start()].count("\n")
                symbols.append(
                    _mk_sym(
                        file_path, route_name, "api_route", line_no,
                        lines[line_no] if line_no < len(lines) else route_name,
                        "elixir", exported=True,
                        keywords=_name_keywords(route_name, [method.lower(), "route"]),
                        extra={
                            "route_method": method,
                            "route_path": full_path,
                            "controller": controller,
                            "action": action,
                        },
                    )
                )

            # Resources routes → expand to 7 standard RESTful routes
            for m in _EX_RESOURCES.finditer(clean_content):
                res_path, controller = m.group(1), m.group(2)
                prefix = _scope_at(m.start())
                base = prefix + res_path.rstrip("/")
                line_no = clean_content[: m.start()].count("\n")
                restful = [
                    ("GET", base, "index"),
                    ("GET", f"{base}/:id", "show"),
                    ("GET", f"{base}/new", "new"),
                    ("POST", base, "create"),
                    ("GET", f"{base}/:id/edit", "edit"),
                    ("PUT", f"{base}/:id", "update"),
                    ("PATCH", f"{base}/:id", "update"),
                    ("DELETE", f"{base}/:id", "delete"),
                ]
                for http_method, rpath, action in restful:
                    route_name = f"{http_method} {rpath}"
                    symbols.append(
                        _mk_sym(
                            file_path, route_name, "api_route", line_no,
                            route_name,
                            "elixir", exported=True,
                            keywords=_name_keywords(route_name, [http_method.lower(), "route"]),
                            extra={
                                "route_method": http_method,
                                "route_path": rpath,
                                "controller": controller,
                                "action": action,
                            },
                        )
                    )

            # LiveView routes
            for m in _EX_LIVE.finditer(clean_content):
                path = m.group(1)
                _module = m.group(2)
                prefix = _scope_at(m.start())
                full_path = prefix + path
                route_name = f"LIVE {full_path}"
                line_no = clean_content[: m.start()].count("\n")
                symbols.append(
                    _mk_sym(
                        file_path, route_name, "api_route", line_no,
                        lines[line_no] if line_no < len(lines) else route_name,
                        "elixir", exported=True,
                        keywords=_name_keywords(route_name, ["live", "liveview", "route"]),
                        extra={
                            "route_method": "LIVE",
                            "route_path": full_path,
                            "controller": _module,
                            "action": m.group(3) or "index",
                        },
                    )
                )

        return symbols
    except Exception:
        return []


def _parse_elixir_imports(content: str, file_id: str) -> list:
    try:
        edges: list = []
        # belongs_to/has_many → references edge to related module
        for m in _EX_ASSOC.finditer(content):
            related_mod = m.group(3)
            edges.append({
                "from": file_id,
                "to": related_mod,
                "type": "references",
                "label": m.group(1),
            })
        # Phoenix routes → references to controller modules
        for m in _EX_ROUTE.finditer(content):
            controller = m.group(3)
            edges.append({
                "from": file_id,
                "to": controller,
                "type": "references",
                "label": "routes_to",
            })
        for m in _EX_RESOURCES.finditer(content):
            controller = m.group(2)
            edges.append({
                "from": file_id,
                "to": controller,
                "type": "references",
                "label": "resources",
            })
        return edges
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Part 2: Swift / Vapor
# ---------------------------------------------------------------------------

_SW_CLASS = re.compile(
    r"^[ \t]*(?:(?:open|public|internal|fileprivate|private)\s+)?(?:final\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*:\s*([^{]+))?",
    re.MULTILINE,
)
_SW_STRUCT = re.compile(
    r"^[ \t]*(?:(?:open|public|internal|fileprivate|private)\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*:\s*([^{]+))?",
    re.MULTILINE,
)
_SW_PROTOCOL = re.compile(
    r"^[ \t]*(?:(?:public|internal|fileprivate|private)\s+)?protocol\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_SW_FUNC = re.compile(
    r"^[ \t]*(?:(?:override|static|class|mutating|public|internal|private|fileprivate|final|async)\s+)*func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_SW_ENUM = re.compile(
    r"^[ \t]*(?:(?:public|internal|fileprivate|private)\s+)?(?:indirect\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_SW_VIEW = re.compile(
    r"^[ \t]*struct\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^{]*\bView\b",
    re.MULTILINE,
)
_SW_VAPOR_ROUTE = re.compile(
    r"([a-zA-Z_]\w*)\.(?:get|post|put|patch|delete|on)\s*\(([^)]+)\)",
    re.MULTILINE,
)
_SW_ROUTE_STRING = re.compile(r'["\']([^"\']+)["\']')
_SW_ROUTE_COLLECTION = re.compile(
    r"\bclass\s+(\w+)\s*:\s*[^{]*RouteCollection\b",
    re.MULTILINE,
)
_SW_IMPORT = re.compile(r"^import\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _strip_swift_comments(content: str) -> str:
    try:
        # Strip block comments first
        content = _strip_block_comments(content)
        # Strip line comments
        lines = content.splitlines()
        cleaned = []
        for line in lines:
            cleaned.append(_strip_line_comments(line, "//"))
        return "\n".join(cleaned)
    except Exception:
        return content


def _extract_swift(content: str, file_path: str) -> list:
    try:
        symbols: list = []
        clean = _strip_swift_comments(content)
        lines = clean.splitlines()

        def _lno(pos: int) -> int:
            return clean[:pos].count("\n")

        # SwiftUI Views (check before generic struct)
        view_names: set = set()
        for m in _SW_VIEW.finditer(clean):
            name = m.group(1)
            view_names.add(name)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, name, "use_case", line_no,
                    lines[line_no] if line_no < len(lines) else name,
                    "swift", exported=True,
                    keywords=_name_keywords(name, ["swiftui", "view"]),
                )
            )

        # RouteCollection conformance
        for m in _SW_ROUTE_COLLECTION.finditer(clean):
            name = m.group(1)
            line_no = _lno(m.start())
            if name not in view_names:
                symbols.append(
                    _mk_sym(
                        file_path, name, "use_case", line_no,
                        lines[line_no] if line_no < len(lines) else name,
                        "swift", exported=True,
                        keywords=_name_keywords(name, ["vapor", "route_collection"]),
                    )
                )

        # Classes
        class_names: set = set()
        for m in _SW_CLASS.finditer(clean):
            name = m.group(1)
            inherits = m.group(2) or ""
            line_no = _lno(m.start())
            # Determine type
            if name.endswith(("Model", "Entity", "DTO")):
                stype = "model"
            elif inherits.strip():
                stype = "use_case"
            else:
                stype = "utility"
            # Check exported
            prefix = clean[max(0, m.start() - 30): m.start()]
            exported = "public" in m.group(0) or "open" in m.group(0)
            kws = _name_keywords(name)
            if name not in view_names:
                symbols.append(
                    _mk_sym(
                        file_path, name, stype, line_no,
                        lines[line_no] if line_no < len(lines) else name,
                        "swift", exported=exported, keywords=kws,
                    )
                )
            class_names.add(name)

        # Structs
        for m in _SW_STRUCT.finditer(clean):
            name = m.group(1)
            conforms = m.group(2) or ""
            line_no = _lno(m.start())
            if name in view_names:
                continue
            # Model if conforms to Codable/Decodable/Model
            if any(t in conforms for t in ("Codable", "Decodable", "Model")):
                stype = "model"
            else:
                stype = "utility"
            exported = "public" in m.group(0) or "open" in m.group(0)
            symbols.append(
                _mk_sym(
                    file_path, name, stype, line_no,
                    lines[line_no] if line_no < len(lines) else name,
                    "swift", exported=exported,
                    keywords=_name_keywords(name),
                )
            )

        # Protocols
        for m in _SW_PROTOCOL.finditer(clean):
            name = m.group(1)
            line_no = _lno(m.start())
            exported = "public" in m.group(0)
            symbols.append(
                _mk_sym(
                    file_path, name, "use_case", line_no,
                    lines[line_no] if line_no < len(lines) else name,
                    "swift", exported=exported,
                    keywords=_name_keywords(name, ["protocol"]),
                )
            )

        # Enums
        for m in _SW_ENUM.finditer(clean):
            name = m.group(1)
            line_no = _lno(m.start())
            exported = "public" in m.group(0)
            symbols.append(
                _mk_sym(
                    file_path, name, "model", line_no,
                    lines[line_no] if line_no < len(lines) else name,
                    "swift", exported=exported,
                    keywords=_name_keywords(name, ["enum"]),
                )
            )

        # Functions
        seen_funcs: set = set()
        for m in _SW_FUNC.finditer(clean):
            name = m.group(1)
            if name in seen_funcs:
                continue
            seen_funcs.add(name)
            full_match = m.group(0)
            line_no = _lno(m.start())
            exported = "public" in full_match or "open" in full_match
            symbols.append(
                _mk_sym(
                    file_path, name, "utility", line_no,
                    lines[line_no] if line_no < len(lines) else name,
                    "swift", exported=exported,
                    keywords=_name_keywords(name),
                )
            )

        # Vapor direct route registrations
        vapor_methods = {"get", "post", "put", "patch", "delete", "on"}
        for m in _SW_VAPOR_ROUTE.finditer(clean):
            method_str = ""
            for part in m.group(0).split("."):
                if part.split("(")[0].lower() in vapor_methods:
                    method_str = part.split("(")[0].upper()
                    break
            if not method_str:
                continue
            args = m.group(2)
            str_m = _SW_ROUTE_STRING.search(args)
            if not str_m:
                continue
            path = str_m.group(1)
            if not path.startswith("/"):
                path = "/" + path
            route_name = f"{method_str} {path}"
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, route_name, "api_route", line_no,
                    lines[line_no] if line_no < len(lines) else route_name,
                    "swift", exported=True,
                    keywords=_name_keywords(route_name, [method_str.lower(), "vapor", "route"]),
                    extra={"route_method": method_str, "route_path": path},
                )
            )

        return symbols
    except Exception:
        return []


def _parse_swift_imports(content: str, file_id: str) -> list:
    try:
        edges: list = []
        for m in _SW_IMPORT.finditer(content):
            module = m.group(1)
            edges.append({
                "from": file_id,
                "to": module,
                "type": "references",
                "label": "import",
            })
        return edges
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Part 3: Dart / Flutter
# ---------------------------------------------------------------------------

_DART_WIDGET = re.compile(
    r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s+extends\s+(StatefulWidget|StatelessWidget)\b",
    re.MULTILINE,
)
_DART_BLOC = re.compile(
    r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s+extends\s+(Cubit|Bloc)<",
    re.MULTILINE,
)
_DART_CLASS = re.compile(
    r"^(?:abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)(?:<[^{]*>)?(?:\s+extends\s+\S+)?(?:\s+with\s+[^{]+)?(?:\s+implements\s+[^{]+)?\s*\{",
    re.MULTILINE,
)
_DART_ASYNC_FN = re.compile(
    r"^(?:Future<[^>]+>|Future)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*async\s*\{",
    re.MULTILINE,
)
_DART_PROVIDER = re.compile(
    r"\bfinal\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(Provider|FutureProvider|StreamProvider|StateProvider|StateNotifierProvider)<",
    re.MULTILINE,
)
_DART_GO_ROUTE = re.compile(
    r"GoRoute\s*\(\s*path\s*:\s*'([^']*)'",
    re.MULTILINE,
)
_DART_IMPORT = re.compile(
    r"^import\s+'package:([^/]+)/",
    re.MULTILINE,
)


def _strip_dart_comments(lines: list) -> list:
    cleaned = []
    for line in lines:
        cleaned.append(_strip_line_comments(line, "//"))
    return cleaned


def _extract_dart(content: str, file_path: str) -> list:
    try:
        symbols: list = []
        lines = content.splitlines()
        clean_lines = _strip_dart_comments(lines)
        clean = "\n".join(clean_lines)

        def _lno(pos: int) -> int:
            return clean[:pos].count("\n")

        seen_classes: set = set()

        # Widgets
        for m in _DART_WIDGET.finditer(clean):
            name = m.group(1)
            seen_classes.add(name)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, name, "use_case", line_no,
                    clean_lines[line_no] if line_no < len(clean_lines) else name,
                    "dart", exported=True,
                    keywords=_name_keywords(name, ["flutter", "widget"]),
                )
            )

        # Cubit/Bloc
        for m in _DART_BLOC.finditer(clean):
            name = m.group(1)
            seen_classes.add(name)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, name, "use_case", line_no,
                    clean_lines[line_no] if line_no < len(clean_lines) else name,
                    "dart", exported=True,
                    keywords=_name_keywords(name, ["state_management", m.group(2).lower()]),
                )
            )

        # Generic classes
        for m in _DART_CLASS.finditer(clean):
            name = m.group(1)
            if name in seen_classes:
                continue
            seen_classes.add(name)
            line_no = _lno(m.start())
            # Determine type by naming convention
            if any(name.endswith(s) for s in ("Service", "Repository", "Manager", "UseCase")):
                stype = "use_case"
            elif any(name.endswith(s) for s in ("Model", "DTO", "Entity")):
                stype = "model"
            else:
                stype = "use_case"
            symbols.append(
                _mk_sym(
                    file_path, name, stype, line_no,
                    clean_lines[line_no] if line_no < len(clean_lines) else name,
                    "dart", exported=True,
                    keywords=_name_keywords(name),
                )
            )

        # Async functions
        seen_fns: set = set()
        for m in _DART_ASYNC_FN.finditer(clean):
            name = m.group(1)
            if name in seen_fns:
                continue
            seen_fns.add(name)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, name, "utility", line_no,
                    clean_lines[line_no] if line_no < len(clean_lines) else name,
                    "dart", exported=True,
                    keywords=_name_keywords(name, ["async"]),
                )
            )

        # Riverpod providers
        for m in _DART_PROVIDER.finditer(clean):
            name = m.group(1)
            provider_type = m.group(2)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, name, "hook", line_no,
                    clean_lines[line_no] if line_no < len(clean_lines) else name,
                    "dart", exported=True,
                    keywords=_name_keywords(name, ["riverpod", provider_type.lower()]),
                )
            )

        # go_router routes
        for m in _DART_GO_ROUTE.finditer(clean):
            path = m.group(1)
            route_name = f"GET {path}"
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, route_name, "api_route", line_no,
                    clean_lines[line_no] if line_no < len(clean_lines) else route_name,
                    "dart", exported=True,
                    keywords=_name_keywords(route_name, ["go_router", "route"]),
                    extra={"route_method": "GET", "route_path": path},
                )
            )

        return symbols
    except Exception:
        return []


def _parse_dart_imports(content: str, file_id: str) -> list:
    try:
        edges: list = []
        for m in _DART_IMPORT.finditer(content):
            pkg = m.group(1)
            edges.append({
                "from": file_id,
                "to": pkg,
                "type": "references",
                "label": "import",
            })
        return edges
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Part 4: Groovy / Gradle
# ---------------------------------------------------------------------------

_GR_PLUGIN_ID = re.compile(
    r"""^\s*id\s+['"]([A-Za-z0-9._\-]+)['"]\s*(?:version\s+['"]([^'"]+)['"])?""",
    re.MULTILINE,
)
_GR_APPLY_PLUGIN = re.compile(
    r"""^\s*apply\s+plugin\s*:\s*['"]([A-Za-z0-9._\-]+)['"]""",
    re.MULTILINE,
)
_GR_DEP = re.compile(
    r"""^\s*(implementation|testImplementation|compileOnly|runtimeOnly|api|annotationProcessor|kapt|provided)\s+['"]([A-Za-z0-9._\-]+):([A-Za-z0-9._\-]+)(?::([^'"]+))?['"]""",
    re.MULTILINE,
)
_GR_TASK_LEGACY = re.compile(
    r"""^\s*task\s+([A-Za-z][A-Za-z0-9_]*)\s*(?:\(\s*type\s*:\s*([A-Za-z][A-Za-z0-9_.]*)\s*\))?\s*\{""",
    re.MULTILINE,
)
_GR_TASK_REGISTER = re.compile(
    r"""^\s*tasks\.register\s*\(\s*['"]([A-Za-z][A-Za-z0-9_\-]*)['"]""",
    re.MULTILINE,
)
_GR_PROJECT_PROP = re.compile(
    r"""^\s*(group|version|description)\s*=\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_GR_ROOT_PROJECT = re.compile(
    r"""^\s*rootProject\.name\s*=\s*['"]([A-Za-z0-9._\-]+)['"]""",
    re.MULTILINE,
)
_GR_INCLUDE = re.compile(
    r"""^\s*include\s+(['"][A-Za-z0-9:._\-]+['"](?:\s*,\s*['"][A-Za-z0-9:._\-]+['"])*)\s*$""",
    re.MULTILINE,
)
_GR_INCLUDE_ITEM = re.compile(r"""['"]([A-Za-z0-9:._\-]+)['"]""")


def _strip_groovy_comments(content: str) -> str:
    try:
        content = _strip_block_comments(content)
        lines = content.splitlines()
        cleaned = []
        for line in lines:
            cleaned.append(_strip_line_comments(line, "//"))
        return "\n".join(cleaned)
    except Exception:
        return content


def _extract_groovy(content: str, file_path: str) -> list:
    try:
        symbols: list = []
        clean = _strip_groovy_comments(content)
        lines = clean.splitlines()

        def _lno(pos: int) -> int:
            return clean[:pos].count("\n")

        # Plugins (id form)
        for m in _GR_PLUGIN_ID.finditer(clean):
            plugin_id = m.group(1)
            version = m.group(2) or ""
            line_no = _lno(m.start())
            kws = _name_keywords(plugin_id, ["plugin"] + ([version] if version else []))
            symbols.append(
                _mk_sym(
                    file_path, plugin_id, "utility", line_no,
                    lines[line_no] if line_no < len(lines) else plugin_id,
                    "groovy", exported=True, keywords=kws,
                )
            )

        # Apply plugin (legacy)
        for m in _GR_APPLY_PLUGIN.finditer(clean):
            plugin_id = m.group(1)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, plugin_id, "utility", line_no,
                    lines[line_no] if line_no < len(lines) else plugin_id,
                    "groovy", exported=True,
                    keywords=_name_keywords(plugin_id, ["plugin", "apply"]),
                )
            )

        # Dependencies
        for m in _GR_DEP.finditer(clean):
            scope = m.group(1)
            group = m.group(2)
            artifact = m.group(3)
            version = m.group(4) or ""
            dep_name = f"{group}:{artifact}"
            line_no = _lno(m.start())
            kws = _name_keywords(dep_name, ["dependency", scope] + ([version] if version else []))
            symbols.append(
                _mk_sym(
                    file_path, dep_name, "model", line_no,
                    lines[line_no] if line_no < len(lines) else dep_name,
                    "groovy", exported=False, keywords=kws,
                )
            )

        # Tasks (legacy form)
        for m in _GR_TASK_LEGACY.finditer(clean):
            task_name = m.group(1)
            task_type = m.group(2) or ""
            line_no = _lno(m.start())
            kws = _name_keywords(task_name, ["task"] + ([task_type] if task_type else []))
            symbols.append(
                _mk_sym(
                    file_path, task_name, "use_case", line_no,
                    lines[line_no] if line_no < len(lines) else task_name,
                    "groovy", exported=True, keywords=kws,
                )
            )

        # Tasks (register form)
        for m in _GR_TASK_REGISTER.finditer(clean):
            task_name = m.group(1)
            line_no = _lno(m.start())
            symbols.append(
                _mk_sym(
                    file_path, task_name, "use_case", line_no,
                    lines[line_no] if line_no < len(lines) else task_name,
                    "groovy", exported=True,
                    keywords=_name_keywords(task_name, ["task", "register"]),
                )
            )

        # Project properties (attach as keyword on a module symbol)
        props: dict = {}
        for m in _GR_PROJECT_PROP.finditer(clean):
            props[m.group(1)] = m.group(2)

        # Root project name (settings.gradle)
        for m in _GR_ROOT_PROJECT.finditer(clean):
            proj_name = m.group(1)
            line_no = _lno(m.start())
            kws = _name_keywords(proj_name, ["gradle", "root_project"])
            if props:
                kws = list(dict.fromkeys(kws + list(props.values())))
            symbols.append(
                _mk_sym(
                    file_path, f"project:{proj_name}", "use_case", line_no,
                    lines[line_no] if line_no < len(lines) else proj_name,
                    "groovy", exported=True, keywords=kws[:12],
                )
            )

        # Include (settings.gradle)
        for m in _GR_INCLUDE.finditer(clean):
            line_no = _lno(m.start())
            for im in _GR_INCLUDE_ITEM.finditer(m.group(1)):
                mod_path = im.group(1)
                symbols.append(
                    _mk_sym(
                        file_path, mod_path, "utility", line_no,
                        lines[line_no] if line_no < len(lines) else mod_path,
                        "groovy", exported=True,
                        keywords=_name_keywords(mod_path, ["gradle", "include"]),
                    )
                )

        return symbols
    except Exception:
        return []


def _parse_groovy_imports(content: str, file_id: str) -> list:
    try:
        edges: list = []
        # implementation "group:artifact" → references if artifact exists as symbol
        for m in _GR_DEP.finditer(content):
            dep_name = f"{m.group(2)}:{m.group(3)}"
            edges.append({
                "from": file_id,
                "to": dep_name,
                "type": "references",
                "label": m.group(1),
            })
        return edges
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API dispatchers
# ---------------------------------------------------------------------------


def extract_lang_extended_symbols(content: str, file_path: str, ext: str) -> list:
    """Return list of symbol dicts for the given file, or [] on any error."""
    if not content or not content.strip():
        return []
    try:
        # Handle .gradle.kts by checking path suffix first
        if file_path.endswith(".gradle.kts"):
            return _extract_groovy(content, file_path)
        if ext in ELIXIR_EXTS:
            return _extract_elixir(content, file_path)
        if ext in SWIFT_EXTS:
            return _extract_swift(content, file_path)
        if ext in DART_EXTS:
            return _extract_dart(content, file_path)
        if ext in GROOVY_EXTS:
            return _extract_groovy(content, file_path)
    except Exception:
        pass
    return []


def parse_lang_extended_imports(content: str, file_id: str, ext: str) -> list:
    """Return list of edge dicts (from/to/type/label), or [] on any error."""
    if not content or not content.strip():
        return []
    try:
        if file_id.endswith(".gradle.kts"):
            return _parse_groovy_imports(content, file_id)
        if ext in ELIXIR_EXTS:
            return _parse_elixir_imports(content, file_id)
        if ext in SWIFT_EXTS:
            return _parse_swift_imports(content, file_id)
        if ext in DART_EXTS:
            return _parse_dart_imports(content, file_id)
        if ext in GROOVY_EXTS:
            return _parse_groovy_imports(content, file_id)
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# MCP helper
# ---------------------------------------------------------------------------


def get_lang_extended_summary(project_root: str) -> dict:
    """Walk project, extract symbols for all 4 language families, return summary."""
    try:
        all_symbols: list = []
        counts: dict = {"elixir": 0, "swift": 0, "dart": 0, "groovy": 0}

        root = Path(project_root)
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".dart_tool",
            "build", ".build", ".gradle", "DerivedData", "_build", "deps",
        }

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip dirs in-place
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                _, raw_ext = os.path.splitext(fname)
                ext = raw_ext.lower()
                is_kts = fpath.endswith(".gradle.kts")
                if ext not in LANG_EXT_EXTS and not is_kts:
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    if not supports_lang_extended(content, fpath, ext):
                        continue
                    syms = extract_lang_extended_symbols(content, fpath, ext)
                    for s in syms:
                        lang = s.get("language", "")
                        if lang in counts:
                            counts[lang] += 1
                    all_symbols.extend(syms)
                except Exception:
                    continue

        total = len(all_symbols)
        top20 = all_symbols[:20]
        return {
            "ok": True,
            "total": total,
            "by_language": counts,
            "symbols": top20,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "total": 0, "by_language": {}, "symbols": []}


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    def _run_tests() -> None:
        # --- Elixir ---
        ex_code = '''
defmodule MyApp.User do
  use Ecto.Schema
  schema "users" do
    field :email, :string
    field :name, :string
    belongs_to :team, MyApp.Team
    has_many :orders, MyApp.Order
  end
  def changeset(user, attrs) do
    user |> cast(attrs, [:email, :name])
  end
  defp validate_email(changeset) do
    changeset
  end
end
'''
        syms = extract_lang_extended_symbols(ex_code, "lib/my_app/user.ex", ".ex")
        assert any(s["name"] == "MyApp.User" for s in syms), (
            f"Elixir module: {[s['name'] for s in syms]}"
        )
        assert any(s["symbol_type"] == "model" and "users" in s["name"] for s in syms), (
            f"Elixir schema: {syms}"
        )
        print("[PASS] Elixir schema + module")

        # Phoenix router
        router_code = '''
defmodule MyAppWeb.Router do
  use MyAppWeb, :router
  scope "/api", MyAppWeb do
    pipe_through :api
    get "/users", UserController, :index
    post "/users", UserController, :create
    resources "/posts", PostController
    live "/dashboard", DashboardLive.Index
  end
end
'''
        routes = extract_lang_extended_symbols(router_code, "lib/my_app_web/router.ex", ".ex")
        route_syms = [s for s in routes if s["symbol_type"] == "api_route"]
        assert any("GET /api/users" in s["name"] for s in route_syms), (
            f"Phoenix routes: {[s['name'] for s in route_syms]}"
        )
        # resources should expand to multiple routes
        post_routes = [s for s in route_syms if "posts" in s["name"]]
        assert len(post_routes) >= 4, (
            f"Phoenix resources expansion: {[s['name'] for s in post_routes]}"
        )
        print("[PASS] Phoenix routes + resources expansion")

        # LiveView
        assert any(
            "LIVE" in s["name"] and "dashboard" in s["name"].lower() for s in route_syms
        ), f"Phoenix live: {[s['name'] for s in route_syms]}"
        print("[PASS] Phoenix LiveView route")

        # --- Swift ---
        swift_code = '''
import Foundation
import Vapor

public struct UserController: RouteCollection {
    func boot(routes: RoutesBuilder) throws {
        let users = routes.grouped("users")
        users.get(use: index)
        users.post(use: create)
        users.group(":userID") { user in
            user.get(use: show)
            user.put(use: update)
            user.delete(use: delete)
        }
    }
    func index(req: Request) async throws -> [User] { [] }
}

struct User: Model, Content {
    static let schema = "users"
    var id: UUID?
    var name: String
}
'''
        swift_syms = extract_lang_extended_symbols(
            swift_code, "Sources/App/Controllers/UserController.swift", ".swift"
        )
        assert any(s["name"] == "UserController" for s in swift_syms), (
            f"Swift class: {[s['name'] for s in swift_syms]}"
        )
        assert any(s["name"] == "User" for s in swift_syms), (
            f"Swift struct: {[s['name'] for s in swift_syms]}"
        )
        print("[PASS] Swift class + struct detection")

        # --- Dart ---
        dart_code = '''
import 'package:flutter/material.dart';
import 'package:flutter_bloc/flutter_bloc.dart';

class UserListPage extends StatelessWidget {
  const UserListPage({super.key});
  @override
  Widget build(BuildContext context) {
    return Scaffold(appBar: AppBar(title: const Text('Users')));
  }
}

class UserCubit extends Cubit<UserState> {
  UserCubit() : super(UserInitial());
  Future<void> loadUsers() async { emit(UserLoaded([])); }
}
'''
        dart_syms = extract_lang_extended_symbols(dart_code, "lib/users/user_list_page.dart", ".dart")
        assert any("UserListPage" in s["name"] for s in dart_syms), (
            f"Dart widget: {[s['name'] for s in dart_syms]}"
        )
        assert any("UserCubit" in s["name"] for s in dart_syms), (
            f"Dart cubit: {[s['name'] for s in dart_syms]}"
        )
        print("[PASS] Dart Flutter widget + Cubit")

        # --- Groovy ---
        groovy_code = '''
plugins {
    id 'java'
    id 'org.springframework.boot' version '3.2.0'
    id 'io.spring.dependency-management' version '1.1.4'
}

group = 'com.example'
version = '0.0.1-SNAPSHOT'

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
    implementation 'org.springframework.boot:spring-boot-starter-data-jpa'
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
    runtimeOnly 'org.postgresql:postgresql'
}

task buildDocker(type: Exec) {
    commandLine 'docker', 'build', '-t', 'myapp', '.'
}
'''
        gradle_syms = extract_lang_extended_symbols(groovy_code, "build.gradle", ".gradle")
        assert any("spring-boot" in s["name"] for s in gradle_syms), (
            f"Gradle plugin: {[s['name'] for s in gradle_syms]}"
        )
        dep_syms = [s for s in gradle_syms if s.get("symbol_type") == "model"]
        assert len(dep_syms) >= 2, f"Gradle deps: {dep_syms}"
        task_syms = [
            s for s in gradle_syms
            if s.get("symbol_type") == "use_case" and "buildDocker" in s.get("name", "")
        ]
        assert task_syms, f"Gradle task: {[s['name'] for s in gradle_syms]}"
        print("[PASS] Groovy Gradle plugins + deps + tasks")

        print("\n=== All Phase 21 tests PASSED ===")

    _run_tests()
