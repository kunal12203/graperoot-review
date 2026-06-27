"""
Language-specific symbol extractors for graph_builder_v6.2.

Supports: Java, Kotlin, Ruby (+ Rails), SQL DDL, Bash/Shell, Scala, C#

Each extractor returns list[dict] with keys:
    id, name, symbol_type, line_start, line_end, body_hash, confidence, exported, keywords

Symbol types: model | use_case | api_route | utility | hook
"""

from __future__ import annotations

import re
import hashlib
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _body_hash(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start:end + 1])
    return hashlib.md5(body.encode()).hexdigest()[:8]


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
            seen.add(w)
            tokens.append(w)

    add(name)
    for p in _split_camel(name):
        add(p)
    for p in name.split("_"):
        add(p)
    return tokens[:10]


def _find_block_end_brace(lines: list[str], start: int, limit: int = 400) -> int:
    """Brace counting for Java/Kotlin/Scala/C#/Shell blocks."""
    depth = 0
    found_open = False
    end = min(start + limit, len(lines))
    for i in range(start, end):
        line = lines[i]
        opens = line.count("{") - line.count("\\{")
        closes = line.count("}") - line.count("\\}")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + 100, len(lines) - 1)


def _find_block_end_brace_robust(lines: list[str], start: int, limit: int = 400) -> int:
    """Brace counting that skips strings and comments (Java/Kotlin)."""
    depth = 0
    found_open = False
    in_multiline_comment = False
    end = min(start + limit, len(lines))

    for i in range(start, end):
        line = lines[i]
        j = 0
        in_string = False
        string_char = None
        in_line_comment = False

        while j < len(line):
            ch = line[j]
            if in_multiline_comment:
                if ch == '*' and j + 1 < len(line) and line[j + 1] == '/':
                    in_multiline_comment = False
                    j += 2
                    continue
                j += 1
                continue
            if in_line_comment:
                break
            if in_string:
                if ch == '\\':
                    j += 2
                    continue
                if ch == string_char:
                    in_string = False
                j += 1
                continue
            if ch == '/' and j + 1 < len(line):
                next_ch = line[j + 1]
                if next_ch == '/':
                    break
                elif next_ch == '*':
                    in_multiline_comment = True
                    j += 2
                    continue
            if ch in ('"', "'"):
                in_string = True
                string_char = ch
                j += 1
                continue
            if ch == '{':
                depth += 1
                if not found_open:
                    found_open = True
            elif ch == '}':
                depth -= 1
            if found_open and depth <= 0:
                return i
            j += 1

    return min(start + 100, len(lines) - 1)


def _is_in_comment(content: str, match_start: int) -> bool:
    """Check if a position is inside a comment (Java/Kotlin/C#)."""
    line_start = content.rfind('\n', 0, match_start) + 1
    line_prefix = content[line_start:match_start]
    if '//' in line_prefix:
        return True
    stripped = line_prefix.lstrip()
    if stripped.startswith('*') and not stripped.startswith('*/'):
        return True
    in_block = False
    i = 0
    text = content[:match_start]
    while i < len(text):
        if in_block:
            if text[i:i + 2] == '*/':
                in_block = False
                i += 2
                continue
        else:
            if text[i:i + 2] == '/*':
                in_block = True
                i += 2
                continue
            if text[i:i + 2] == '//':
                nl = text.find('\n', i)
                if nl == -1:
                    break
                i = nl + 1
                continue
        i += 1
    return in_block


# ═══════════════════════════════════════════════════════════════════════════════
# JAVA
# ═══════════════════════════════════════════════════════════════════════════════

JAVA_CLASS_RE = re.compile(
    r'^(?:[ \t]*(?:@\w+(?:\([^)]*\))?\s*\n)*[ \t]*)'
    r'(?:public\s+|protected\s+|private\s+)?(?:abstract\s+|final\s+|static\s+|sealed\s+)*'
    r'(?:class|interface|enum|record)\s+(\w+)',
    re.MULTILINE,
)

JAVA_METHOD_RE = re.compile(
    r'^\s{2,}(?:@Override\s+)?'
    r'(public|protected|private)\s+'
    r'(?:(?:static|final|synchronized|default|native|abstract)\s+)*'
    r'(?:<[^>]{0,80}>\s+)?'
    r'(?!(?:class|interface|enum|record)\b)'
    r'(?:\w[\w.<>\[\]]*\s+)+'
    r'(\w+)\s*\(',
    re.MULTILINE,
)

JAVA_IFACE_METHOD_RE = re.compile(
    r'^[ \t]{4}(?:@\w+(?:\([^)]*\))?\s+)*'
    r'(?!(?:public|protected|private|class|interface|enum|record|static|final|default|abstract)\b)'
    r'(?:<[^>]{0,80}>\s+)?'
    r'(?:\w[\w.<>\[\]]*\s+)+'
    r'(\w+)\s*\(',
    re.MULTILINE,
)

JAVA_SPRING_STEREOTYPE_RE = re.compile(
    r'@(Service|Controller|RestController|Component|Repository|Configuration)'
    r'(?:\s*\(\s*(?:value\s*=\s*)?"([^"]*)"\s*\))?',
)

JAVA_SPRING_MAPPING_RE = re.compile(
    r'@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)',
)

JAVA_JPA_ENTITY_RE = re.compile(
    r'@(Entity|MappedSuperclass|Embeddable)',
)

JAVA_CLASS_TYPE_MAP: dict[str, str] = {
    "RestController": "api_route",
    "Controller": "api_route",
    "Service": "use_case",
    "Component": "use_case",
    "Repository": "model",
    "Entity": "model",
    "MappedSuperclass": "model",
    "Embeddable": "model",
    "Configuration": "use_case",
}


def extract_symbols_java(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    is_interface = bool(re.search(
        r'^\s*(?:public\s+)?interface\s+\w+', content, re.MULTILINE
    ))

    class_annotations: list[str] = []
    for m in JAVA_SPRING_STEREOTYPE_RE.finditer(content):
        class_annotations.append(m.group(1))
    if JAVA_JPA_ENTITY_RE.search(content):
        class_annotations.append("Entity")

    for m in JAVA_CLASS_RE.finditer(content):
        name = m.group(1)
        if not name or name in seen:
            continue
        if _is_in_comment(content, m.start()):
            continue
        seen.add(name)
        line_no = content[:m.start()].count("\n")
        end = _find_block_end_brace_robust(lines, line_no)

        sym_type = "utility"
        for ann in class_annotations:
            if ann in JAVA_CLASS_TYPE_MAP:
                sym_type = JAVA_CLASS_TYPE_MAP[ann]
                break

        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": "high",
            "exported": True,
            "keywords": _name_keywords(name),
        })

    if is_interface:
        for m in JAVA_IFACE_METHOD_RE.finditer(content):
            name = m.group(1)
            if not name or name in seen or name[0].isupper():
                continue
            if _is_in_comment(content, m.start()):
                continue
            seen.add(name)
            line_no = content[:m.start()].count("\n")
            end = min(line_no + 5, len(lines) - 1)
            if line_no < len(lines) and "{" in lines[line_no]:
                end = _find_block_end_brace_robust(lines, line_no)
            symbols.append({
                "id": f"{file_path}::{name}",
                "name": name,
                "symbol_type": "use_case",
                "line_start": line_no,
                "line_end": end,
                "body_hash": _body_hash(lines, line_no, end),
                "confidence": "medium",
                "exported": True,
                "keywords": _name_keywords(name),
            })

    for m in JAVA_METHOD_RE.finditer(content):
        visibility = m.group(1)
        name = m.group(2)
        if not name or name in seen:
            continue
        if _is_in_comment(content, m.start()):
            continue
        seen.add(name)
        line_no = content[:m.start()].count("\n")
        end = _find_block_end_brace_robust(lines, line_no)
        exported = visibility in ("public", "protected")

        pre = content[max(0, m.start() - 200):m.start()]
        if JAVA_SPRING_MAPPING_RE.search(pre):
            sym_type = "api_route"
        elif any(a in ("RestController", "Controller") for a in class_annotations):
            sym_type = "use_case"
        elif exported:
            sym_type = "use_case"
        else:
            sym_type = "utility"

        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": "high",
            "exported": exported,
            "keywords": _name_keywords(name),
        })

    return symbols


def _parse_imports_java(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'^\s*import\s+(?:static\s+)?([a-zA-Z0-9_.]+)', content, re.MULTILINE):
        pkg = m.group(1)
        if pkg not in seen:
            seen.add(pkg)
            edges.append({"from": file_id, "to": pkg, "rel": "imports"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# KOTLIN
# ═══════════════════════════════════════════════════════════════════════════════

KOTLIN_CLASS_RE = re.compile(
    r'^(?:[ \t]*)(?:public\s+|internal\s+|private\s+)?'
    r'(?:abstract\s+|open\s+|sealed\s+|data\s+|inner\s+)*'
    r'(?:class|interface|object|enum\s+class)\s+(\w+)',
    re.MULTILINE,
)

KOTLIN_FUN_RE = re.compile(
    r'^\s{2,}(?:override\s+)?(public\s+|internal\s+|private\s+|protected\s+)?'
    r'(?:suspend\s+)?(?:inline\s+)?fun\s+(?:<[^>]{0,60}>\s+)?(\w+)\s*\(',
    re.MULTILINE,
)

KOTLIN_TOP_FUN_RE = re.compile(
    r'^(?:public\s+|internal\s+|private\s+)?'
    r'(?:(?:suspend|inline|infix|operator|tailrec)\s+)*'
    r'fun\s+(?:<[^>]{0,60}>\s+)?(?:[\w<>,\s?*]+\.\s*)?(\w+)\s*\(',
    re.MULTILINE,
)

KOTLIN_TYPEALIAS_RE = re.compile(
    r'^[ \t]*(?:public\s+|internal\s+|private\s+)?typealias\s+(\w+)',
    re.MULTILINE,
)


def _find_expression_body_end(lines: list[str], start: int) -> int:
    if start >= len(lines):
        return start
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    last_content = start
    for i in range(start + 1, min(start + 50, len(lines))):
        line = lines[i]
        if not line.strip():
            return last_content
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            return last_content
        last_content = i
    return last_content


def _is_exported_kotlin(visibility: str | None) -> bool:
    if not visibility or visibility.strip() == "":
        return True
    return visibility.strip() not in ("private", "internal")


def extract_symbols_kotlin(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    class_annotations: list[str] = []
    for m in JAVA_SPRING_STEREOTYPE_RE.finditer(content):
        class_annotations.append(m.group(1))

    for m in KOTLIN_CLASS_RE.finditer(content):
        name = m.group(1)
        if not name or name in seen:
            continue
        if _is_in_comment(content, m.start()):
            continue
        seen.add(name)
        line_no = content[:m.start()].count("\n")
        end = _find_block_end_brace_robust(lines, line_no)

        sym_type = "utility"
        for ann in class_annotations:
            if ann in JAVA_CLASS_TYPE_MAP:
                sym_type = JAVA_CLASS_TYPE_MAP[ann]
                break

        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": "high",
            "exported": True,
            "keywords": _name_keywords(name),
        })

    for m in KOTLIN_FUN_RE.finditer(content):
        vis = (m.group(1) or "").strip()
        name = m.group(2)
        if not name or name in seen:
            continue
        if _is_in_comment(content, m.start()):
            continue
        seen.add(name)
        line_no = content[:m.start()].count("\n")

        if line_no < len(lines):
            rest = lines[line_no][lines[line_no].find(")"):] if ")" in lines[line_no] else ""
            if "=" in rest and "{" not in rest:
                end = _find_expression_body_end(lines, line_no)
            else:
                end = _find_block_end_brace_robust(lines, line_no)
        else:
            end = _find_block_end_brace_robust(lines, line_no)

        exported = _is_exported_kotlin(vis if vis else None)

        pre = content[max(0, m.start() - 200):m.start()]
        if JAVA_SPRING_MAPPING_RE.search(pre):
            sym_type = "api_route"
        elif exported:
            sym_type = "use_case"
        else:
            sym_type = "utility"

        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": "high",
            "exported": exported,
            "keywords": _name_keywords(name),
        })

    for m in KOTLIN_TOP_FUN_RE.finditer(content):
        name = m.group(1)
        if not name or name in seen:
            continue
        if _is_in_comment(content, m.start()):
            continue
        seen.add(name)
        line_no = content[:m.start()].count("\n")
        if line_no < len(lines) and "=" in lines[line_no] and "{" not in lines[line_no]:
            end = _find_expression_body_end(lines, line_no)
        else:
            end = _find_block_end_brace_robust(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": "use_case",
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": "high",
            "exported": True,
            "keywords": _name_keywords(name),
        })

    for m in KOTLIN_TYPEALIAS_RE.finditer(content):
        name = m.group(1)
        if not name or name in seen:
            continue
        if _is_in_comment(content, m.start()):
            continue
        seen.add(name)
        line_no = content[:m.start()].count("\n")
        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": "utility",
            "line_start": line_no,
            "line_end": line_no,
            "body_hash": _body_hash(lines, line_no, line_no),
            "confidence": "high",
            "exported": True,
            "keywords": _name_keywords(name),
        })

    return symbols


def _parse_imports_kotlin(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'^\s*import\s+([a-zA-Z0-9_.]+)', content, re.MULTILINE):
        pkg = m.group(1)
        if pkg not in seen:
            seen.add(pkg)
            edges.append({"from": file_id, "to": pkg, "rel": "imports"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# RUBY + RAILS
# ═══════════════════════════════════════════════════════════════════════════════

def _find_block_end_ruby(lines: list[str], start: int) -> int:
    OPENERS = re.compile(
        r"(?<!\w)(?:class|module|def|do|if|unless|while|until|begin|case|for)\b"
    )
    ENDER = re.compile(r"(?<!\w)end\b")

    depth = 0
    in_heredoc = False
    heredoc_delim = ""

    for i in range(start, min(start + 500, len(lines))):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        if in_heredoc:
            if stripped == heredoc_delim or stripped == heredoc_delim.strip("-~"):
                in_heredoc = False
            continue

        heredoc_match = re.search(r"<<[-~]?['\"]?([A-Za-z_]\w*)['\"]?", line)
        if heredoc_match:
            heredoc_delim = heredoc_match.group(1)
            before_heredoc = line[:heredoc_match.start()]
            depth += len(OPENERS.findall(before_heredoc))
            in_heredoc = True
            continue

        code = re.sub(r'#.*$', '', line)
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"'[^']*'", "''", code)

        openers_found = OPENERS.findall(code)
        for opener in openers_found:
            if opener in ("if", "unless", "while", "until"):
                before_kw = re.split(r'\b' + opener + r'\b', code)[0]
                if before_kw.strip():
                    continue
            depth += 1

        ends_found = ENDER.findall(code)
        depth -= len(ends_found)

        if i > start and depth <= 0:
            return i

    return min(start + 50, len(lines) - 1)


def _ruby_visibility_at(lines: list[str], line_no: int) -> str:
    for i in range(line_no - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped in ("private", "protected", "public"):
            return stripped
        if re.match(r"(private|protected|public)\s+:", stripped):
            continue
    return "public"


def extract_symbols_ruby(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str,
                exported: bool, qualifier: Optional[str] = None) -> None:
        full = f"{qualifier}.{name}" if qualifier else name
        if full in seen or not name:
            return
        seen.add(full)
        end = _find_block_end_ruby(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{full}",
            "name": full,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": confidence,
            "exported": exported,
            "keywords": _name_keywords(name),
        })

    # Classes
    for m in re.finditer(
        r"^\s*class\s+(?P<name>[A-Z]\w*(?:::[A-Z]\w*)*)"
        r"(?:\s*<\s*(?P<parent>[A-Z]\w*(?:::[A-Z]\w*)*))?",
        text, re.MULTILINE,
    ):
        name = m.group("name")
        parent = m.group("parent") or ""
        ln = line_of(m.start())
        is_model = parent in (
            "ApplicationRecord", "ActiveRecord::Base",
            "ApplicationController", "ActionController::Base",
        )
        sym_type = "model" if is_model or "Record" in parent else "model"
        add_sym(name, ln, sym_type, "high", True)

    # Modules
    for m in re.finditer(
        r"^\s*module\s+(?P<name>[A-Z]\w*(?:::[A-Z]\w*)*)", text, re.MULTILINE,
    ):
        add_sym(m.group("name"), line_of(m.start()), "model", "high", True)

    # Methods
    _RUBY_METHOD_RE = re.compile(
        r"^\s*def\s+(?P<receiver>self\.)?(?P<name>[A-Za-z_]\w*[?!=]?|<=>|[<>=!]=?|\[\]=?|[+\-*/%&|^~]|<<|>>|=~|!~)"
        r"(?:\s*\((?P<params>[^)]*)\))?",
        re.MULTILINE,
    )
    for m in _RUBY_METHOD_RE.finditer(text):
        name = m.group("name")
        is_class_method = bool(m.group("receiver"))
        ln = line_of(m.start())
        visibility = _ruby_visibility_at(lines, ln)
        exported = visibility == "public"
        if name.startswith("test_"):
            continue
        sym_type = "use_case"
        qualifier = "self" if is_class_method else None
        add_sym(name, ln, sym_type, "high", exported, qualifier)

    # ActiveRecord associations
    for m in re.finditer(
        r"^\s*(?P<type>belongs_to|has_many|has_one|has_and_belongs_to_many)\s+:(?P<name>\w+)",
        text, re.MULTILINE,
    ):
        assoc_name = f"{m.group('type')}:{m.group('name')}"
        add_sym(assoc_name, line_of(m.start()), "hook", "high", True)

    # Validations
    for m in re.finditer(r"^\s*(?:validates?|validate)\s+:(?P<name>\w+)", text, re.MULTILINE):
        add_sym(f"validate:{m.group('name')}", line_of(m.start()), "hook", "medium", True)

    # Callbacks
    _CALLBACK_RE = re.compile(
        r"^\s*(?:before_save|after_save|before_create|after_create|"
        r"before_update|after_update|before_destroy|after_destroy|"
        r"before_validation|after_validation|after_commit|after_rollback|"
        r"before_action|after_action|around_action|skip_before_action)"
        r"\s+:(?P<name>\w+)",
        re.MULTILINE,
    )
    for m in _CALLBACK_RE.finditer(text):
        cb_name = f"{m.group(0).split()[0].strip()}:{m.group('name')}"
        add_sym(cb_name, line_of(m.start()), "hook", "high", True)

    # Scopes
    for m in re.finditer(r"^\s*scope\s+:(?P<name>\w+)\s*,", text, re.MULTILINE):
        add_sym(f"scope:{m.group('name')}", line_of(m.start()), "hook", "high", True)

    # Rails routes
    _ROUTE_VERB_RE = re.compile(
        r"^\s*(?P<verb>get|post|put|patch|delete|head|options)\s+"
        r"['\"](?P<path>[^'\"]+)['\"]",
        re.MULTILINE,
    )
    for m in _ROUTE_VERB_RE.finditer(text):
        route_name = f"{m.group('verb').upper()} {m.group('path')}"
        add_sym(route_name, line_of(m.start()), "api_route", "high", True)

    for m in re.finditer(r"^\s*resources?\s+:(?P<name>\w+)", text, re.MULTILINE):
        add_sym(f"resources:{m.group('name')}", line_of(m.start()), "api_route", "high", True)

    for m in re.finditer(r"^\s*root\s+['\"](?P<target>[^'\"]+)['\"]", text, re.MULTILINE):
        add_sym(f"root:{m.group('target')}", line_of(m.start()), "api_route", "high", True)

    return symbols


def _parse_imports_ruby(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]", content, re.MULTILINE):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            edges.append({"from": file_id, "to": path, "rel": "imports"})
    for m in re.finditer(
        r"^\s*(?:include|extend|prepend)\s+([A-Z]\w*(?:::[A-Z]\w*)*)", content, re.MULTILINE,
    ):
        mod = m.group(1)
        if mod not in seen:
            seen.add(mod)
            edges.append({"from": file_id, "to": mod, "rel": "imports"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# SQL DDL
# ═══════════════════════════════════════════════════════════════════════════════

def _find_block_end_sql(lines: list[str], start: int) -> int:
    text_from_start = "\n".join(lines[start:])

    dollar_match = re.search(r'\$\$|\$[A-Za-z_]\w*\$', text_from_start)
    if dollar_match:
        delim = dollar_match.group(0)
        first_end = dollar_match.end()
        second = text_from_start.find(delim, first_end)
        if second >= 0:
            total_len = second + len(delim)
            semi = text_from_start.find(";", total_len)
            if semi >= 0:
                total_len = semi + 1
            end_line = start + text_from_start[:total_len].count("\n")
            return min(end_line, len(lines) - 1)

    begin_count = 0
    for i in range(start, min(start + 300, len(lines))):
        line = lines[i].strip().upper()
        if line.startswith("--") or line.startswith("/*"):
            continue
        if "BEGIN" in line:
            begin_count += line.count("BEGIN")
        if "END" in line:
            begin_count -= line.count("END")
            if begin_count <= 0 and i > start:
                if ";" in lines[i]:
                    return i
                if i + 1 < len(lines) and ";" in lines[i + 1]:
                    return i + 1
                return i

    for i in range(start, min(start + 200, len(lines))):
        code = re.sub(r'--.*$', '', lines[i])
        if ";" in code:
            return i

    return min(start + 30, len(lines) - 1)


def extract_symbols_sql(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_sql(lines, line_no)
        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": confidence,
            "exported": True,
            "keywords": _name_keywords(name),
        })

    # CREATE TABLE
    for m in re.finditer(
        r"^\s*CREATE\s+(?:(?:TEMPORARY|TEMP)\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        schema = (m.group(1) or "").rstrip(".")
        name = m.group(2)
        full_name = f"{schema}.{name}" if schema else name
        add_sym(full_name, line_of(m.start()), "model", "high")

    # CREATE VIEW
    for m in re.finditer(
        r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:MATERIALIZED|TEMPORARY|TEMP)\s+)?"
        r"VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        schema = (m.group(1) or "").rstrip(".")
        name = m.group(2)
        full_name = f"{schema}.{name}" if schema else name
        add_sym(full_name, line_of(m.start()), "model", "high")

    # CREATE FUNCTION
    for m in re.finditer(
        r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*\(",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        schema = (m.group(1) or "").rstrip(".")
        name = m.group(2)
        full_name = f"{schema}.{name}" if schema else name
        add_sym(full_name, line_of(m.start()), "use_case", "high")

    # CREATE PROCEDURE
    for m in re.finditer(
        r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:PROC|PROCEDURE)\s+(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        schema = (m.group(1) or "").rstrip(".")
        name = m.group(2)
        full_name = f"{schema}.{name}" if schema else name
        add_sym(full_name, line_of(m.start()), "use_case", "high")

    # CREATE TRIGGER
    for m in re.finditer(
        r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+"
        r"([A-Za-z_]\w*)\s+(?:BEFORE|AFTER|INSTEAD\s+OF)",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        add_sym(m.group(1), line_of(m.start()), "hook", "high")

    # CREATE INDEX
    for m in re.finditer(
        r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
        r"([A-Za-z_]\w*)\s+ON",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        add_sym(m.group(1), line_of(m.start()), "utility", "medium")

    # CREATE TYPE
    for m in re.finditer(
        r"^\s*CREATE\s+TYPE\s+(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s+(?:AS\s+)?",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        schema = (m.group(1) or "").rstrip(".")
        name = m.group(2)
        full_name = f"{schema}.{name}" if schema else name
        add_sym(full_name, line_of(m.start()), "model", "high")

    # ALTER TABLE
    for m in re.finditer(
        r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\s+"
        r"(ADD|DROP|RENAME|ALTER|MODIFY)",
        text, re.MULTILINE | re.IGNORECASE,
    ):
        table = m.group(1)
        action = m.group(2).upper()
        add_sym(f"ALTER:{table}.{action}", line_of(m.start()), "utility", "medium")

    # Migration detection
    flyway_match = re.match(r".*[/\\]V([\d.]+)__([^.]+)\.sql$", file_path)
    if flyway_match:
        add_sym(f"migration:V{flyway_match.group(1)}", 0, "utility", "high")

    alembic_match = re.search(r"--\s*[Rr]evision\s*(?:ID)?\s*:\s*([a-f0-9]+)", text)
    if alembic_match:
        add_sym(f"revision:{alembic_match.group(1)}", line_of(alembic_match.start()), "utility", "high")

    for m in re.finditer(r"--\s*changeset\s+\w+:([^\s]+)", text, re.IGNORECASE):
        add_sym(f"changeset:{m.group(1)}", line_of(m.start()), "utility", "high")

    return symbols


def _parse_imports_sql(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
        r"REFERENCES\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(", content, re.IGNORECASE,
    ):
        table = m.group(1)
        key = f"{file_id}->{table}"
        if key not in seen:
            seen.add(key)
            edges.append({"from": file_id, "to": table, "rel": "references"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# BASH / SHELL
# ═══════════════════════════════════════════════════════════════════════════════

def _find_block_end_shell(lines: list[str], start: int) -> int:
    depth = 0
    found_open = False
    in_heredoc = False
    heredoc_delim = ""

    for i in range(start, min(start + 200, len(lines))):
        line = lines[i]
        stripped = line.strip()

        if in_heredoc:
            if stripped == heredoc_delim or stripped.rstrip() == heredoc_delim:
                in_heredoc = False
            continue

        heredoc_match = re.search(r'<<-?\s*[\'"]?(\w+)[\'"]?', line)
        if heredoc_match:
            heredoc_delim = heredoc_match.group(1)
            in_heredoc = True
            before = line[:heredoc_match.start()]
            depth += before.count("{") - before.count("}")
            if before.count("{") > 0:
                found_open = True
            continue

        if stripped.startswith("#") and not stripped.startswith("#!"):
            continue

        code = re.sub(r'(?<!\\)#(?!!).*$', '', line)
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"'[^']*'", "''", code)

        opens = code.count("{")
        closes = code.count("}")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0 and i > start:
            return i

    return min(start + 30, len(lines) - 1)


def _classify_shell(name: str) -> tuple[str, str]:
    low = name.lower()
    if any(x in low for x in ("log", "print", "echo", "debug", "warn", "error", "die")):
        return "utility", "medium"
    if any(x in low for x in ("usage", "help", "version")):
        return "utility", "low"
    if any(x in low for x in ("main", "run", "start", "exec", "init", "setup", "install", "deploy", "build")):
        return "use_case", "high"
    return "use_case", "high"


def extract_symbols_shell(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str, exported: bool) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_shell(lines, line_no)
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

    # function keyword style
    for m in re.finditer(
        r"^\s*function\s+([A-Za-z_][A-Za-z0-9_.-]*)\s*(?:\(\s*\))?\s*\{?",
        text, re.MULTILINE,
    ):
        name = m.group(1)
        sym_type, conf = _classify_shell(name)
        add_sym(name, line_of(m.start()), sym_type, conf, True)

    # POSIX style: name() {
    _SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "do", "done",
                       "for", "until", "case", "esac", "select", "in", "function",
                       "time", "coproc", "test"}
    for m in re.finditer(
        r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*\(\s*\)\s*\{?", text, re.MULTILINE,
    ):
        name = m.group(1)
        if name in _SHELL_KEYWORDS or name in seen:
            continue
        sym_type, conf = _classify_shell(name)
        add_sym(name, line_of(m.start()), sym_type, conf, True)

    # Exported variables
    for m in re.finditer(r"^\s*export\s+([A-Z][A-Z0-9_]*)=", text, re.MULTILINE):
        add_sym(m.group(1), line_of(m.start()), "utility", "low", True)

    return symbols


def _parse_imports_shell(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"^\s*(?:source|\.)\s+[\"']?([^\s\"'#]+)[\"']?", content, re.MULTILINE):
        path = m.group(1)
        if path and path not in seen:
            seen.add(path)
            edges.append({"from": file_id, "to": path, "rel": "imports"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# SCALA
# ═══════════════════════════════════════════════════════════════════════════════

def _find_block_end_scala(lines: list[str], start: int) -> int:
    for i in range(start, min(start + 3, len(lines))):
        if "{" in lines[i]:
            return _find_block_end_brace(lines, start, 400)

    if start >= len(lines):
        return start
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    last_content = start
    DECL_START = re.compile(
        r"^\s*(?:(?:private|protected|override|abstract|sealed|final|lazy|implicit|inline)\s+)*"
        r"(?:def|val|var|class|object|trait|type|enum|case|import|package)\b"
    )
    for i in range(start + 1, min(start + 200, len(lines))):
        line = lines[i]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent and DECL_START.match(line):
            return last_content
        if indent < base_indent and line.strip() == "}":
            return last_content
        last_content = i
    return last_content


def _classify_scala(name: str, kind: str) -> tuple[str, str]:
    if kind in ("class", "object", "trait", "case_class", "case_object", "enum"):
        return "model", "high"
    low = name.lower()
    if any(x in low for x in ("handle", "route", "endpoint", "serve", "controller")):
        return "api_route", "high"
    return "use_case", "high"


def extract_symbols_scala(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str, exported: bool) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_scala(lines, line_no)
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

    # Objects
    for m in re.finditer(
        r"^\s*(?:private(?:\[[^\]]*\])?\s+|protected(?:\[[^\]]*\])?\s+)?"
        r"(?:case\s+)?object\s+([A-Z]\w*)",
        text, re.MULTILINE,
    ):
        name = m.group(1)
        vis_prefix = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        exported = "private" not in vis_prefix
        sym_type, conf = _classify_scala(name, "object")
        add_sym(name, line_of(m.start()), sym_type, conf, exported)

    # Classes
    for m in re.finditer(
        r"^\s*(?:private(?:\[[^\]]*\])?\s+|protected(?:\[[^\]]*\])?\s+)?"
        r"(?:(?:abstract|sealed|final|implicit|inline|open)\s+)*"
        r"(?:case\s+)?class\s+([A-Z]\w*)",
        text, re.MULTILINE,
    ):
        name = m.group(1)
        if name in seen:
            continue
        line_text = text[text.rfind("\n", 0, m.start()) + 1:m.end()]
        exported = "private" not in line_text
        kind = "case_class" if "case" in line_text else "class"
        sym_type, conf = _classify_scala(name, kind)
        add_sym(name, line_of(m.start()), sym_type, conf, exported)

    # Traits
    for m in re.finditer(
        r"^\s*(?:private(?:\[[^\]]*\])?\s+|protected(?:\[[^\]]*\])?\s+)?"
        r"(?:sealed\s+)?trait\s+([A-Z]\w*)",
        text, re.MULTILINE,
    ):
        name = m.group(1)
        if name in seen:
            continue
        line_text = text[text.rfind("\n", 0, m.start()) + 1:m.end()]
        exported = "private" not in line_text
        sym_type, conf = _classify_scala(name, "trait")
        add_sym(name, line_of(m.start()), sym_type, conf, exported)

    # Enums (Scala 3)
    for m in re.finditer(r"^\s*enum\s+([A-Z]\w*)", text, re.MULTILINE):
        name = m.group(1)
        if name not in seen:
            add_sym(name, line_of(m.start()), "model", "high", True)

    # Methods
    for m in re.finditer(
        r"^\s*(?:private(?:\[[^\]]*\])?\s+|protected(?:\[[^\]]*\])?\s+)?"
        r"(?:(?:override|abstract|final|implicit|inline|lazy)\s+)*"
        r"def\s+([a-z_]\w*|[A-Z]\w*)",
        text, re.MULTILINE,
    ):
        name = m.group(1)
        if name in seen:
            continue
        ln = line_of(m.start())
        line_text = text[text.rfind("\n", 0, m.start()) + 1:m.end()]
        exported = "private" not in line_text
        sym_type, conf = _classify_scala(name, "def")
        add_sym(name, ln, sym_type, conf, exported)

    # Type aliases
    for m in re.finditer(r"^\s*(?:private(?:\[[^\]]*\])?\s+)?type\s+([A-Z]\w*)\s*(?:\[[^\]]*\])?\s*=", text, re.MULTILINE):
        name = m.group(1)
        if name not in seen:
            add_sym(name, line_of(m.start()), "model", "medium", True)

    return symbols


def _parse_imports_scala(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"^\s*import\s+([A-Za-z_][\w.]*(?:\{[^}]+\}|_|\*)?)", content, re.MULTILINE):
        raw = m.group(1)
        if "{" in raw:
            module = raw[:raw.index("{")].rstrip(". ")
        elif raw.endswith("._") or raw.endswith(".*"):
            module = raw.rsplit(".", 1)[0]
        else:
            module = raw
        if module and module not in seen:
            seen.add(module)
            edges.append({"from": file_id, "to": module, "rel": "imports"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# C#
# ═══════════════════════════════════════════════════════════════════════════════

def _find_block_end_csharp(lines: list[str], start: int) -> int:
    line = lines[start] if start < len(lines) else ""
    if re.match(r"\s*namespace\s+[\w.]+\s*;", line):
        return len(lines) - 1
    if "=>" in line and ";" in line and "{" not in line:
        return start
    for i in range(start, min(start + 5, len(lines))):
        if "{" in lines[i]:
            return _find_block_end_brace(lines, start, 500)
        if ";" in lines[i] and "=>" in "\n".join(lines[start:i + 1]):
            return i
    return _find_block_end_brace(lines, start, 500)


def _csharp_visibility(vis_str: str) -> bool:
    vis = vis_str.strip().lower() if vis_str else ""
    if "public" in vis or "internal" in vis:
        return True
    if "private" in vis or "protected" in vis:
        return False
    return True


def _classify_csharp(name: str, kind: str, attributes: str) -> tuple[str, str]:
    attrs_lower = (attributes or "").lower()
    if kind in ("class", "interface", "record", "struct", "enum"):
        if name.endswith("Controller"):
            return "api_route", "high"
        return "model", "high"
    if kind == "method":
        if any(x in attrs_lower for x in ("httpget", "httppost", "httpput", "httpdelete", "httppatch")):
            return "api_route", "high"
        if "route" in attrs_lower:
            return "api_route", "high"
        return "use_case", "high"
    return "use_case", "high"


def extract_symbols_csharp(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str, exported: bool) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_csharp(lines, line_no)
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

    def _get_attributes(line_no: int) -> str:
        attrs: list[str] = []
        for i in range(line_no - 1, max(line_no - 10, -1), -1):
            stripped = lines[i].strip() if i < len(lines) else ""
            if stripped.startswith("[") and stripped.endswith("]"):
                attrs.append(stripped)
            elif stripped.startswith("//") or not stripped:
                continue
            else:
                break
        return " ".join(attrs)

    # Namespaces
    for m in re.finditer(r"^\s*namespace\s+([\w.]+)\s*[;{]", text, re.MULTILINE):
        add_sym(m.group(1), line_of(m.start()), "model", "low", True)

    # Types (class, interface, record, struct, enum)
    _TYPE_RE = re.compile(
        r"^\s*(?:(?:public|internal|private|protected|private\s+protected|protected\s+internal)\s+)?"
        r"(?:(?:abstract|sealed|static|partial|readonly|ref|unsafe|new|file)\s+)*"
        r"(class|interface|record(?:\s+struct)?|struct|enum)\s+"
        r"([A-Z]\w*)",
        re.MULTILINE,
    )
    for m in _TYPE_RE.finditer(text):
        kind_raw = m.group(1).split()[0]
        name = m.group(2)
        ln = line_of(m.start())
        vis_line = text[text.rfind("\n", 0, m.start()) + 1:m.start() + len(m.group(0))]
        exported = _csharp_visibility(vis_line)
        attrs = _get_attributes(ln)
        sym_type, conf = _classify_csharp(name, kind_raw, attrs)
        add_sym(name, ln, sym_type, conf, exported)

    # Methods
    _METHOD_RE = re.compile(
        r"^\s*(?:(?:public|internal|private|protected)\s+)?"
        r"(?:(?:static|virtual|override|abstract|sealed|async|extern|unsafe|new|partial)\s+)*"
        r"(?:[\w<>\[\],.\s?]+?)\s+"
        r"([A-Z]\w*|[a-z]\w*)\s*"
        r"(?:<[^>]*>)?\s*\([^)]*\)",
        re.MULTILINE,
    )
    _CS_NOT_METHODS = {"if", "else", "while", "for", "foreach", "switch", "catch",
                       "using", "lock", "return", "throw", "yield", "await",
                       "new", "typeof", "sizeof", "nameof", "default", "class",
                       "struct", "interface", "enum", "namespace", "delegate",
                       "event", "var", "get", "set", "add", "remove", "value"}

    for m in _METHOD_RE.finditer(text):
        name = m.group(1)
        if name in _CS_NOT_METHODS or name in seen:
            continue
        ln = line_of(m.start())
        vis_line = text[text.rfind("\n", 0, m.start()) + 1:m.start() + len(m.group(0))]
        exported = _csharp_visibility(vis_line)
        attrs = _get_attributes(ln)
        sym_type, conf = _classify_csharp(name, "method", attrs)
        add_sym(name, ln, sym_type, conf, exported)

    # ASP.NET route attributes -> api_route symbols
    for m in re.finditer(
        r"^\s*\[(Http(?:Get|Post|Put|Delete|Patch))(?:\(\"([^\"]*)\"\))?\]",
        text, re.MULTILINE,
    ):
        ln = line_of(m.start())
        attr = m.group(1)
        route = m.group(2) or ""
        for i in range(ln + 1, min(ln + 5, len(lines))):
            method_match = re.match(
                r"\s*(?:public|internal)?\s*(?:(?:static|virtual|override|async)\s+)*"
                r"[\w<>\[\],.?\s]+?\s+([A-Z]\w*)\s*[<(]",
                lines[i],
            )
            if method_match:
                action_name = method_match.group(1)
                route_sym = f"{attr}:{route}" if route else f"{attr}:{action_name}"
                if route_sym not in seen:
                    seen.add(route_sym)
                    end = _find_block_end_csharp(lines, i)
                    symbols.append({
                        "id": f"{file_path}::{route_sym}",
                        "name": route_sym,
                        "symbol_type": "api_route",
                        "line_start": ln,
                        "line_end": end,
                        "body_hash": _body_hash(lines, ln, end),
                        "confidence": "high",
                        "exported": True,
                        "keywords": _name_keywords(action_name) + [attr.lower()],
                    })
                break

    return symbols


def _parse_imports_csharp(content: str, file_id: str) -> list[dict]:
    edges: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
        r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;",
        content, re.MULTILINE,
    ):
        ns = m.group(1)
        if ns and ns not in seen:
            seen.add(ns)
            edges.append({"from": file_id, "to": ns, "rel": "imports"})
    return edges


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

LANG_EXTENSIONS: dict[str, str] = {
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    ".sql": "sql",
    ".ddl": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ksh": "shell",
    ".scala": "scala",
    ".sc": "scala",
    ".sbt": "scala",
    ".cs": "csharp",
    ".csx": "csharp",
}

_EXTRACTORS = {
    "java": extract_symbols_java,
    "kotlin": extract_symbols_kotlin,
    "ruby": extract_symbols_ruby,
    "sql": extract_symbols_sql,
    "shell": extract_symbols_shell,
    "scala": extract_symbols_scala,
    "csharp": extract_symbols_csharp,
}

_IMPORT_PARSERS = {
    "java": _parse_imports_java,
    "kotlin": _parse_imports_kotlin,
    "ruby": _parse_imports_ruby,
    "sql": _parse_imports_sql,
    "shell": _parse_imports_shell,
    "scala": _parse_imports_scala,
    "csharp": _parse_imports_csharp,
}


def extract_symbols_for_ext(content: str, file_path: str, ext: str) -> list[dict]:
    """Dispatch to correct extractor based on file extension. Returns [] if unsupported."""
    lang = LANG_EXTENSIONS.get(ext)
    if lang and lang in _EXTRACTORS:
        return _EXTRACTORS[lang](content, file_path)
    return []


def parse_imports_for_ext(content: str, file_id: str, ext: str) -> list[dict]:
    """Dispatch to correct import parser based on file extension. Returns [] if unsupported."""
    lang = LANG_EXTENSIONS.get(ext)
    if lang and lang in _IMPORT_PARSERS:
        return _IMPORT_PARSERS[lang](content, file_id)
    return []


def supports_ext(ext: str) -> bool:
    """Check if this module handles the given extension."""
    return ext in LANG_EXTENSIONS
