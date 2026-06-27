"""
Prisma schema symbol extractor for graph_builder_v6.2.

Handles .prisma files: model, enum, type, datasource, generator blocks.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional


def _body_hash(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start:end + 1])
    return hashlib.md5(body.encode()).hexdigest()[:8]


def _name_keywords(name: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(w: str) -> None:
        w = w.lower().strip("_")
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            tokens.append(w)

    add(name)
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    for p in parts.split():
        add(p)
    for p in name.split("_"):
        add(p)
    return tokens[:10]


def _find_block_end_brace(lines: list[str], start: int) -> int:
    """Brace counting for Prisma blocks (simple, no strings to worry about)."""
    depth = 0
    found_open = False
    for i in range(start, min(start + 300, len(lines))):
        line = lines[i]
        opens = line.count("{")
        closes = line.count("}")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + 50, len(lines) - 1)


# ── Prisma relation detection ──────────────────────────────────────────────────

_RELATION_RE = re.compile(
    r"^\s+\w+\s+(\w+)\s*(?:\[\])?\s*\??.*@relation",
    re.MULTILINE,
)

_FIELD_RE = re.compile(
    r"^\s+(?P<name>\w+)\s+(?P<type>\w+)(?P<arr>\[\])?(?P<opt>\?)?(?P<mods>[^/\n]*)",
    re.MULTILINE,
)

_UNIQUE_RE = re.compile(r"@unique", re.IGNORECASE)
_ID_RE = re.compile(r"@id\b")
_DEFAULT_RE = re.compile(r"@default\(([^)]+)\)")
_MAP_RE = re.compile(r'@map\("([^"]+)"\)')
_DB_MAP_RE = re.compile(r'@@map\("([^"]+)"\)')


def extract_symbols_prisma(content: str, file_path: str) -> list[dict]:
    """Extract symbols from Prisma schema files."""
    lines = content.splitlines()
    text = content
    symbols: list[dict] = []
    seen: set[str] = set()

    def line_of(pos: int) -> int:
        return text[:pos].count("\n")

    def add_sym(name: str, line_no: int, sym_type: str, confidence: str,
                exported: bool, extra_kw: Optional[list[str]] = None) -> None:
        if name in seen or not name:
            return
        seen.add(name)
        end = _find_block_end_brace(lines, line_no)
        kw = _name_keywords(name)
        if extra_kw:
            for w in extra_kw:
                w = w.lower()
                if len(w) >= 3 and w not in kw:
                    kw.append(w)
        symbols.append({
            "id": f"{file_path}::{name}",
            "name": name,
            "symbol_type": sym_type,
            "line_start": line_no,
            "line_end": end,
            "body_hash": _body_hash(lines, line_no, end),
            "confidence": confidence,
            "exported": exported,
            "keywords": kw[:10],
        })

    # ── model blocks ───────────────────────────────────────────────────────────
    # model User { ... }
    for m in re.finditer(r"^model\s+(\w+)\s*\{", text, re.MULTILINE):
        name = m.group(1)
        ln = line_of(m.start())
        add_sym(name, ln, "model", "high", True, ["prisma", "model"])

    # ── enum blocks ────────────────────────────────────────────────────────────
    # enum Role { ADMIN USER }
    for m in re.finditer(r"^enum\s+(\w+)\s*\{", text, re.MULTILINE):
        name = m.group(1)
        ln = line_of(m.start())
        add_sym(name, ln, "model", "high", True, ["prisma", "enum"])

    # ── type blocks (composite types, Prisma MongoDB) ──────────────────────────
    # type Address { street String city String }
    for m in re.finditer(r"^type\s+(\w+)\s*\{", text, re.MULTILINE):
        name = m.group(1)
        ln = line_of(m.start())
        add_sym(name, ln, "model", "high", True, ["prisma", "type"])

    # ── datasource blocks ──────────────────────────────────────────────────────
    for m in re.finditer(r"^datasource\s+(\w+)\s*\{", text, re.MULTILINE):
        name = m.group(1)
        ln = line_of(m.start())
        # Extract provider
        block_end = _find_block_end_brace(lines, ln)
        block_text = "\n".join(lines[ln:block_end + 1])
        provider_m = re.search(r'provider\s*=\s*"([^"]+)"', block_text)
        provider = provider_m.group(1) if provider_m else "unknown"
        add_sym(f"datasource:{name}", ln, "utility", "high", True,
                ["prisma", "datasource", provider])

    # ── generator blocks ───────────────────────────────────────────────────────
    for m in re.finditer(r"^generator\s+(\w+)\s*\{", text, re.MULTILINE):
        name = m.group(1)
        ln = line_of(m.start())
        add_sym(f"generator:{name}", ln, "utility", "medium", True, ["prisma", "generator"])

    # ── Extract @relation fields as hook symbols ───────────────────────────────
    # Parse inside model blocks to find relation fields
    for model_m in re.finditer(r"^model\s+(\w+)\s*\{", text, re.MULTILINE):
        model_name = model_m.group(1)
        model_ln = line_of(model_m.start())
        model_end = _find_block_end_brace(lines, model_ln)
        block_text = "\n".join(lines[model_ln:model_end + 1])

        # Find @relation annotated fields
        for rel_m in re.finditer(
            r"^\s+(?P<field>\w+)\s+(?P<type>\w+)(?P<arr>\[\])?\s*.*?@relation",
            block_text, re.MULTILINE,
        ):
            field_name = rel_m.group("field")
            target_type = rel_m.group("type")
            rel_sym_name = f"{model_name}.{field_name}"
            if rel_sym_name not in seen:
                seen.add(rel_sym_name)
                # Find line within original file
                field_abs_ln = model_ln + block_text[:rel_m.start()].count("\n")
                symbols.append({
                    "id": f"{file_path}::{rel_sym_name}",
                    "name": rel_sym_name,
                    "symbol_type": "hook",
                    "line_start": field_abs_ln,
                    "line_end": field_abs_ln,
                    "body_hash": _body_hash(lines, field_abs_ln, field_abs_ln),
                    "confidence": "high",
                    "exported": True,
                    "keywords": _name_keywords(field_name) + [target_type.lower(), "relation"],
                })

    return symbols


def _parse_imports_prisma(content: str, file_id: str) -> list[dict]:
    """Parse Prisma model references as import edges (model → related model)."""
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # Find all model names defined in this file
    local_models: set[str] = set()
    for m in re.finditer(r"^model\s+(\w+)\s*\{", content, re.MULTILINE):
        local_models.add(m.group(1))
    for m in re.finditer(r"^enum\s+(\w+)\s*\{", content, re.MULTILINE):
        local_models.add(m.group(1))

    # For each @relation, find the target model type
    for m in re.finditer(
        r"^\s+\w+\s+(?P<type>[A-Z]\w*)(?:\[\])?\s*.*?@relation",
        content, re.MULTILINE,
    ):
        target = m.group("type")
        if target not in local_models:
            key = (file_id, target)
            if key not in seen:
                seen.add(key)
                edges.append({"from": file_id, "to": target, "rel": "references"})

    return edges


# ── Dispatcher ─────────────────────────────────────────────────────────────────

PRISMA_EXTS = {".prisma"}


def extract_symbols_for_prisma(content: str, file_path: str, ext: str) -> list[dict]:
    if ext in PRISMA_EXTS:
        return extract_symbols_prisma(content, file_path)
    return []


def parse_imports_for_prisma(content: str, file_id: str, ext: str) -> list[dict]:
    if ext in PRISMA_EXTS:
        return _parse_imports_prisma(content, file_id)
    return []
