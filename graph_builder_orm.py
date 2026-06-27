"""
ORM model symbol extractor for graph_builder_v6.2.

Supports 6 major ORMs via regex-only detection (no AST / YAML libs):
  1. TypeORM    (.ts, .js)   — @Entity decorator
  2. Sequelize  (.js, .ts)   — class extends Model / .init()
  3. GORM       (.go)        — struct with gorm tags / gorm.io import
  4. Drizzle    (.ts)        — pgTable / mysqlTable / sqliteTable exports
  5. Mongoose   (.js, .ts)   — new mongoose.Schema + mongoose.model(...)
  6. SQLAlchemy (.py)        — class extends Base / db.Model

Public API
----------
ORM_EXTS          : set[str]  — all file extensions handled
supports_orm()    : fast pre-filter called on every file
extract_orm_symbols() : main dispatcher → list[symbol dict]
parse_orm_imports()   : reference edges between ORM models
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORM_EXTS: set[str] = {".ts", ".js", ".go", ".py"}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _body_hash(lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start : end + 1])
    return hashlib.md5(body.encode()).hexdigest()[:8]


def _name_keywords(name: str, extras: Optional[list[str]] = None) -> list[str]:
    """Split a CamelCase / snake_case identifier into keyword tokens."""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(w: str) -> None:
        w = w.lower().strip("_")
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            tokens.append(w)

    _add(name)
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    for p in parts.split():
        _add(p)
    for p in name.split("_"):
        _add(p)
    if extras:
        for e in extras:
            _add(e)
    return tokens[:10]


def _line_of(text: str, pos: int) -> int:
    """0-indexed line number for character position `pos`."""
    return text[:pos].count("\n")


def _find_block_end(lines: list[str], start: int, max_scan: int = 400) -> int:
    """Find the closing brace line for a block that opens at or after `start`."""
    depth = 0
    found_open = False
    for i in range(start, min(start + max_scan, len(lines))):
        line = lines[i]
        depth += line.count("{") - line.count("}")
        if line.count("{") > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + 60, len(lines) - 1)


def _make_symbol(
    file_path: str,
    name: str,
    symbol_type: str,
    line_start: int,
    line_end: int,
    lines: list[str],
    confidence: str = "high",
    exported: bool = True,
    extra_kw: Optional[list[str]] = None,
) -> dict:
    return {
        "id": f"{file_path}::{name}",
        "name": name,
        "symbol_type": symbol_type,
        "line_start": line_start,
        "line_end": line_end,
        "body_hash": _body_hash(lines, line_start, line_end),
        "confidence": confidence,
        "exported": exported,
        "keywords": _name_keywords(name, extra_kw),
    }


# ---------------------------------------------------------------------------
# 1. TypeORM
# ---------------------------------------------------------------------------

# @Entity() or @Entity('table')
_TYPEORM_ENTITY_RE = re.compile(
    r"@Entity\s*\(\s*(?:'([^']*)'|\"([^\"]*)\")?\s*\)",
)
_TYPEORM_CLASS_RE = re.compile(r"(?:export\s+)?class\s+(\w+)")
_TYPEORM_RELATION_RE = re.compile(
    r"@(ManyToOne|OneToMany|ManyToMany|OneToOne)\s*\(\s*\(\s*\)\s*=>\s*(\w+)"
)
_TYPEORM_IMPORT_RE = re.compile(
    r"""from\s+['"]typeorm['"]""",
)


def _extract_typeorm(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    i = 0
    while i < len(lines):
        line = lines[i]
        m_entity = _TYPEORM_ENTITY_RE.search(line)
        if m_entity:
            # look ahead for the class declaration (usually next non-blank line)
            table_name = m_entity.group(1) or m_entity.group(2) or ""
            entity_line = i
            class_name: Optional[str] = None
            for j in range(i + 1, min(i + 5, len(lines))):
                m_cls = _TYPEORM_CLASS_RE.search(lines[j])
                if m_cls:
                    class_name = m_cls.group(1)
                    i = j  # advance to class line
                    break
            if class_name and class_name not in seen:
                seen.add(class_name)
                end = _find_block_end(lines, i)
                extra = ["typeorm", "entity"]
                if table_name:
                    extra.append(table_name.lower())
                symbols.append(
                    _make_symbol(
                        file_path,
                        class_name,
                        "model",
                        entity_line,
                        end,
                        lines,
                        confidence="high",
                        exported=True,
                        extra_kw=extra,
                    )
                )
                # scan body for relation decorators
                body_text = "\n".join(lines[entity_line : end + 1])
                for m_rel in _TYPEORM_RELATION_RE.finditer(body_text):
                    rel_type = m_rel.group(1)   # e.g. ManyToOne
                    target = m_rel.group(2)      # e.g. Role
                    rel_name = f"{class_name}.{rel_type.lower()}_{target}"
                    if rel_name not in seen:
                        seen.add(rel_name)
                        # line inside the body
                        rel_line = entity_line + body_text[: m_rel.start()].count("\n")
                        symbols.append(
                            _make_symbol(
                                file_path,
                                rel_name,
                                "hook",
                                rel_line,
                                rel_line,
                                lines,
                                confidence="high",
                                exported=False,
                                extra_kw=["typeorm", rel_type.lower(), target.lower()],
                            )
                        )
        i += 1
    return symbols


# ---------------------------------------------------------------------------
# 2. Sequelize
# ---------------------------------------------------------------------------

_SEQ_EXTENDS_RE = re.compile(
    r"class\s+(\w+)\s+extends\s+Model\b"
)
_SEQ_INIT_RE = re.compile(
    r"(\w+)\.init\s*\("
)
_SEQ_DATATYPE_RE = re.compile(
    r"DataTypes\.(\w+)"
)
_SEQ_ASSOC_RE = re.compile(
    r"(\w+)\.(belongsTo|hasMany|hasOne|belongsToMany)\s*\(\s*(\w+)"
)
_SEQ_IMPORT_RE = re.compile(
    r"""['"]sequelize['"]"""
)


def _extract_sequelize(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # class Foo extends Model
    for m in _SEQ_EXTENDS_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        # gather DataTypes from the .init() block if present
        dt_kw: list[str] = []
        init_block = content[m.start() : content.find("\n\n", m.start()) + 200]
        for dt_m in _SEQ_DATATYPE_RE.finditer(init_block):
            kw = dt_m.group(1).lower()
            if kw not in dt_kw:
                dt_kw.append(kw)
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["sequelize", "model"] + dt_kw,
            )
        )

    # ModelName.init({...}) — catches define-style without explicit class extends
    for m in _SEQ_INIT_RE.finditer(content):
        name = m.group(1)
        # skip already-seen and obvious non-model names
        if name in seen or name in ("module", "exports", "app", "db", "sequelize"):
            continue
        # only treat as model if we can find DataTypes in the nearby init block
        init_start = m.start()
        init_snippet = content[init_start : init_start + 800]
        if not _SEQ_DATATYPE_RE.search(init_snippet):
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        dt_kw = [dt.group(1).lower() for dt in _SEQ_DATATYPE_RE.finditer(init_snippet)]
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "model",
                ln,
                end,
                lines,
                confidence="medium",
                exported=True,
                extra_kw=["sequelize", "model"] + dt_kw[:5],
            )
        )

    # Association hooks: Order.belongsTo(User)
    for m in _SEQ_ASSOC_RE.finditer(content):
        source = m.group(1)
        rel = m.group(2)
        target = m.group(3)
        hook_name = f"{source}.{rel}_{target}"
        if hook_name in seen:
            continue
        seen.add(hook_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                hook_name,
                "hook",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["sequelize", rel.lower(), source.lower(), target.lower()],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 3. GORM
# ---------------------------------------------------------------------------

_GORM_IMPORT_RE = re.compile(
    r'"(gorm\.io/gorm|github\.com/jinzhu/gorm)"'
)
_GORM_STRUCT_RE = re.compile(
    r"^type\s+(\w+)\s+struct\s*\{",
    re.MULTILINE,
)
_GORM_TAG_RE = re.compile(r'`[^`]*gorm:"[^"]*"[^`]*`')
_GORM_MODEL_EMBED_RE = re.compile(r"\bgorm\.Model\b")


def _extract_gorm(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    has_gorm_import = bool(_GORM_IMPORT_RE.search(content))
    if not has_gorm_import:
        return symbols

    for m in _GORM_STRUCT_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        body = "\n".join(lines[ln : end + 1])

        # Only include if gorm tags are present OR gorm.Model is embedded
        has_tags = bool(_GORM_TAG_RE.search(body))
        has_embed = bool(_GORM_MODEL_EMBED_RE.search(body))
        if not (has_tags or has_embed):
            continue

        seen.add(name)
        # Extract column/primaryKey keywords from gorm tags
        col_kw: list[str] = []
        for tag_m in re.finditer(r'gorm:"([^"]*)"', body):
            tag_val = tag_m.group(1)
            for directive in tag_val.split(";"):
                directive = directive.strip()
                if ":" in directive:
                    k, v = directive.split(":", 1)
                    if k.strip() in ("column", "type") and v.strip():
                        col_kw.append(v.strip().lower()[:20])
                elif directive in ("primaryKey", "primary_key", "autoIncrement"):
                    col_kw.append("primary_key")
                elif directive == "foreignKey":
                    col_kw.append("foreign_key")

        symbols.append(
            _make_symbol(
                file_path,
                name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=name[0].isupper(),
                extra_kw=["gorm", "model"] + col_kw[:5],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 4. Drizzle ORM
# ---------------------------------------------------------------------------

_DRIZZLE_TABLE_RE = re.compile(
    r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(pgTable|mysqlTable|sqliteTable|pgView|mysqlView)\s*\(",
)
_DRIZZLE_COLUMN_RE = re.compile(
    r"\b(varchar|integer|text|serial|boolean|timestamp|date|bigint|"
    r"decimal|numeric|real|float|uuid|json|jsonb|blob|char|smallint|"
    r"tinyint|mediumint|longtext|mediumtext|tinytext)\s*\("
)
_DRIZZLE_RELATIONS_RE = re.compile(
    r"(?:export\s+)?(?:const|let|var)\s+(\w+Relations)\s*=\s*relations\s*\("
)
_DRIZZLE_IMPORT_RE = re.compile(r"""['"]drizzle-orm""")


def _extract_drizzle(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for m in _DRIZZLE_TABLE_RE.finditer(content):
        var_name = m.group(1)
        table_fn = m.group(2)
        if var_name in seen:
            continue
        seen.add(var_name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        body = "\n".join(lines[ln : end + 1])

        col_kw = list(
            dict.fromkeys(cm.group(1).lower() for cm in _DRIZZLE_COLUMN_RE.finditer(body))
        )
        db_type = "postgres" if "pg" in table_fn.lower() else (
            "mysql" if "mysql" in table_fn.lower() else "sqlite"
        )
        exported = "export" in lines[ln] if ln < len(lines) else True

        symbols.append(
            _make_symbol(
                file_path,
                var_name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=exported,
                extra_kw=["drizzle", "table", db_type] + col_kw[:5],
            )
        )

    # relations(...) → hook
    for m in _DRIZZLE_RELATIONS_RE.finditer(content):
        rel_name = m.group(1)
        if rel_name in seen:
            continue
        seen.add(rel_name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                rel_name,
                "hook",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["drizzle", "relations"],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 5. Mongoose
# ---------------------------------------------------------------------------

_MONGO_SCHEMA_RE = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*new\s+(?:mongoose\.)?Schema\s*\("
)
_MONGO_MODEL_RE = re.compile(
    r"mongoose\.model\s*\(\s*['\"](\w+)['\"]\s*,\s*(\w+)"
)
# Also catch: const User = model('User', schema)
_MONGO_MODEL2_RE = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*model\s*\(\s*['\"](\w+)['\"]\s*,\s*(\w+)"
)
_MONGO_REF_RE = re.compile(r"ref\s*:\s*['\"](\w+)['\"]")
_MONGO_TYPE_RE = re.compile(
    r"\btype\s*:\s*(String|Number|Boolean|Date|Buffer|Mixed|ObjectId|Array|Map)\b"
)
_MONGO_IMPORT_RE = re.compile(r"""['"]mongoose['"]""")


def _extract_mongoose(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Map schema variable → (line_start, line_end, type keywords)
    schema_info: dict[str, tuple[int, int, list[str]]] = {}

    for m in _MONGO_SCHEMA_RE.finditer(content):
        var = m.group(1)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        body = "\n".join(lines[ln : end + 1])
        type_kw = list(
            dict.fromkeys(t.group(1).lower() for t in _MONGO_TYPE_RE.finditer(body))
        )
        ref_kw = [r.group(1).lower() for r in _MONGO_REF_RE.finditer(body)]
        schema_info[var] = (ln, end, type_kw + ref_kw)

    def _emit_model(name: str, schema_var: str) -> None:
        if name in seen:
            return
        seen.add(name)
        info = schema_info.get(schema_var)
        if info:
            ln, end, kw = info
        else:
            # fallback — find mongoose.model call line
            for mm in _MONGO_MODEL_RE.finditer(content):
                if mm.group(1) == name:
                    ln = _line_of(content, mm.start())
                    end = ln
                    kw = []
                    break
            else:
                return
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["mongoose", "model"] + list(kw)[:5],
            )
        )

    # mongoose.model('ModelName', schemaVar)
    for m in _MONGO_MODEL_RE.finditer(content):
        model_name = m.group(1)
        schema_var = m.group(2)
        _emit_model(model_name, schema_var)

    # const User = model('User', userSchema)
    for m in _MONGO_MODEL2_RE.finditer(content):
        model_name = m.group(2)  # string arg is canonical name
        schema_var = m.group(3)
        _emit_model(model_name, schema_var)

    return symbols


# ---------------------------------------------------------------------------
# 6. SQLAlchemy
# ---------------------------------------------------------------------------

_SA_IMPORT_RE = re.compile(r"from\s+sqlalchemy|import\s+sqlalchemy")
_SA_CLASS_RE = re.compile(
    r"^class\s+(\w+)\s*\(([^)]+)\)\s*:",
    re.MULTILINE,
)
_SA_TABLENAME_RE = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
_SA_COLUMN_RE = re.compile(
    r"\bColumn\s*\(\s*(String|Integer|Float|Boolean|Date|DateTime|"
    r"Text|BigInteger|Numeric|LargeBinary|Enum|JSON|ARRAY|UUID)\b"
)
_SA_RELATIONSHIP_RE = re.compile(
    r"\brelationship\s*\(\s*['\"](\w+)['\"]"
)
_SA_FLASK_BASES = re.compile(r"\b(db\.Model|Base|DeclarativeBase)\b")


def _extract_sqlalchemy(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    if not _SA_IMPORT_RE.search(content):
        return symbols

    for m in _SA_CLASS_RE.finditer(content):
        name = m.group(1)
        bases = m.group(2)
        if name in seen:
            continue

        # Must extend a SQLAlchemy base: Base, db.Model, DeclarativeBase, or
        # any identifier ending with Model/Base
        if not _SA_FLASK_BASES.search(bases):
            # loose check — accept anything ending in Base or Model
            if not re.search(r"\b\w*(Base|Model)\b", bases):
                continue

        ln = _line_of(content, m.start())
        # Find body end: scan until next same-or-lower indented class or end
        end = ln
        for j in range(ln + 1, min(ln + 200, len(lines))):
            stripped = lines[j]
            if stripped and not stripped[0].isspace() and (
                stripped.startswith("class ") or stripped.startswith("def ")
            ):
                end = j - 1
                break
            end = j

        body = "\n".join(lines[ln : end + 1])

        # Must have __tablename__ or at least one Column
        has_tablename = bool(_SA_TABLENAME_RE.search(body))
        has_column = bool(_SA_COLUMN_RE.search(body))
        if not (has_tablename or has_column):
            # Flask-SQLAlchemy files might just use db.Model without both
            if "db.Model" not in bases:
                continue

        seen.add(name)

        # Extract table name
        tbl_m = _SA_TABLENAME_RE.search(body)
        table_kw = [tbl_m.group(1).lower()] if tbl_m else []

        # Column type keywords
        col_kw = list(
            dict.fromkeys(c.group(1).lower() for c in _SA_COLUMN_RE.finditer(body))
        )

        # relationship targets → hook symbols
        for rel_m in _SA_RELATIONSHIP_RE.finditer(body):
            target = rel_m.group(1)
            hook_name = f"{name}.relationship_{target}"
            if hook_name not in seen:
                seen.add(hook_name)
                rel_line = ln + body[: rel_m.start()].count("\n")
                symbols.append(
                    _make_symbol(
                        file_path,
                        hook_name,
                        "hook",
                        rel_line,
                        rel_line,
                        lines,
                        confidence="high",
                        exported=False,
                        extra_kw=["sqlalchemy", "relationship", target.lower()],
                    )
                )

        symbols.append(
            _make_symbol(
                file_path,
                name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["sqlalchemy", "model"] + table_kw + col_kw[:4],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def supports_orm(content: str, file_path: str, ext: str) -> bool:
    """Fast pre-filter — True if this file likely contains ORM model definitions.

    Called on every file; must be cheap. Uses short-circuit string checks before
    falling back to regex.
    """
    if ext not in ORM_EXTS:
        return False

    # Quick substring checks (fastest possible)
    _quick = (
        "typeorm",
        "sequelize",
        "gorm.io/gorm",
        "jinzhu/gorm",
        "drizzle-orm",
        "mongoose",
        "sqlalchemy",
        "@Entity",
        "@Column",
        "extends Model",
        "extends db.Model",
        "pgTable",
        "mysqlTable",
        "sqliteTable",
    )
    c_lower = content[:4096]  # only scan the top of large files
    for token in _quick:
        if token in c_lower:
            return True
    return False


def extract_orm_symbols(content: str, file_path: str, ext: str) -> list[dict]:
    """Main dispatcher — returns symbols for all ORMs present in the file."""
    if ext not in ORM_EXTS:
        return []

    symbols: list[dict] = []

    if ext in (".ts", ".js"):
        # TypeORM: needs @Entity decorator
        if "@Entity" in content or "typeorm" in content:
            symbols.extend(_extract_typeorm(content, file_path))

        # Sequelize: needs extends Model or .init( with DataTypes
        if "sequelize" in content.lower() or "extends Model" in content:
            symbols.extend(_extract_sequelize(content, file_path))

        # Drizzle
        if "drizzle-orm" in content or "pgTable" in content or "mysqlTable" in content or "sqliteTable" in content:
            symbols.extend(_extract_drizzle(content, file_path))

        # Mongoose
        if "mongoose" in content:
            symbols.extend(_extract_mongoose(content, file_path))

    elif ext == ".go":
        if "gorm" in content:
            symbols.extend(_extract_gorm(content, file_path))

    elif ext == ".py":
        if "sqlalchemy" in content.lower():
            symbols.extend(_extract_sqlalchemy(content, file_path))

    # Deduplicate by id (in case two extractors overlap on a generic file)
    seen_ids: set[str] = set()
    unique: list[dict] = []
    for s in symbols:
        if s["id"] not in seen_ids:
            seen_ids.add(s["id"])
            unique.append(s)
    return unique


def parse_orm_imports(content: str, file_id: str, ext: str) -> list[dict]:
    """Return reference edges (source → target model name) between ORM models.

    Each edge dict:
    {
        "source": file_id,
        "target_name": str,   # model name; caller resolves to file_id
        "edge_type": "references",
        "orm": str,
    }
    """
    if ext not in ORM_EXTS:
        return []

    edges: list[dict] = []

    if ext in (".ts", ".js"):
        # TypeORM: @ManyToOne(() => TargetModel, ...)
        for m in _TYPEORM_RELATION_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(2),
                    "edge_type": "references",
                    "orm": "typeorm",
                }
            )

        # Sequelize: ModelName.belongsTo(Other), .hasMany, .hasOne, .belongsToMany
        for m in _SEQ_ASSOC_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(3),
                    "edge_type": "references",
                    "orm": "sequelize",
                }
            )

        # Drizzle: relations block — extract referenced table variables
        # relations(tableName, ({one, many}) => ({ other: one(OtherTable) }))
        for m in re.finditer(r"\bone\s*\(\s*(\w+)|\bmany\s*\(\s*(\w+)", content):
            target = m.group(1) or m.group(2)
            edges.append(
                {
                    "source": file_id,
                    "target_name": target,
                    "edge_type": "references",
                    "orm": "drizzle",
                }
            )

        # Mongoose: ref: 'ModelName'
        for m in _MONGO_REF_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "mongoose",
                }
            )

    elif ext == ".py":
        # SQLAlchemy: relationship('OtherModel')
        for m in _SA_RELATIONSHIP_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "sqlalchemy",
                }
            )

    return edges


# ---------------------------------------------------------------------------
# Inline tests (run with: python graph_builder_orm.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ── Test 1: TypeORM ───────────────────────────────────────────────────────
    typeorm_code = """
import { Entity, Column, PrimaryGeneratedColumn, ManyToOne } from 'typeorm'
import { Role } from './role.entity'

@Entity('users')
export class User {
    @PrimaryGeneratedColumn()
    id: number

    @Column()
    name: string

    @ManyToOne(() => Role, role => role.users)
    role: Role
}
"""
    syms = extract_orm_symbols(typeorm_code, "src/user.entity.ts", ".ts")
    assert any(
        s["name"] == "User" and s["symbol_type"] == "model" for s in syms
    ), f"No User model: {syms}"
    assert any(s["symbol_type"] == "hook" for s in syms), f"No relation hooks: {syms}"
    print("PASS  TypeORM")

    # ── Test 2: Sequelize ─────────────────────────────────────────────────────
    seq_code = """
const { Model, DataTypes } = require('sequelize')
class Order extends Model {}
Order.init({ total: DataTypes.FLOAT, status: DataTypes.STRING }, { sequelize })
Order.belongsTo(User)
"""
    syms = extract_orm_symbols(seq_code, "src/order.model.js", ".js")
    assert any(
        s["name"] == "Order" and s["symbol_type"] == "model" for s in syms
    ), f"No Order: {syms}"
    print("PASS  Sequelize")

    # ── Test 3: GORM ──────────────────────────────────────────────────────────
    gorm_code = '''
import "gorm.io/gorm"
type Product struct {
    gorm.Model
    Code  string `gorm:"column:code"`
    Price uint
}
'''
    syms = extract_orm_symbols(gorm_code, "models/product.go", ".go")
    assert any(
        s["name"] == "Product" and s["symbol_type"] == "model" for s in syms
    ), f"No Product: {syms}"
    print("PASS  GORM")

    # ── Test 4: Drizzle ───────────────────────────────────────────────────────
    drizzle_code = """
import { pgTable, serial, varchar, integer } from 'drizzle-orm/pg-core'
export const users = pgTable('users', {
    id: serial('id').primaryKey(),
    name: varchar('name', { length: 256 }),
    age: integer('age'),
})
"""
    syms = extract_orm_symbols(drizzle_code, "db/schema.ts", ".ts")
    assert any(
        s["name"] == "users" and s["symbol_type"] == "model" for s in syms
    ), f"No users table: {syms}"
    print("PASS  Drizzle")

    # ── Test 5: Mongoose ──────────────────────────────────────────────────────
    mongo_code = """
const mongoose = require('mongoose')
const userSchema = new mongoose.Schema({
    name: { type: String, required: true },
    role: { type: mongoose.Schema.Types.ObjectId, ref: 'Role' }
})
const User = mongoose.model('User', userSchema)
module.exports = User
"""
    syms = extract_orm_symbols(mongo_code, "models/user.js", ".js")
    assert any(
        s["name"] == "User" and s["symbol_type"] == "model" for s in syms
    ), f"No User: {syms}"
    print("PASS  Mongoose")

    # ── Test 6: SQLAlchemy ────────────────────────────────────────────────────
    sa_code = """
from sqlalchemy import Column, String, Integer
from sqlalchemy.ext.declarative import declarative_base
Base = declarative_base()
class Article(Base):
    __tablename__ = 'articles'
    id = Column(Integer, primary_key=True)
    title = Column(String)
"""
    syms = extract_orm_symbols(sa_code, "app/models.py", ".py")
    assert any(
        s["name"] == "Article" and s["symbol_type"] == "model" for s in syms
    ), f"No Article: {syms}"
    print("PASS  SQLAlchemy")

    print("\nALL ORM TESTS PASSED")
