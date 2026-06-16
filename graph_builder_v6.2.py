#!/usr/bin/env python3
"""Build a lightweight information graph by scanning project files.

v6.2: TypeScript/JS symbol extraction and import parsing run BOTH tree-sitter
AST AND regex, then merge the results (union).  AST results take precedence for
symbols that both find; regex supplements with anything AST misses.  This gives
AST precision (accurate line ranges, class methods, enums, interfaces) plus
regex recall (Zod schemas, unusual patterns, dynamic constructs).

All other behaviour (Go/Python/PHP extraction, dead-export detection, keywords,
file walk, output format) is unchanged from v6.

Falls back to regex-only automatically if tree-sitter is not installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# ── Tree-sitter setup ──────────────────────────────────────────────────────────
try:
    import tree_sitter_typescript as _tsts
    import tree_sitter_javascript as _tsjs
    from tree_sitter import Language, Parser, Node as TSNode

    _LANG_TS  = Language(_tsts.language_typescript())
    _LANG_TSX = Language(_tsts.language_tsx())
    _LANG_JS  = Language(_tsjs.language())
    _AST_AVAILABLE = True
except Exception as e:
    print(f"[warn] tree-sitter not available, falling back to regex: {e}")
    _AST_AVAILABLE = False


SKIP_DIRS = {
    ".git", ".beads", ".beads-hooks", "node_modules", "vendor",
    "dist", "build", ".next", ".idea", ".vscode", "__pycache__",
    "venv", ".venv", ".dual-graph", ".dual-graph-pro", ".graperoot-pro",
    ".claude", ".cursor", ".gemini", ".graperoot",
    # Common vendored/third-party C/C++ directories
    "third_party", "thirdparty", "3rdparty", "external", "deps",
    "contrib", "submodules",
}

# Allow projects to add extra skip dirs via env var (comma-separated)
_extra_skip = os.environ.get("GRAPEROOT_SKIP_DIRS", "")
if _extra_skip:
    SKIP_DIRS |= {d.strip() for d in _extra_skip.split(",") if d.strip()}

MAX_FILE_BYTES = 300_000
MAX_CONTENT_CHARS = 24_000

# Only extract symbols from code files (not config/markdown)
SYMBOL_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".php", ".go",
               ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx",
               ".rs"}

# ── Named import regexes (for dead-export detection — unchanged from v6) ──────
_RE_TS_NAMED_IMPORT = re.compile(
    r'import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+[\'"][^\'"]+[\'"]'
)
_RE_TS_WILDCARD_IMPORT = re.compile(
    r'import\s+\*\s+as\s+(\w+)\s+from\s+[\'"][^\'"]+[\'"]'
)
_RE_TS_REEXPORT = re.compile(
    r'export\s+(?:type\s+)?\{([^}]+)\}\s+from\s+[\'"][^\'"]+[\'"]'
)


def _collect_named_imports(content: str, ext: str) -> tuple[set[str], set[str]]:
    """Return (named_symbols, wildcard_namespaces) imported/re-exported in this file."""
    named: set[str] = set()
    wildcards: set[str] = set()
    if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        for m in _RE_TS_NAMED_IMPORT.finditer(content):
            for part in m.group(1).split(","):
                sym = part.strip().split(" as ")[0].strip()
                if sym and sym != "type":
                    named.add(sym)
        for m in _RE_TS_WILDCARD_IMPORT.finditer(content):
            wildcards.add(m.group(1))
        for m in _RE_TS_REEXPORT.finditer(content):
            for part in m.group(1).split(","):
                sym = part.strip().split(" as ")[0].strip()
                if sym and sym != "type":
                    named.add(sym)
    elif ext == ".py":
        for m in re.finditer(r'from\s+\S+\s+import\s+\(([^)]+)\)', content):
            for part in m.group(1).split(","):
                sym = part.strip().split(" as ")[0].strip()
                if sym:
                    named.add(sym)
        for m in re.finditer(r'from\s+\S+\s+import\s+([^\n\\(]+)', content):
            for part in m.group(1).split(","):
                sym = part.strip().split(" as ")[0].strip()
                if sym and not sym.startswith("#"):
                    named.add(sym)
    return named, wildcards


@dataclass
class Node:
    id: str
    kind: str       # "file" or "symbol"
    path: str       # relative file path (for symbols: the containing file)
    ext: str
    size: int
    keywords: list[str]
    content: str = ""
    summary: str = ""       # heuristic NL summary (file nodes only)
    file_hash: str = ""     # md5[:8] of file content for incremental rescan
    # Symbol-only fields
    symbol_type: str = ""   # api_route | hook | model | use_case | utility
    name: str = ""
    line_start: int = 0
    line_end: int = 0
    body_hash: str = ""
    confidence: str = ""    # high | medium | low
    exported: bool = False

    def as_dict(self) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "ext": self.ext,
            "size": self.size,
            "keywords": self.keywords,
        }
        if self.kind == "file":
            d["content"] = self.content
            d["summary"] = self.summary
            d["file_hash"] = self.file_hash
        else:
            d["symbol_type"] = self.symbol_type
            d["name"] = self.name
            d["line_start"] = self.line_start
            d["line_end"] = self.line_end
            d["body_hash"] = self.body_hash
            d["confidence"] = self.confidence
            d["exported"] = self.exported
        return d


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_camel(name: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [p.lower() for p in parts.split() if len(p) >= 3]


def _name_keywords(name: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    def add(w: str) -> None:
        w = w.lower().strip("_")
        if len(w) >= 3 and w not in seen:
            seen.add(w); tokens.append(w)
    add(name)
    for p in _split_camel(name):
        add(p)
    for p in name.split("_"):
        add(p)
    return tokens[:10]


def _body_hash(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start : end + 1])
    return hashlib.md5(body.encode()).hexdigest()[:8]


def _find_block_end_ts(lines: list[str], start: int) -> int:
    """Find closing brace of a TS/JS block starting at `start` via brace counting."""
    depth = 0
    found_open = False
    limit = min(start + 400, len(lines))
    for i in range(start, limit):
        line = lines[i]
        opens  = line.count("{") - line.count("\\{")
        closes = line.count("}") - line.count("\\}")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + 100, len(lines) - 1)


def _find_block_end_py(lines: list[str], start: int) -> int:
    """Find end of Python def/class block by indentation tracking."""
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


# ── AST helpers ───────────────────────────────────────────────────────────────

def _node_text(node: "TSNode", src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _ts_is_exported(node: "TSNode") -> bool:
    parent = node.parent
    if parent is None:
        return False
    if parent.type in ("export_statement", "export_clause", "export_default_declaration"):
        return True
    return False


def _classify_ts(name: str, exported: bool) -> tuple[str, str]:
    if re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$", name):
        return "api_route", "high"
    if name.startswith("use") and len(name) > 3 and name[3].isupper():
        return "hook", "high"
    if exported and name[0].isupper():
        return "use_case", "medium"
    if exported:
        return "use_case", "high"
    return "utility", "medium"


# ── TS/JS symbol extraction — AST (v6.1) ─────────────────────────────────────

def _extract_symbols_ts_ast(content: str, file_path: str, ext: str) -> list[dict]:
    """AST-based symbol extraction for TypeScript/JavaScript."""
    src = content.encode("utf-8", errors="ignore")
    lines = content.splitlines()
    lang = _LANG_TSX if ext == ".tsx" else (_LANG_JS if ext in {".js", ".jsx"} else _LANG_TS)
    parser = Parser(lang)
    tree = parser.parse(src)

    symbols: list[dict] = []
    seen: set[str] = set()

    def add_sym(name: str, line_start: int, line_end: int, sym_type: str, confidence: str, exported: bool) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        symbols.append({
            "id":          f"{file_path}::{name}",
            "name":        name,
            "symbol_type": sym_type,
            "line_start":  line_start,
            "line_end":    line_end,
            "body_hash":   _body_hash(lines, line_start, line_end),
            "confidence":  confidence,
            "exported":    exported,
            "keywords":    _name_keywords(name),
        })

    def walk(node: "TSNode") -> None:
        t = node.type

        if t in ("function_declaration", "generator_function_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, src)
                exported = _ts_is_exported(node)
                sym_type, conf = _classify_ts(name, exported)
                add_sym(name, node.start_point[0], node.end_point[0], sym_type, conf, exported)

        elif t == "lexical_declaration":
            exported = _ts_is_exported(node)
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    val_node  = child.child_by_field_name("value")
                    if name_node and val_node and val_node.type in ("arrow_function", "function"):
                        name = _node_text(name_node, src)
                        sym_type, conf = _classify_ts(name, exported)
                        add_sym(name, node.start_point[0], node.end_point[0], sym_type, conf, exported)

        elif t == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, src)
                exported = _ts_is_exported(node)
                add_sym(name, node.start_point[0], node.end_point[0], "model", "high", exported)

        elif t in ("interface_declaration", "type_alias_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, src)
                exported = _ts_is_exported(node)
                add_sym(name, node.start_point[0], node.end_point[0], "model", "high", exported)

        elif t == "enum_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, src)
                exported = _ts_is_exported(node)
                add_sym(name, node.start_point[0], node.end_point[0], "model", "high", exported)

        elif t == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, src)
                if not name.startswith("#"):
                    add_sym(name, node.start_point[0], node.end_point[0], "use_case", "medium", False)

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols


# ── TS/JS symbol extraction — regex fallback (unchanged from v6) ──────────────

def _extract_symbols_ts_regex(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str, exported: bool) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_ts(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": confidence,
            "exported": exported,
            "keywords": _name_keywords(name),
        })

    def line_of(match_start: int) -> int:
        return text[:match_start].count("\n")

    for m in re.finditer(
        r"^export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*[\(<]",
        text, re.MULTILINE,
    ):
        add_sym(m.group(1), line_of(m.start()), "api_route", "high", True)

    for m in re.finditer(
        r"^(export\s+)?(default\s+)?(async\s+)?function\s+([A-Za-z_]\w*)\s*[\(<]",
        text, re.MULTILINE,
    ):
        name = m.group(4)
        exported = bool(m.group(1))
        sym_type, conf = _classify_ts(name, exported)
        if sym_type != "api_route":
            add_sym(name, line_of(m.start()), sym_type, conf, exported)

    for m in re.finditer(
        r"^export\s+(?:const|let)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|\w+\s*=>)",
        text, re.MULTILINE,
    ):
        name = m.group(1)
        sym_type, conf = _classify_ts(name, True)
        add_sym(name, line_of(m.start()), sym_type, conf, True)

    for m in re.finditer(
        r"^(export\s+)?(?:interface|type)\s+([A-Za-z_]\w*)\s*[={<]",
        text, re.MULTILINE,
    ):
        name = m.group(2)
        exported = bool(m.group(1))
        add_sym(name, line_of(m.start()), "model", "high", exported)

    for m in re.finditer(
        r"^(export\s+)?(?:default\s+)?class\s+([A-Za-z_]\w*)",
        text, re.MULTILINE,
    ):
        name = m.group(2)
        exported = bool(m.group(1))
        add_sym(name, line_of(m.start()), "model", "high", exported)

    for m in re.finditer(
        r"^(?:export\s+)?const\s+([A-Za-z_]\w*[Ss]chema)\s*=\s*z\.",
        text, re.MULTILINE,
    ):
        add_sym(m.group(1), line_of(m.start()), "model", "high", True)

    return symbols


def extract_symbols_ts(content: str, file_path: str, ext: str = ".ts") -> list[dict]:
    """Union: AST + regex merged.  AST takes precedence; regex fills gaps."""
    if not _AST_AVAILABLE:
        return _extract_symbols_ts_regex(content, file_path)

    ast_syms = _extract_symbols_ts_ast(content, file_path, ext)
    seen = {s["name"] for s in ast_syms}

    # Regex supplement — add any symbol the AST didn't find
    for sym in _extract_symbols_ts_regex(content, file_path):
        if sym["name"] not in seen:
            seen.add(sym["name"])
            ast_syms.append(sym)

    return ast_syms


# ── Python symbol extraction (unchanged from v6) ──────────────────────────────

def extract_symbols_py(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    route_deco_lines: set[int] = set()
    for m in re.finditer(
        r"@(?:app|router|blueprint)\.(?:get|post|put|delete|patch)\s*\(",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        route_deco_lines.add(text[:m.start()].count("\n"))

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_py(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": confidence,
            "exported": not name.startswith("_"),
            "keywords": _name_keywords(name),
        })

    for m in re.finditer(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", text, re.MULTILINE):
        name = m.group(1)
        if name.startswith("_") or name.startswith("test_"):
            continue
        line_no = text[:m.start()].count("\n")
        is_route = any(abs(line_no - dl) <= 3 for dl in route_deco_lines)
        add_sym(name, line_no, "api_route" if is_route else "use_case", "high")

    for m in re.finditer(r"^class\s+([A-Za-z_]\w*)", text, re.MULTILINE):
        add_sym(m.group(1), text[:m.start()].count("\n"), "model", "high")

    return symbols


# ── PHP symbol extraction (unchanged from v6) ─────────────────────────────────

def extract_symbols_php(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    for m in re.finditer(r"(?:public|private|protected|\s)(?:static\s+)?function\s+([A-Za-z_]\w*)", text):
        name = m.group(1)
        if name in seen or not name:
            continue
        seen.add(name)
        exported = "public" in text[m.start():m.start() + 20]
        line_no = text[:m.start()].count("\n")
        end = _find_block_end_ts(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}", "name": name,
            "symbol_type": "use_case", "line_start": line_no, "line_end": end,
            "body_hash": _body_hash(lines, line_no, end), "confidence": "high",
            "exported": exported, "keywords": _name_keywords(name),
        })

    for m in re.finditer(r"(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)", text):
        name = m.group(1)
        if name in seen or not name:
            continue
        seen.add(name)
        line_no = text[:m.start()].count("\n")
        end = _find_block_end_ts(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}", "name": name,
            "symbol_type": "model", "line_start": line_no, "line_end": end,
            "body_hash": _body_hash(lines, line_no, end), "confidence": "high",
            "exported": True, "keywords": _name_keywords(name),
        })

    return symbols


# ── Go symbol extraction (unchanged from v6) ──────────────────────────────────

def _classify_go(name: str, receiver: str | None) -> tuple[str, str]:
    low = name.lower()
    if low.startswith("handle") or low.startswith("serve") or "handler" in low:
        return "api_route", "high"
    if low.startswith("test") or low.startswith("benchmark"):
        return "utility", "medium"
    if low.startswith("new") and len(name) > 3 and name[3].isupper():
        return "use_case", "high"
    if receiver:
        return "use_case", "high"
    if name[0].isupper():
        return "use_case", "high"
    return "utility", "medium"


def extract_symbols_go(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str, exported: bool, receiver: str | None = None) -> None:
        sym_id = f"{receiver}.{name}" if receiver else name
        if sym_id in seen or not name:
            return
        seen.add(sym_id)
        end = _find_block_end_ts(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{sym_id}", "name": sym_id,
            "symbol_type": sym_type, "line_start": line_no, "line_end": end,
            "body_hash": _body_hash(lines, line_no, end), "confidence": confidence,
            "exported": exported, "keywords": _name_keywords(name),
        })

    def line_of(match_start: int) -> int:
        return text[:match_start].count("\n")

    for m in re.finditer(r"^func\s+(?:\((\w+)\s+\*?(\w+)\)\s+)?([A-Za-z_]\w*)\s*\(", text, re.MULTILINE):
        receiver_type = m.group(2)
        name = m.group(3)
        sym_type, conf = _classify_go(name, receiver_type)
        add_sym(name, line_of(m.start()), sym_type, conf, name[0].isupper(), receiver_type)

    for m in re.finditer(r"^type\s+([A-Za-z_]\w*)\s+struct\s*\{", text, re.MULTILINE):
        name = m.group(1)
        add_sym(name, line_of(m.start()), "model", "high", name[0].isupper())

    for m in re.finditer(r"^type\s+([A-Za-z_]\w*)\s+interface\s*\{", text, re.MULTILINE):
        name = m.group(1)
        add_sym(name, line_of(m.start()), "model", "high", name[0].isupper())

    for m in re.finditer(r"^type\s+([A-Za-z_]\w*)\s+(?!=struct|interface)\w", text, re.MULTILINE):
        name = m.group(1)
        if name not in seen:
            add_sym(name, line_of(m.start()), "model", "medium", name[0].isupper())

    return symbols


# ── C/C++ symbol extraction (regex-based) ─────────────────────────────────────

_CPP_EXTS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"}


def _classify_cpp(name: str, sym_type: str) -> tuple[str, str]:
    low = name.lower()
    if sym_type in ("class", "struct"):
        return "model", "high"
    if low.startswith("test") or low.startswith("bench"):
        return "utility", "medium"
    if low.startswith("handle") or low.startswith("on_") or "handler" in low or "callback" in low:
        return "api_route", "high"
    if low.startswith("init") or low.startswith("create") or low.startswith("new"):
        return "use_case", "high"
    return "use_case", "high"


def extract_symbols_cpp(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str, exported: bool) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_ts(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}", "name": name,
            "symbol_type": sym_type, "line_start": line_no, "line_end": end,
            "body_hash": _body_hash(lines, line_no, end), "confidence": confidence,
            "exported": exported, "keywords": _name_keywords(name),
        })

    def line_of(match_start: int) -> int:
        return text[:match_start].count("\n")

    # class Foo / struct Foo (with optional inheritance)
    for m in re.finditer(r"^(?:class|struct)\s+([A-Za-z_]\w*)\b", text, re.MULTILINE):
        name = m.group(1)
        st, conf = _classify_cpp(name, "class")
        add_sym(name, line_of(m.start()), st, conf, True)

    # Free functions and method definitions: ReturnType Foo::Bar(...) or ReturnType Bar(...)
    # Matches: void foo(, int* bar(, MyClass::method(
    for m in re.finditer(
        r"^(?:(?:static|inline|virtual|extern|const|unsigned|signed|volatile|explicit)\s+)*"
        r"(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*[\s*&]+)"
        r"(?:([A-Za-z_]\w*)::)?([A-Za-z_]\w*)\s*\(",
        text, re.MULTILINE
    ):
        cls = m.group(1)
        name = m.group(2)
        if name in ("if", "while", "for", "switch", "return", "sizeof", "typeof", "catch"):
            continue
        sym_id = f"{cls}.{name}" if cls else name
        if sym_id in seen:
            continue
        seen.add(sym_id)
        end = _find_block_end_ts(lines, line_of(m.start()))
        st, conf = _classify_cpp(name, "function")
        symbols.append({
            "id": f"{file_path}::{sym_id}", "name": sym_id,
            "symbol_type": st, "line_start": line_of(m.start()), "line_end": end,
            "body_hash": _body_hash(lines, line_of(m.start()), end), "confidence": conf,
            "exported": True, "keywords": _name_keywords(name),
        })

    # enum Foo {
    for m in re.finditer(r"^enum\s+(?:class\s+)?([A-Za-z_]\w*)\s*(?:\{|:)", text, re.MULTILINE):
        name = m.group(1)
        add_sym(name, line_of(m.start()), "model", "medium", True)

    # typedef ... Name;
    for m in re.finditer(r"^typedef\s+.+?\s+([A-Za-z_]\w*)\s*;", text, re.MULTILINE):
        name = m.group(1)
        add_sym(name, line_of(m.start()), "model", "medium", True)

    # #define MACRO_NAME (only uppercase/underscore macros — skip include guards)
    for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]{2,})(?:\s|\()", text, re.MULTILINE):
        name = m.group(1)
        if name.endswith("_H") or name.endswith("_H_") or name.endswith("_HPP"):
            continue
        add_sym(name, line_of(m.start()), "utility", "low", True)

    return symbols


def _parse_imports_cpp(content: str, file_id: str) -> list[dict]:
    """Parse #include directives into import edges."""
    edges: list[dict] = []
    for m in re.finditer(r'^#include\s*[<"]([^>"]+)[>"]', content, re.MULTILINE):
        edges.append({"from": file_id, "to": m.group(1), "rel": "imports"})
    return edges


# ── Rust symbol extraction (regex-based) ──────────────────────────────────────

def _classify_rust(name: str, kind: str) -> tuple[str, str]:
    low = name.lower()
    if kind in ("struct", "enum", "trait", "type"):
        return "model", "high"
    if kind == "macro":
        return "utility", "medium"
    if kind == "const":
        return "utility", "low"
    if low.startswith("test") or low.startswith("bench") or low == "main":
        return "utility", "medium"
    if any(x in low for x in ("handle", "handler", "route", "endpoint", "serve", "dispatch")):
        return "api_route", "high"
    if any(x in low for x in ("new", "create", "build", "init", "setup", "from")):
        return "use_case", "high"
    return "use_case", "high"


def extract_symbols_rust(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text  = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def add_sym(name: str, line_no: int, kind: str, exported: bool,
                qualifier: str | None = None) -> None:
        full = f"{qualifier}.{name}" if qualifier else name
        if full in seen or not name:
            return
        seen.add(full)
        end = _find_block_end_ts(lines, line_no)
        sym_type, conf = _classify_rust(name, kind)
        symbols.append({
            "id": f"{file_path}::{full}", "name": full,
            "symbol_type": sym_type, "line_start": line_no, "line_end": end,
            "body_hash": _body_hash(lines, line_no, end), "confidence": conf,
            "exported": exported, "keywords": _name_keywords(name),
        })

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    # pub fn / async fn / pub async fn / fn
    for m in re.finditer(
        r"^(?P<pub>pub(?:\(crate\))?\s+)?(?:async\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(",
        text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "fn", bool(m.group("pub")))

    # pub struct / struct
    for m in re.finditer(
        r"^(?P<pub>pub(?:\(crate\))?\s+)?struct\s+(?P<name>[A-Za-z_]\w*)\b",
        text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "struct", bool(m.group("pub")))

    # pub enum / enum
    for m in re.finditer(
        r"^(?P<pub>pub(?:\(crate\))?\s+)?enum\s+(?P<name>[A-Za-z_]\w*)\b",
        text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "enum", bool(m.group("pub")))

    # pub trait / trait
    for m in re.finditer(
        r"^(?P<pub>pub(?:\(crate\))?\s+)?trait\s+(?P<name>[A-Za-z_]\w*)\b",
        text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "trait", bool(m.group("pub")))

    # pub type Foo = ...
    for m in re.finditer(
        r"^(?P<pub>pub(?:\(crate\))?\s+)?type\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*)?\s*=",
        text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "type", bool(m.group("pub")))

    # pub const / pub static
    for m in re.finditer(
        r"^pub(?:\(crate\))?\s+(?:const|static)\s+(?P<name>[A-Z_][A-Z0-9_]*)\s*:",
        text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "const", True)

    # macro_rules! foo
    for m in re.finditer(r"^(?:(?:#\[macro_export\]\s+))?macro_rules!\s+(?P<name>[A-Za-z_]\w*)", text, re.MULTILINE):
        exported = "#[macro_export]" in text[max(0, m.start()-30):m.start()]
        add_sym(m.group("name"), line_of(m.start()), "macro", exported)

    # impl Foo { pub fn method — extract methods with qualifier
    for impl_m in re.finditer(
        r"^impl(?:\s*<[^>]*>)?\s+(?:[A-Za-z_]\w*\s+for\s+)?(?P<type>[A-Za-z_]\w*)\s*(?:<[^>]*)?\s*\{",
        text, re.MULTILINE,
    ):
        impl_type = impl_m.group("type")
        impl_start = impl_m.end()
        # Find matching closing brace
        depth, pos = 1, impl_start
        while pos < len(text) and depth > 0:
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        impl_body = text[impl_start:pos - 1]
        body_offset = impl_start
        for fn_m in re.finditer(
            r"(?P<pub>pub(?:\(crate\))?\s+)?(?:async\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*)?\s*\(",
            impl_body,
        ):
            fn_name = fn_m.group("name")
            if fn_name in ("new", ) or fn_m.group("pub"):  # include new() and pub methods
                abs_pos = body_offset + fn_m.start()
                add_sym(fn_name, line_of(abs_pos), "fn", bool(fn_m.group("pub")), impl_type)

    return symbols


def _parse_imports_rust(content: str, file_id: str) -> list[dict]:
    """Parse Rust `use` declarations into import edges."""
    edges: list[dict] = []
    seen: set[str] = set()

    def add_edge(module: str) -> None:
        # Normalise: strip leading `crate::`, `super::`, `self::`
        clean = module.strip().rstrip(";").strip()
        if clean and clean not in seen:
            seen.add(clean)
            edges.append({"from": file_id, "to": clean, "rel": "imports"})

    for m in re.finditer(r"^use\s+([^;{]+(?:\{[^}]*\})?[^;]*);", content, re.MULTILINE):
        raw = m.group(1).strip()
        # use a::b::{C, D, E} → emit a::b as module
        if "{" in raw:
            module = raw[:raw.index("{")].rstrip(":").strip()
            add_edge(module)
            # also emit each specific symbol
            inner = raw[raw.index("{") + 1:raw.index("}")]
            for sym in inner.split(","):
                sym = sym.strip().split(" as ")[0].strip()
                if sym and sym != "*":
                    add_edge(f"{module}::{sym}")
        else:
            # use a::b::C  →  emit a::b::C and a::b
            add_edge(raw)
            parts = raw.split("::")
            if len(parts) > 1:
                add_edge("::".join(parts[:-1]))

    # extern crate foo;
    for m in re.finditer(r"^extern\s+crate\s+([A-Za-z_]\w*)\s*;", content, re.MULTILINE):
        add_edge(m.group(1))

    return edges


# ── Import/relation parsing — AST+regex union for TS/JS, regex for rest (v6.2) ─

def _parse_imports_ts_ast(content: str, file_id: str, ext: str) -> list[dict]:
    """AST-accurate import extraction for TypeScript/JavaScript."""
    src = content.encode("utf-8", errors="ignore")
    lang = _LANG_TSX if ext == ".tsx" else (_LANG_JS if ext in {".js", ".jsx"} else _LANG_TS)
    parser = Parser(lang)
    tree = parser.parse(src)
    edges: list[dict] = []

    def walk(node: "TSNode") -> None:
        t = node.type
        if t == "import_statement":
            src_node = node.child_by_field_name("source")
            if src_node:
                path = _node_text(src_node, src).strip("'\"")
                edges.append({"from": file_id, "to": path, "rel": "imports"})
        elif t == "export_statement":
            # re-exports: export { X } from "..."  /  export * from "..."
            src_node = node.child_by_field_name("source")
            if src_node:
                path = _node_text(src_node, src).strip("'\"")
                edges.append({"from": file_id, "to": path, "rel": "imports"})
        elif t == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn and _node_text(fn, src) == "require" and args:
                for child in args.children:
                    if child.type == "string":
                        path = _node_text(child, src).strip("'\"")
                        edges.append({"from": file_id, "to": path, "rel": "requires"})
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return edges


def parse_relations(path: Path, text: str, root: Path) -> list[dict]:
    """Parse import/require edges.  TS/JS: AST + regex merged; all others regex (v6)."""
    file_id = rel(path, root)
    ext = path.suffix.lower()

    if _AST_AVAILABLE and ext in {".ts", ".tsx", ".js", ".jsx"}:
        edges = _parse_imports_ts_ast(text, file_id, ext)
        seen_edges = {(e["from"], e["to"]) for e in edges}

        # Regex supplement — add any edge the AST didn't produce
        for match in re.finditer(r'import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', text):
            to = match.group(1)
            if (file_id, to) not in seen_edges:
                seen_edges.add((file_id, to))
                edges.append({"from": file_id, "to": to, "rel": "imports"})
        for match in re.finditer(r'export\s+(?:\{[^}]+\}|\*(?:\s+as\s+\w+)?)\s+from\s+[\'"]([^\'"]+)[\'"]', text):
            to = match.group(1)
            if (file_id, to) not in seen_edges:
                seen_edges.add((file_id, to))
                edges.append({"from": file_id, "to": to, "rel": "imports"})
        for match in re.finditer(r'require\([\'"]([^\'"]+)[\'"]\)', text):
            to = match.group(1)
            if (file_id, to) not in seen_edges:
                seen_edges.add((file_id, to))
                edges.append({"from": file_id, "to": to, "rel": "requires"})

        # File-reference edges
        for match in re.finditer(r'([A-Za-z0-9_\-\/]+\.(go|py|ts|tsx|js|jsx|swift|md|json|yaml|yml|c|cpp|cc|cxx|h|hpp|hh|hxx|rs))', text):
            candidate = match.group(1)
            if "/" in candidate:
                edges.append({"from": file_id, "to": candidate, "rel": "references"})
        return edges

    # C/C++ #include parsing
    if ext in _CPP_EXTS:
        return _parse_imports_cpp(text, file_id)

    # Rust use/extern crate parsing
    if ext == ".rs":
        return _parse_imports_rust(text, file_id)

    # Regex fallback (identical to v6 parse_relations)
    edges: list[dict] = []
    for match in re.finditer(r'import\s+\(?\s*"([^"]+)"', text):
        edges.append({"from": file_id, "to": match.group(1), "rel": "imports"})
    for match in re.finditer(r'import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', text):
        edges.append({"from": file_id, "to": match.group(1), "rel": "imports"})
    for match in re.finditer(r'export\s+(?:\{[^}]+\}|\*(?:\s+as\s+\w+)?)\s+from\s+[\'"]([^\'"]+)[\'"]', text):
        edges.append({"from": file_id, "to": match.group(1), "rel": "imports"})
    for match in re.finditer(r'require\([\'"]([^\'"]+)[\'"]\)', text):
        edges.append({"from": file_id, "to": match.group(1), "rel": "requires"})
    for match in re.finditer(r'^\s*import\s+([a-zA-Z0-9_\.]+)', text, flags=re.MULTILINE):
        edges.append({"from": file_id, "to": match.group(1), "rel": "imports"})
    for match in re.finditer(r'^\s*from\s+([a-zA-Z0-9_\.]+)\s+import\s+', text, flags=re.MULTILINE):
        edges.append({"from": file_id, "to": match.group(1), "rel": "imports"})
    if path.suffix == ".swift":
        for match in re.finditer(r'^import\s+([A-Za-z_]\w*)', text, flags=re.MULTILINE):
            edges.append({"from": file_id, "to": match.group(1), "rel": "imports"})
    for match in re.finditer(r'([A-Za-z0-9_\-\/]+\.(go|py|ts|tsx|js|jsx|swift|md|json|yaml|yml|c|cpp|cc|cxx|h|hpp|hh|hxx|rs))', text):
        candidate = match.group(1)
        if "/" in candidate:
            edges.append({"from": file_id, "to": candidate, "rel": "references"})
    return edges


# ── Keyword extraction (unchanged from v6) ────────────────────────────────────

def extract_keywords(content: str, ext: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        w = word.lower().strip("_")
        if len(w) >= 3 and w not in seen:
            seen.add(w); tokens.append(w)

    def add_name(name: str) -> None:
        add(name)
        for part in _split_camel(name):
            add(part)

    route_pat = re.compile(
        r"""(?:app|router|r|mux|api)\s*[.\@]\s*(get|post|put|delete|patch|handle|handlefunc)\s*\(\s*['"/]([^'")\s]+)""",
        re.IGNORECASE,
    )
    for m in route_pat.finditer(content):
        add(m.group(1).upper())
        for seg in m.group(2).strip("/").split("/"):
            seg = re.sub(r"[{}:<>]", "", seg)
            if seg: add(seg)

    dec_route = re.compile(r'@\w+\.(get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE)
    for m in dec_route.finditer(content):
        add(m.group(1).upper())
        for seg in m.group(2).strip("/").split("/"):
            seg = re.sub(r"[{}:<>]", "", seg)
            if seg: add(seg)

    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        for m in re.finditer(r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|type|interface|enum)\s+([A-Za-z_]\w*)", content):
            add_name(m.group(1))
        for m in re.finditer(r"export\s*\{([^}]+)\}", content):
            for name in re.split(r"[,\s]+", m.group(1)):
                name = name.strip()
                if name: add_name(name)

    if ext == ".py":
        for m in re.finditer(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)|^class\s+([A-Za-z_]\w*)", content, re.MULTILINE):
            name = m.group(1) or m.group(2)
            if name and not name.startswith("_"):
                add_name(name)
                for part in name.split("_"): add(part)

    if ext == ".go":
        for m in re.finditer(r"^func\s+(?:\([^)]+\)\s+)?([A-Z]\w*)", content, re.MULTILINE):
            add_name(m.group(1))
        for m in re.finditer(r"^type\s+([A-Z]\w*)\s+(?:struct|interface)", content, re.MULTILINE):
            add_name(m.group(1))
        for m in re.finditer(r"^func\s+\(\w+\s+\*?([A-Z]\w*)\)\s+(\w+)", content, re.MULTILINE):
            add_name(m.group(1)); add_name(m.group(2))
        if "go func" in content or "goroutine" in content.lower():
            add("goroutine"); add("concurrency")
        if "chan " in content or "<-" in content:
            add("channel")
        if "sync.Mutex" in content or "sync.RWMutex" in content or "sync.WaitGroup" in content:
            add("sync"); add("mutex")
        if "context.Context" in content or "ctx context" in content:
            add("context")
        if "errors.New" in content or "fmt.Errorf" in content or "errors.Is" in content:
            add("error"); add("errors")
        for m in re.finditer(r"\.(?:GET|POST|PUT|DELETE|PATCH|Handle|HandleFunc)\s*\(\s*[\"']([^\"']+)", content):
            for seg in m.group(1).strip("/").split("/"):
                seg = re.sub(r"[{}:<>*]", "", seg)
                if seg: add(seg)
        pkg = re.match(r"^package\s+(\w+)", content, re.MULTILINE)
        if pkg: add(pkg.group(1))

    if ext == ".swift":
        for m in re.finditer(r"^(?:public\s+|private\s+|internal\s+|open\s+|final\s+)*(?:class|struct|enum|protocol|actor|extension)\s+([A-Za-z_]\w*)", content, re.MULTILINE):
            add_name(m.group(1))
        for m in re.finditer(r"^(?:\s*)(?:public\s+|private\s+|internal\s+|open\s+|static\s+|class\s+|mutating\s+|override\s+)*func\s+([A-Za-z_]\w*)", content, re.MULTILINE):
            add_name(m.group(1))

    doc_m = re.match(r'\s*"""([^"]{10,200})', content)
    if doc_m:
        for word in re.findall(r"[a-zA-Z]{4,}", doc_m.group(1)): add(word)
    jsdoc_m = re.match(r"\s*/\*\*?\s*([^\n*]{10,200})", content)
    if jsdoc_m:
        for word in re.findall(r"[a-zA-Z]{4,}", jsdoc_m.group(1)): add(word)

    http_call = re.compile(
        r"""(?:fetch|axios\.(?:get|post|put|delete)|requests\.(?:get|post|put|delete))\s*\(\s*[`'"](https?://[^`'"]+|/[^`'"]+)[`'"]""",
        re.IGNORECASE,
    )
    for m in http_call.finditer(content):
        url = m.group(1)
        for seg in url.replace("http://", "").replace("https://", "").strip("/").split("/"):
            seg = re.sub(r"[{}?=&<>]", "", seg).split(".")[0]
            if len(seg) >= 3: add(seg)

    return tokens[:80]


# ── NL summary (unchanged from v6) ────────────────────────────────────────────

def _make_summary(content: str, path: str, ext: str) -> str:
    lines = content.splitlines()
    lead = ""
    if ext == ".py":
        m = re.search(r'^\s*(?:\'\'\'|""")([^\'\"]{8,200}?)(?:\'\'\'|""")', content, re.DOTALL)
        if m:
            lead = " ".join(m.group(1).split())[:120]
    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        m = re.match(r"\s*/\*\*?\s*(.*?)\*/", content, re.DOTALL)
        if m:
            lead = " ".join(m.group(1).replace("*", "").split())[:120]
    if not lead:
        for line in lines[:5]:
            s = line.strip().lstrip("#").lstrip("//").strip()
            if len(s) >= 10:
                lead = s[:120]; break

    names: list[str] = []
    if ext == ".py":
        for m2 in re.finditer(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)|^class\s+([A-Za-z_]\w*)", content, re.MULTILINE):
            name = m2.group(1) or m2.group(2)
            if name and not name.startswith("_") and name not in names:
                names.append(name)
                if len(names) >= 5: break
    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        for m2 in re.finditer(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)|(?:export\s+)?class\s+([A-Za-z_]\w*)", content, re.MULTILINE):
            name = m2.group(1) or m2.group(2)
            if name and name not in names:
                names.append(name)
                if len(names) >= 5: break

    parts = [p for p in re.split(r"[/\\]", path) if p and p != "."]
    domain_parts = [p for p in parts if p.lower() not in {"src", "lib", "app", "pkg", "main", "index"}]
    domain = " ".join(domain_parts[-2:]).replace("_", " ").replace("-", " ") if domain_parts else parts[-1] if parts else ""
    domain = re.sub(r"\.\w+$", "", domain).strip()

    parts_out: list[str] = []
    if lead:
        parts_out.append(lead.rstrip(".") + ".")
    elif domain:
        parts_out.append(f"Handles {domain}.")
    if names:
        n_label = "functions/classes" if len(names) > 1 else "function"
        names_str = ", ".join(names[:4])
        if len(names) >= 5: names_str += ", ..."
        parts_out.append(f"Contains {len(names)} {n_label}: {names_str}.")
    return " ".join(parts_out)[:250]


# ── File scanning (unchanged from v6) ─────────────────────────────────────────

def should_scan(path: Path) -> bool:
    posix = path.as_posix()
    if "/venv/" in posix or "/.venv/" in posix:
        return False
    return path.suffix.lower() in {
        ".go", ".py", ".js", ".jsx", ".ts", ".tsx", ".swift",
        ".json", ".yaml", ".yml", ".md",
        ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx",
        ".rs",
    }


def walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        base = Path(dirpath)
        def _keep_dir(d: str) -> bool:
            if d in SKIP_DIRS: return False
            p = base / d
            try:
                if p.is_symlink() and not p.resolve().is_relative_to(root_resolved):
                    return False
            except Exception:
                return False
            return True
        dirnames[:] = [d for d in dirnames if _keep_dir(d)]
        for name in filenames:
            path = base / name
            try:
                if should_scan(path) and path.is_file():
                    files.append(path)
            except (PermissionError, OSError):
                pass
    return files


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return os.path.relpath(path.resolve(), root.resolve())


def _extract_symbols_for_file(content: str, file_id: str, ext: str) -> list[dict]:
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        return extract_symbols_ts(content, file_id, ext)
    if ext == ".py":
        return extract_symbols_py(content, file_id)
    if ext == ".php":
        return extract_symbols_php(content, file_id)
    if ext == ".go":
        return extract_symbols_go(content, file_id)
    if ext in _CPP_EXTS:
        return extract_symbols_cpp(content, file_id)
    if ext == ".rs":
        return extract_symbols_rust(content, file_id)
    return []


def _append_symbol_nodes(nodes: list[Node], edges: list[dict], syms: list[dict], file_id: str, ext: str) -> None:
    for s in syms:
        nodes.append(Node(
            id=s["id"], kind="symbol", path=file_id, ext=ext,
            size=s["line_end"] - s["line_start"] + 1,
            keywords=s["keywords"],
            symbol_type=s["symbol_type"], name=s["name"],
            line_start=s["line_start"], line_end=s["line_end"],
            body_hash=s["body_hash"], confidence=s["confidence"],
            exported=s["exported"],
        ))
        edges.append({"from": file_id, "to": s["id"], "rel": "contains"})


def dedupe_edges(edges: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for edge in edges:
        key = (edge["from"], edge["to"], edge["rel"])
        if key not in seen:
            seen.add(key)
            out.append(edge)
    return out


# ── Main scan (unchanged from v6) ─────────────────────────────────────────────

def scan(root: Path, existing_nodes: dict[str, dict] | None = None) -> dict:
    nodes: list[Node] = []
    edges: list[dict] = []
    files = walk_files(root)
    prior: dict[str, dict] = existing_nodes or {}

    for path in files:
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue

        file_id = rel(path, root)
        ext = path.suffix.lower()
        fhash = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()[:8]

        if file_id in prior:
            old = prior[file_id]
            if old.get("file_hash") == fhash:
                old_node = Node(
                    id=old["id"], kind=old.get("kind", "file"),
                    path=old.get("path", file_id), ext=old.get("ext", ext),
                    size=old.get("size", size), keywords=old.get("keywords", []),
                    content=old.get("content", content[:MAX_CONTENT_CHARS]),
                    summary=old.get("summary", ""), file_hash=fhash,
                )
                nodes.append(old_node)
                edges.extend(parse_relations(path, content, root))
                if ext in SYMBOL_EXTS:
                    _append_symbol_nodes(nodes, edges, _extract_symbols_for_file(content, file_id, ext), file_id, ext)
                continue

        summary = _make_summary(content, file_id, ext)
        nodes.append(Node(
            id=file_id, kind="file", path=file_id, ext=ext, size=size,
            keywords=extract_keywords(content, ext),
            content=content[:MAX_CONTENT_CHARS], summary=summary, file_hash=fhash,
        ))
        edges.extend(parse_relations(path, content, root))
        if ext in SYMBOL_EXTS:
            _append_symbol_nodes(nodes, edges, _extract_symbols_for_file(content, file_id, ext), file_id, ext)

    unique_edges = dedupe_edges(edges)
    symbol_count = sum(1 for n in nodes if n.kind == "symbol")

    # ── Dead export detection (unchanged from v6) ─────────────────────────────
    all_imported: set[str] = set()
    has_wildcard_import: bool = False
    for path in files:
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        ext = path.suffix.lower()
        named, wildcards = _collect_named_imports(content, ext)
        all_imported.update(named)
        if wildcards:
            has_wildcard_import = True

    dead_exports: list[dict] = []
    for n in nodes:
        if n.kind != "symbol" or not n.exported:
            continue
        if has_wildcard_import:
            continue
        if n.name and n.name not in all_imported:
            dead_exports.append({
                "file": n.path, "symbol": n.name,
                "symbol_type": n.symbol_type, "line": n.line_start,
            })

    return {
        "root": str(root),
        "node_count": len(nodes),
        "edge_count": len(unique_edges),
        "file_count": len(nodes) - symbol_count,
        "symbol_count": symbol_count,
        "nodes": [n.as_dict() for n in nodes],
        "edges": unique_edges,
        "dead_export_count": len(dead_exports),
        "dead_exports": dead_exports,
        "ast_mode": _AST_AVAILABLE,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="GrapeRoot Pro v6.2 — graph builder with AST+regex union for TS/JS")
    parser.add_argument("--root", default=".", help="Project root to scan.")
    parser.add_argument("--out", default=".dual-graph/info_graph.json", help="Output JSON path.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing_nodes: dict = {}
    if out_path.exists():
        try:
            old_graph = json.loads(out_path.read_text(encoding="utf-8"))
            existing_nodes = {n["id"]: n for n in old_graph.get("nodes", []) if n.get("kind") == "file"}
        except Exception:
            pass

    graph = scan(root, existing_nodes=existing_nodes)
    out_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")

    sym_index = {
        node["id"]: {
            "line_start": node["line_start"], "line_end": node["line_end"],
            "body_hash": node["body_hash"], "confidence": node.get("confidence", ""),
            "path": node["path"],
        }
        for node in graph["nodes"] if node.get("kind") == "symbol"
    }
    sym_index_path = out_path.parent / "symbol_index.json"
    sym_index_path.write_text(json.dumps(sym_index), encoding="utf-8")

    mode = "AST+tree-sitter" if graph.get("ast_mode") else "regex-fallback"
    print(f"[{mode}] {graph['file_count']} files, {graph['symbol_count']} symbols, {graph['edge_count']} edges")
    print(f"Dead exports: {graph['dead_export_count']}")
    print(f"Wrote: {out_path}")
    print(f"Symbol index: {sym_index_path} ({len(sym_index)} symbols)")


if __name__ == "__main__":
    main()
