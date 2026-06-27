"""
ORM model symbol extractor for graph_builder_v6.2.

Supports 15 major ORMs via regex-only detection (no AST / YAML libs):
  1.  TypeORM     (.ts, .js)      — @Entity decorator
  2.  Sequelize   (.js, .ts)      — class extends Model / .init()
  3.  GORM        (.go)           — struct with gorm tags / gorm.io import
  4.  Drizzle     (.ts)           — pgTable / mysqlTable / sqliteTable exports
  5.  Mongoose    (.js, .ts)      — new mongoose.Schema + mongoose.model(...)
  6.  SQLAlchemy  (.py)           — class extends Base / db.Model
  7.  EF Core     (.cs)           — DbContext / DbSet<T> / [Table(
  8.  Dapper      (.cs)           — connection.Query<T> / connection.Execute
  9.  Ent ORM     (.go)           — ent.Schema / entgo.io/ent
  10. Tortoise    (.py)           — tortoise.models / from tortoise
  11. SQLModel    (.py)           — SQLModel base class
  12. Exposed     (.kt, .kts)     — object extends *IdTable / Table
  13. Knex.js     (.js, .ts)      — knex.schema / knex(
  14. Kysely      (.ts)           — Kysely<T> / from 'kysely'
  15. Objection.js(.js, .ts)      — class extends Model + static tableName

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

ORM_EXTS: set[str] = {".ts", ".js", ".go", ".py", ".cs", ".kt", ".kts", ".rs", ".java", ".rb"}

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
# 7. EF Core (C#)
# ---------------------------------------------------------------------------

_EFCORE_DBCONTEXT_RE = re.compile(
    r"class\s+(\w+)\s*:\s*(?:IdentityDbContext|DbContext)\b"
)
_EFCORE_DBSET_RE = re.compile(
    r"public\s+DbSet<(\w+)>\s+(\w+)\s*\{"
)
_EFCORE_TABLE_RE = re.compile(
    r'\[Table\s*\(\s*["\']([^"\']+)["\']'
)
_EFCORE_CLASS_RE = re.compile(r"class\s+(\w+)\b")
_EFCORE_KEYATTR_RE = re.compile(r"\[Key\]|\[Column\(|\[ForeignKey\(")
_EFCORE_MIGRATION_RE = re.compile(
    r"class\s+(\w+)\s*:\s*Migration\b"
)
_EFCORE_HASONE_RE = re.compile(
    r"HasOne\s*\(.*?=>\s*\w+\.(\w+)\)"
)
_EFCORE_HASMANY_RE = re.compile(
    r"HasMany\s*\(.*?=>\s*\w+\.(\w+)\)"
)
_EFCORE_FK_ATTR_RE = re.compile(
    r'\[ForeignKey\s*\(\s*"(\w+)"\s*\)\]'
)


def _extract_efcore(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # DbContext classes
    for m in _EFCORE_DBCONTEXT_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "use_case",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["efcore", "dbcontext", "DbContext"],
            )
        )

    # DbSet<EntityType> properties  →  model symbol for the entity type
    for m in _EFCORE_DBSET_RE.finditer(content):
        entity_type = m.group(1)
        if entity_type in seen:
            continue
        seen.add(entity_type)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                entity_type,
                "model",
                ln,
                ln,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["efcore", "entity"],
            )
        )

    # Migration classes
    for m in _EFCORE_MIGRATION_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "utility",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["efcore", "migration"],
            )
        )

    # Entity classes: class with [Key] / [Column( / [ForeignKey( nearby
    # (scan by finding class declarations and checking the surrounding context)
    for m in _EFCORE_CLASS_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        # look 300 chars around the class declaration for EF attribute markers
        ctx_start = max(0, m.start() - 100)
        ctx_end = min(len(content), m.start() + 300)
        ctx = content[ctx_start:ctx_end]
        if not _EFCORE_KEYATTR_RE.search(ctx):
            continue
        # also check for [Table( annotation
        table_m = _EFCORE_TABLE_RE.search(ctx)
        extra = ["efcore", "entity"]
        if table_m:
            extra.append(table_m.group(1).lower())
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
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
                extra_kw=extra,
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 8. Dapper (C#)
# ---------------------------------------------------------------------------

_DAPPER_QUERY_RE = re.compile(
    r"connection\.Query(?:Async)?<(\w+)>"
)
_DAPPER_QUERY_SINGLE_RE = re.compile(
    r"connection\.QuerySingle(?:Async)?<(\w+)>"
)
_DAPPER_QUERY_FIRST_RE = re.compile(
    r"connection\.QueryFirst(?:Async)?<(\w+)>"
)
_DAPPER_EXECUTE_RE = re.compile(
    r'connection\.Execute(?:Async)?\s*\(\s*["\']([^"\']+)["\']'
)
_DAPPER_ADDHANDLER_RE = re.compile(
    r"SqlMapper\.AddTypeHandler<(\w+)>"
)
_DAPPER_TYPEHANDLER_RE = re.compile(
    r"class\s+(\w+)\s*:\s*SqlMapper\.TypeHandler<(\w+)>"
)


def _extract_dapper(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Query<T> / QueryAsync<T> → model for the generic type
    for pattern in (_DAPPER_QUERY_RE, _DAPPER_QUERY_SINGLE_RE, _DAPPER_QUERY_FIRST_RE):
        for m in pattern.finditer(content):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            ln = _line_of(content, m.start())
            symbols.append(
                _make_symbol(
                    file_path,
                    name,
                    "model",
                    ln,
                    ln,
                    lines,
                    confidence="high",
                    exported=True,
                    extra_kw=["dapper", "model"],
                )
            )

    # Execute("SQL ...") → use_case with truncated SQL as name hint
    for m in _DAPPER_EXECUTE_RE.finditer(content):
        sql_hint = m.group(1)[:40].replace("\n", " ").strip()
        name = f"Execute:{sql_hint}"
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "use_case",
                ln,
                ln,
                lines,
                confidence="medium",
                exported=False,
                extra_kw=["dapper", "execute"],
            )
        )

    # SqlMapper.AddTypeHandler<T>
    for m in _DAPPER_ADDHANDLER_RE.finditer(content):
        name = f"TypeHandler:{m.group(1)}"
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "hook",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["dapper", "typehandler", m.group(1).lower()],
            )
        )

    # class Foo : SqlMapper.TypeHandler<T>
    for m in _DAPPER_TYPEHANDLER_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "hook",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["dapper", "typehandler", m.group(2).lower()],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 9. Ent ORM (Go)
# ---------------------------------------------------------------------------

_ENT_SCHEMA_RE = re.compile(
    r"type\s+(\w+)\s+struct\s*\{[^}]*ent\.Schema[^}]*\}",
    re.DOTALL,
)
_ENT_FIELDS_RE = re.compile(
    r"func\s*\(\s*\w+\s+(\w+)\s*\)\s*Fields\s*\(\s*\)\s*\[\]ent\.Field"
)
_ENT_EDGES_RE = re.compile(
    r"func\s*\(\s*\w+\s+(\w+)\s*\)\s*Edges\s*\(\s*\)\s*\[\]ent\.Edge"
)
_ENT_GENERATE_RE = re.compile(r"entc\.Generate\s*\(")
_ENT_CLIENT_CREATE_RE = re.compile(r"client\.(\w+)\.Create\s*\(\s*\)")
_ENT_EDGE_TO_RE = re.compile(r'edge\.To\s*\(\s*"([^"]+)"')
_ENT_EDGE_FROM_RE = re.compile(r'edge\.From\s*\(\s*"([^"]+)"')
_ENT_EDGE_THROUGH_RE = re.compile(r'edge\.Through\s*\(\s*"([^"]+)"')


def _extract_ent(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Schema structs: type User struct { ent.Schema }
    for m in _ENT_SCHEMA_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
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
                extra_kw=["ent", "schema"],
            )
        )

    # Fields() / Edges() methods → ensure the schema class is registered
    for pattern in (_ENT_FIELDS_RE, _ENT_EDGES_RE):
        for m in pattern.finditer(content):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            ln = _line_of(content, m.start())
            end = _find_block_end(lines, ln)
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
                    extra_kw=["ent", "schema"],
                )
            )

    # entc.Generate → utility
    for m in _ENT_GENERATE_RE.finditer(content):
        name = "entc.Generate"
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "utility",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["ent", "generate"],
            )
        )

    # client.EntityName.Create() → use_case
    for m in _ENT_CLIENT_CREATE_RE.finditer(content):
        entity = m.group(1)
        uc_name = f"client.{entity}.Create"
        if uc_name in seen:
            continue
        seen.add(uc_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                uc_name,
                "use_case",
                ln,
                ln,
                lines,
                confidence="medium",
                exported=False,
                extra_kw=["ent", "client", entity.lower()],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 10. Tortoise ORM (Python)
# ---------------------------------------------------------------------------

_TORTOISE_MODEL_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*(?:tortoise\.)?Model\s*\)\s*:",
    re.MULTILINE,
)
_TORTOISE_META_TABLE_RE = re.compile(
    r'class\s+Meta\s*:.*?table\s*=\s*["\']([^"\']+)["\']',
    re.DOTALL,
)
_TORTOISE_REGISTER_RE = re.compile(r"@register_tortoise")
_TORTOISE_INIT_RE = re.compile(r"Tortoise\.init\s*\(")
_TORTOISE_FK_RE = re.compile(
    r'fields\.ForeignKeyField\s*\(\s*["\']([^"\']+)["\']'
)


def _extract_tortoise(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for m in _TORTOISE_MODEL_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        # find block end: scan until unindented class/def
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

        # extract table name from inner Meta class
        tbl_m = _TORTOISE_META_TABLE_RE.search(body)
        extra = ["tortoise", "model"]
        if tbl_m:
            extra.append(tbl_m.group(1).lower())

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
                extra_kw=extra,
            )
        )

    # @register_tortoise decorator
    for m in _TORTOISE_REGISTER_RE.finditer(content):
        if "register_tortoise" in seen:
            continue
        seen.add("register_tortoise")
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                "register_tortoise",
                "utility",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["tortoise", "register"],
            )
        )

    # Tortoise.init(...)
    for m in _TORTOISE_INIT_RE.finditer(content):
        if "Tortoise.init" in seen:
            continue
        seen.add("Tortoise.init")
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                "Tortoise.init",
                "utility",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["tortoise", "init"],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 11. SQLModel (Python)
# ---------------------------------------------------------------------------

_SQLMODEL_TABLE_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*SQLModel\s*,\s*table\s*=\s*True\s*\)\s*:",
    re.MULTILINE,
)
_SQLMODEL_DATA_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*SQLModel\s*\)\s*:",
    re.MULTILINE,
)
_SQLMODEL_FK_RE = re.compile(
    r'Field\s*\([^)]*foreign_key\s*=\s*["\']([^"\']+)["\']'
)
_SQLMODEL_REL_RE = re.compile(
    r'Relationship\s*\([^)]*back_populates\s*=\s*["\']([^"\']+)["\']'
)
_SQLMODEL_FK_TABLE_RE = re.compile(
    r'foreign_key\s*=\s*["\']([^"\'\.]+)\.\w+["\']'
)


def _extract_sqlmodel(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Table models: class Foo(SQLModel, table=True):
    for m in _SQLMODEL_TABLE_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
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

        # Collect FK fields → hook symbols
        for fk_m in _SQLMODEL_FK_RE.finditer(body):
            fk_name = f"{name}.fk_{fk_m.group(1).replace('.', '_')}"
            if fk_name not in seen:
                seen.add(fk_name)
                fk_ln = ln + body[: fk_m.start()].count("\n")
                symbols.append(
                    _make_symbol(
                        file_path,
                        fk_name,
                        "hook",
                        fk_ln,
                        fk_ln,
                        lines,
                        confidence="high",
                        exported=False,
                        extra_kw=["sqlmodel", "foreign_key"],
                    )
                )

        # Relationship(...) → hook symbols
        for rel_m in _SQLMODEL_REL_RE.finditer(body):
            rel_name = f"{name}.rel_{rel_m.group(1)}"
            if rel_name not in seen:
                seen.add(rel_name)
                rel_ln = ln + body[: rel_m.start()].count("\n")
                symbols.append(
                    _make_symbol(
                        file_path,
                        rel_name,
                        "hook",
                        rel_ln,
                        rel_ln,
                        lines,
                        confidence="high",
                        exported=False,
                        extra_kw=["sqlmodel", "relationship"],
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
                extra_kw=["sqlmodel", "table"],
            )
        )

    # Data-only models: class Foo(SQLModel):
    for m in _SQLMODEL_DATA_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = ln
        for j in range(ln + 1, min(ln + 200, len(lines))):
            stripped = lines[j]
            if stripped and not stripped[0].isspace() and (
                stripped.startswith("class ") or stripped.startswith("def ")
            ):
                end = j - 1
                break
            end = j
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "utility",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["sqlmodel", "data"],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 12. Exposed (Kotlin)
# ---------------------------------------------------------------------------

_EXPOSED_IDTABLE_RE = re.compile(
    r"^object\s+(\w+)\s*:\s*(?:Int|Long|UUID)?IdTable(?:<[^>]+>)?\s*\(",
    re.MULTILINE,
)
_EXPOSED_TABLE_RE = re.compile(
    r"^object\s+(\w+)\s*:\s*Table\s*\(",
    re.MULTILINE,
)
_EXPOSED_REFERENCES_RE = re.compile(
    r"\.references\s*\(\s*(\w+)\.\w+"
)
_EXPOSED_TRANSACTION_RE = re.compile(r"\btransaction\s*\{")
_EXPOSED_SCHEMA_CREATE_RE = re.compile(
    r"SchemaUtils\.create\s*\(([^)]+)\)"
)


def _extract_exposed(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # *IdTable objects
    for m in _EXPOSED_IDTABLE_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
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
                extra_kw=["exposed", "table"],
            )
        )

    # Simple Table objects
    for m in _EXPOSED_TABLE_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
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
                extra_kw=["exposed", "table"],
            )
        )

    # .references(OtherTable.col) → hook
    for m in _EXPOSED_REFERENCES_RE.finditer(content):
        target = m.group(1)
        hook_name = f"references_{target}"
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
                extra_kw=["exposed", "fk", target.lower()],
            )
        )

    # transaction { } → use_case
    for m in _EXPOSED_TRANSACTION_RE.finditer(content):
        ln = _line_of(content, m.start())
        uc_name = f"transaction_L{ln + 1}"
        if uc_name in seen:
            continue
        seen.add(uc_name)
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                uc_name,
                "use_case",
                ln,
                end,
                lines,
                confidence="medium",
                exported=False,
                extra_kw=["exposed", "transaction"],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 13. Knex.js (Node.js)
# ---------------------------------------------------------------------------

_KNEX_CREATE_TABLE_RE = re.compile(
    r"\.createTable\s*\(\s*[\"'`]([^\"'`]+)[\"'`]"
)
_KNEX_ALTER_TABLE_RE = re.compile(
    r"\.alterTable\s*\(\s*[\"'`]([^\"'`]+)[\"'`]"
)
_KNEX_QUERY_RE = re.compile(
    r"knex\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*\)"
)
_KNEX_MIGRATION_UP_RE = re.compile(
    r"exports\.up\s*=|export\s+.*?async\s+.*?\bup\b"
)
_KNEX_REFERENCES_RE = re.compile(
    r"\.references\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*\)"
)


def _extract_knex(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for pattern in (_KNEX_CREATE_TABLE_RE, _KNEX_ALTER_TABLE_RE):
        for m in pattern.finditer(content):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            ln = _line_of(content, m.start())
            end = _find_block_end(lines, ln)
            symbols.append(
                _make_symbol(
                    file_path,
                    name,
                    "model",
                    ln,
                    end,
                    lines,
                    confidence="high",
                    exported=False,
                    extra_kw=["knex", "table"],
                )
            )

    # knex('table') query references
    for m in _KNEX_QUERY_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "model",
                ln,
                ln,
                lines,
                confidence="medium",
                exported=False,
                extra_kw=["knex", "query"],
            )
        )

    # Migration file → utility
    if _KNEX_MIGRATION_UP_RE.search(content):
        name = "migration.up"
        if name not in seen:
            seen.add(name)
            m = _KNEX_MIGRATION_UP_RE.search(content)
            assert m is not None
            ln = _line_of(content, m.start())
            symbols.append(
                _make_symbol(
                    file_path,
                    name,
                    "utility",
                    ln,
                    ln,
                    lines,
                    confidence="high",
                    exported=True,
                    extra_kw=["knex", "migration"],
                )
            )

    return symbols


# ---------------------------------------------------------------------------
# 14. Kysely (TypeScript)
# ---------------------------------------------------------------------------

_KYSELY_DB_INTERFACE_RE = re.compile(
    r"interface\s+(\w+)\s*\{[^}]*:\s*\w+Table\b",
    re.DOTALL,
)
_KYSELY_TABLE_INTERFACE_RE = re.compile(
    r"interface\s+(\w+)Table\s*\{"
)
_KYSELY_TABLE_TYPE_RE = re.compile(
    r"type\s+(\w+)Table\s*="
)
_KYSELY_INSTANCE_RE = re.compile(
    r"new\s+Kysely<(\w+)>\s*\("
)
_KYSELY_MIGRATOR_RE = re.compile(r"new\s+Migrator\s*\(")
_KYSELY_SELECT_FROM_RE = re.compile(
    r"\.selectFrom\s*\(\s*[\"'`]([^\"'`]+)[\"'`]"
)
_KYSELY_INSERT_INTO_RE = re.compile(
    r"\.insertInto\s*\(\s*[\"'`]([^\"'`]+)[\"'`]"
)


def _extract_kysely(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # Database interface (contains *Table fields)
    for m in _KYSELY_DB_INTERFACE_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "use_case",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["kysely", "database"],
            )
        )

    # Table interfaces: interface UserTable { ... }
    for pattern in (_KYSELY_TABLE_INTERFACE_RE, _KYSELY_TABLE_TYPE_RE):
        for m in pattern.finditer(content):
            # strip trailing "Table" from group(1) for the symbol name
            raw = m.group(1)
            name = f"{raw}Table"
            if name in seen:
                continue
            seen.add(name)
            ln = _line_of(content, m.start())
            end = _find_block_end(lines, ln)
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
                    extra_kw=["kysely", "table", raw.lower()],
                )
            )

    # new Kysely<DatabaseType>(...) → use_case
    for m in _KYSELY_INSTANCE_RE.finditer(content):
        db_type = m.group(1)
        uc_name = f"Kysely<{db_type}>"
        if uc_name in seen:
            continue
        seen.add(uc_name)
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                uc_name,
                "use_case",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["kysely", "instance", db_type.lower()],
            )
        )

    # new Migrator(...) → utility
    for m in _KYSELY_MIGRATOR_RE.finditer(content):
        if "Migrator" in seen:
            continue
        seen.add("Migrator")
        ln = _line_of(content, m.start())
        symbols.append(
            _make_symbol(
                file_path,
                "Migrator",
                "utility",
                ln,
                ln,
                lines,
                confidence="high",
                exported=False,
                extra_kw=["kysely", "migrator"],
            )
        )

    # .selectFrom('table') / .insertInto('table') → model (table name)
    for pattern in (_KYSELY_SELECT_FROM_RE, _KYSELY_INSERT_INTO_RE):
        for m in pattern.finditer(content):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            ln = _line_of(content, m.start())
            symbols.append(
                _make_symbol(
                    file_path,
                    name,
                    "model",
                    ln,
                    ln,
                    lines,
                    confidence="medium",
                    exported=False,
                    extra_kw=["kysely", "table"],
                )
            )

    return symbols


# ---------------------------------------------------------------------------
# 15. Objection.js (Node.js / TypeScript)
# ---------------------------------------------------------------------------

_OBJECTION_CLASS_RE = re.compile(
    r"class\s+(\w+)\s+extends\s+(?:\w+\.)?Model\b"
)
_OBJECTION_TABLENAME_RE = re.compile(
    r"static\s+tableName\s*=\s*[\"'`]([^\"'`]+)[\"'`]"
)
_OBJECTION_JSONSCHEMA_RE = re.compile(
    r"static\s+jsonSchema\s*=\s*\{"
)
_OBJECTION_RELATIONS_RE = re.compile(
    r"static\s+(?:get\s+)?relationMappings\s*"
)
_OBJECTION_MODELCLASS_RE = re.compile(
    r"modelClass\s*:\s*(\w+)"
)


def _extract_objection(content: str, file_path: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for m in _OBJECTION_CLASS_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        body = "\n".join(lines[ln : end + 1])

        extra = ["objection", "model"]

        # annotate table name
        tbl_m = _OBJECTION_TABLENAME_RE.search(body)
        if tbl_m:
            extra.append(tbl_m.group(1).lower())

        # jsonSchema → hook
        if _OBJECTION_JSONSCHEMA_RE.search(body):
            hook_name = f"{name}.jsonSchema"
            if hook_name not in seen:
                seen.add(hook_name)
                js_m = _OBJECTION_JSONSCHEMA_RE.search(body)
                assert js_m is not None
                js_ln = ln + body[: js_m.start()].count("\n")
                symbols.append(
                    _make_symbol(
                        file_path,
                        hook_name,
                        "hook",
                        js_ln,
                        js_ln,
                        lines,
                        confidence="high",
                        exported=False,
                        extra_kw=["objection", "jsonschema"],
                    )
                )

        # relationMappings → hook + extract modelClass references
        rel_m = _OBJECTION_RELATIONS_RE.search(body)
        if rel_m:
            hook_name = f"{name}.relationMappings"
            if hook_name not in seen:
                seen.add(hook_name)
                rel_ln = ln + body[: rel_m.start()].count("\n")
                rel_end = _find_block_end(lines, rel_ln)
                symbols.append(
                    _make_symbol(
                        file_path,
                        hook_name,
                        "hook",
                        rel_ln,
                        rel_end,
                        lines,
                        confidence="high",
                        exported=False,
                        extra_kw=["objection", "relations"],
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
                extra_kw=extra,
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 16. Diesel ORM (Rust)
# ---------------------------------------------------------------------------

_DIESEL_TABLE_MACRO_RE = re.compile(
    r"table!\s*\{[\s\n]*(\w+)\s*\(",
    re.MULTILINE,
)
_DIESEL_DERIVE_RE = re.compile(
    r"#\[derive\([^)]*(?:Queryable|Insertable)[^)]*\)\]"
)
_DIESEL_STRUCT_RE = re.compile(
    r"(?:pub\s+)?struct\s+(\w+)\s*\{"
)
_DIESEL_USE_CASE_FN_RE = re.compile(
    r"fn\s+(\w+)\s*\([^)]*\)\s*->\s*Result",
)
_DIESEL_USE_CASE_BODY_KEYWORDS = re.compile(
    r"\.get_results\(|\.first\(|\.insert_into\(|\.update\("
)
_DIESEL_BELONGS_TO_RE = re.compile(
    r"#\[belongs_to\s*\(\s*(\w+)"
)


def _extract_diesel(content: str, file_path: str, ext: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # table! { tablename (...) } → model
    for m in _DIESEL_TABLE_MACRO_RE.finditer(content):
        table_name = m.group(1)
        if table_name in seen:
            continue
        seen.add(table_name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                table_name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["diesel", "table"],
            )
        )

    # #[derive(Queryable)] / #[derive(Insertable)] on a struct → model
    i = 0
    while i < len(lines):
        line = lines[i]
        if _DIESEL_DERIVE_RE.search(line):
            # look ahead for the struct definition
            for j in range(i + 1, min(i + 6, len(lines))):
                m_struct = _DIESEL_STRUCT_RE.search(lines[j])
                if m_struct:
                    struct_name = m_struct.group(1)
                    if struct_name not in seen:
                        seen.add(struct_name)
                        end = _find_block_end(lines, j)
                        symbols.append(
                            _make_symbol(
                                file_path,
                                struct_name,
                                "model",
                                i,  # start at the derive line
                                end,
                                lines,
                                confidence="high",
                                exported=True,
                                extra_kw=["diesel", "model"],
                            )
                        )
                    break
        i += 1

    # fn_name() -> Result<...> with Diesel query methods → use_case
    for m in _DIESEL_USE_CASE_FN_RE.finditer(content):
        fn_name = m.group(1)
        if fn_name in seen:
            continue
        fn_start = m.start()
        # scan the function body (next ~50 lines from fn start)
        fn_ln = _line_of(content, fn_start)
        end = _find_block_end(lines, fn_ln)
        body = "\n".join(lines[fn_ln : end + 1])
        if not _DIESEL_USE_CASE_BODY_KEYWORDS.search(body):
            continue
        seen.add(fn_name)
        symbols.append(
            _make_symbol(
                file_path,
                fn_name,
                "use_case",
                fn_ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["diesel", "query"],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 17. Spring Data JPA / Hibernate (Java)
# ---------------------------------------------------------------------------

_JPA_ENTITY_RE = re.compile(r"@Entity\b")
_JPA_TABLE_RE = re.compile(
    r'@Table\s*\(\s*name\s*=\s*"([^"]+)"'
)
_JPA_CLASS_RE = re.compile(
    r"(?:public\s+)?(?:abstract\s+)?class\s+(\w+)\b"
)
_JPA_REPOSITORY_RE = re.compile(
    r"(?:public\s+)?interface\s+(\w+)\s+extends\s+"
    r"(?:JpaRepository|CrudRepository|PagingAndSortingRepository|JpaSpecificationExecutor)"
    r"[<\s]"
)
_JPA_MANY_TO_ONE_RE = re.compile(
    r"@ManyToOne\b[^;]*?(?:private|protected|public)\s+(\w+)\s+\w+"
)
_JPA_ONE_TO_MANY_RE = re.compile(
    r"@OneToMany\b[^;]*?(?:private|protected|public)\s+\w+<(\w+)>"
)
_JPA_MANY_TO_MANY_RE = re.compile(
    r"@ManyToMany\b[^;]*?(?:private|protected|public)\s+\w+<(\w+)>"
)
_JPA_ONE_TO_ONE_RE = re.compile(
    r"@OneToOne\b[^;]*?(?:private|protected|public)\s+(\w+)\s+\w+"
)
_JPA_RELATIONSHIP_ANNOTATION_RE = re.compile(
    r"@(OneToMany|ManyToOne|ManyToMany|OneToOne)\b"
)


def _extract_spring_data_jpa(content: str, file_path: str, ext: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    # @Entity classes → model
    i = 0
    while i < len(lines):
        line = lines[i]
        if _JPA_ENTITY_RE.search(line):
            # Find the class name within the next few lines
            # Also check for @Table(name=...) between @Entity and class
            table_name = ""
            class_name: Optional[str] = None
            entity_line = i
            for j in range(i, min(i + 10, len(lines))):
                tbl_m = _JPA_TABLE_RE.search(lines[j])
                if tbl_m:
                    table_name = tbl_m.group(1)
                cls_m = _JPA_CLASS_RE.search(lines[j])
                if cls_m and j >= i:
                    class_name = cls_m.group(1)
                    i = j
                    break
            if class_name and class_name not in seen:
                seen.add(class_name)
                end = _find_block_end(lines, i)
                body = "\n".join(lines[entity_line : end + 1])

                extra = ["jpa", "entity", "hibernate"]
                if table_name:
                    extra.append(table_name.lower())

                # Relationship annotations → hook symbols
                for rel_m in _JPA_RELATIONSHIP_ANNOTATION_RE.finditer(body):
                    rel_type = rel_m.group(1)
                    hook_name = f"{class_name}.{rel_type}"
                    if hook_name not in seen:
                        seen.add(hook_name)
                        rel_ln = entity_line + body[: rel_m.start()].count("\n")
                        symbols.append(
                            _make_symbol(
                                file_path,
                                hook_name,
                                "hook",
                                rel_ln,
                                rel_ln,
                                lines,
                                confidence="high",
                                exported=False,
                                extra_kw=["jpa", rel_type.lower()],
                            )
                        )

                # The entity model symbol itself
                sym = _make_symbol(
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
                if table_name:
                    sym["table_name"] = table_name
                symbols.append(sym)
        i += 1

    # @Repository interfaces → use_case
    for m in _JPA_REPOSITORY_RE.finditer(content):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        end = _find_block_end(lines, ln)
        symbols.append(
            _make_symbol(
                file_path,
                name,
                "use_case",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["jpa", "repository", "spring"],
            )
        )

    return symbols


# ---------------------------------------------------------------------------
# 18. Ruby ActiveRecord (Rails)
# ---------------------------------------------------------------------------

_AR_MODEL_RE = re.compile(
    r"^class\s+(\w+)\s*<\s*(?:ApplicationRecord|ActiveRecord::Base)\s*$",
    re.MULTILINE,
)
_AR_HAS_MANY_RE = re.compile(r"^\s*has_many\s+:(\w+)", re.MULTILINE)
_AR_HAS_ONE_RE = re.compile(r"^\s*has_one\s+:(\w+)", re.MULTILINE)
_AR_BELONGS_TO_RE = re.compile(r"^\s*belongs_to\s+:(\w+)", re.MULTILINE)
_AR_SCOPE_RE = re.compile(r"^\s*scope\s+:(\w+),", re.MULTILINE)


def _singularize_capitalize(name: str) -> str:
    """Basic singularize + CamelCase for a snake_case Rails association name.

    Examples: line_items → LineItem, users → User, categories → Category
    """
    # Singularize the last segment (handles plural forms)
    if name.endswith("ies"):
        singular = name[:-3] + "y"   # categories → category
    elif name.endswith("ses") or name.endswith("xes") or name.endswith("zes"):
        singular = name[:-2]         # addresses → address
    elif name.endswith("s") and not name.endswith("ss"):
        singular = name[:-1]         # users → user, line_items → line_item
    else:
        singular = name

    # Convert snake_case → CamelCase
    return "".join(part.capitalize() for part in singular.split("_"))


def _extract_activerecord(content: str, file_path: str, ext: str) -> list[dict]:
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for m in _AR_MODEL_RE.finditer(content):
        class_name = m.group(1)
        if class_name in seen:
            continue
        seen.add(class_name)
        ln = _line_of(content, m.start())

        # Find body end: scan for 'end' at root indent level
        end = ln
        depth = 0
        for j in range(ln + 1, min(ln + 300, len(lines))):
            stripped = lines[j].strip()
            if re.match(r"^(class|module|def|do)\b", stripped):
                depth += 1
            if stripped == "end":
                if depth == 0:
                    end = j
                    break
                depth -= 1
            end = j

        body = "\n".join(lines[ln : end + 1])

        # has_many, has_one, belongs_to → hook symbols
        for rel_pattern, rel_label in (
            (_AR_HAS_MANY_RE, "has_many"),
            (_AR_HAS_ONE_RE, "has_one"),
            (_AR_BELONGS_TO_RE, "belongs_to"),
        ):
            for rel_m in rel_pattern.finditer(body):
                assoc_name = rel_m.group(1)
                hook_name = f"{class_name}.{rel_label}_{assoc_name}"
                if hook_name not in seen:
                    seen.add(hook_name)
                    rel_ln = ln + body[: rel_m.start()].count("\n")
                    symbols.append(
                        _make_symbol(
                            file_path,
                            hook_name,
                            "hook",
                            rel_ln,
                            rel_ln,
                            lines,
                            confidence="high",
                            exported=False,
                            extra_kw=["activerecord", rel_label, assoc_name],
                        )
                    )

        # scope :name, → use_case
        for scope_m in _AR_SCOPE_RE.finditer(body):
            scope_name = f"{class_name}.scope_{scope_m.group(1)}"
            if scope_name not in seen:
                seen.add(scope_name)
                scope_ln = ln + body[: scope_m.start()].count("\n")
                symbols.append(
                    _make_symbol(
                        file_path,
                        scope_name,
                        "use_case",
                        scope_ln,
                        scope_ln,
                        lines,
                        confidence="high",
                        exported=False,
                        extra_kw=["activerecord", "scope", scope_m.group(1)],
                    )
                )

        symbols.append(
            _make_symbol(
                file_path,
                class_name,
                "model",
                ln,
                end,
                lines,
                confidence="high",
                exported=True,
                extra_kw=["activerecord", "model", "rails"],
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

    c_top = content[:4096]  # only scan the top of large files

    # Quick substring checks (fastest possible) — grouped by extension for speed
    if ext in (".ts", ".js"):
        _quick_tsjs = (
            "typeorm",
            "sequelize",
            "drizzle-orm",
            "mongoose",
            "@Entity",
            "@Column",
            "extends Model",
            "pgTable",
            "mysqlTable",
            "sqliteTable",
            # Knex
            "knex.schema",
            "require('knex')",
            'require("knex")',
            "from 'knex'",
            'from "knex"',
            # Kysely
            "Kysely<",
            "from 'kysely'",
            'from "kysely"',
            "new Kysely(",
            # Objection
            "static tableName",
            "relationMappings",
        )
        for token in _quick_tsjs:
            if token in c_top:
                return True
        return False

    if ext == ".go":
        _quick_go = (
            "gorm.io/gorm",
            "jinzhu/gorm",
            # Ent ORM
            "ent.Schema",
            "entgo.io/ent",
            "[]ent.Field",
            "[]ent.Edge",
        )
        for token in _quick_go:
            if token in c_top:
                return True
        return False

    if ext == ".py":
        _quick_py = (
            "sqlalchemy",
            # Tortoise ORM
            "tortoise.models",
            "from tortoise",
            # SQLModel
            "SQLModel",
            "from sqlmodel",
        )
        for token in _quick_py:
            if token in c_top:
                return True
        return False

    if ext == ".cs":
        _quick_cs = (
            # EF Core
            "DbContext",
            "DbSet<",
            "[Table(",
            "[Key]",
            "modelBuilder.Entity",
            # Dapper
            "connection.Query<",
            "connection.QueryAsync<",
            "connection.Execute(",
            "SqlMapper",
        )
        for token in _quick_cs:
            if token in c_top:
                return True
        return False

    if ext in (".kt", ".kts"):
        _quick_kt = (
            "IntIdTable",
            "LongIdTable",
            "UUIDIdTable",
            "IdTable",
            "transaction {",
            "org.jetbrains.exposed",
            "SchemaUtils",
        )
        for token in _quick_kt:
            if token in c_top:
                return True
        return False

    if ext == ".rs":
        _quick_rs = (
            "extern crate diesel",
            "use diesel::",
            "diesel::table!",
            "#[derive(Queryable)]",
            "#[derive(Insertable)]",
        )
        for token in _quick_rs:
            if token in c_top:
                return True
        # Also check for multi-derive on same line
        if "Queryable" in c_top or "Insertable" in c_top:
            if "diesel" in c_top:
                return True
        return False

    if ext == ".java":
        _quick_java = (
            "import javax.persistence.",
            "import jakarta.persistence.",
            "import org.springframework.data.jpa.",
            "@Entity",
            "@Repository",
        )
        for token in _quick_java:
            if token in c_top:
                return True
        return False

    if ext == ".rb":
        _quick_rb = (
            "< ApplicationRecord",
            "< ActiveRecord::Base",
        )
        for token in _quick_rb:
            if token in c_top:
                return True
        return False

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
        # Disambiguation: skip if this is clearly Objection (static tableName) or
        # Kysely — Sequelize-style .init() with DataTypes is the key indicator
        if "sequelize" in content.lower() or (
            "extends Model" in content and "DataTypes" in content
        ):
            symbols.extend(_extract_sequelize(content, file_path))

        # Drizzle
        if (
            "drizzle-orm" in content
            or "pgTable" in content
            or "mysqlTable" in content
            or "sqliteTable" in content
        ):
            symbols.extend(_extract_drizzle(content, file_path))

        # Mongoose
        if "mongoose" in content:
            symbols.extend(_extract_mongoose(content, file_path))

        # Knex.js — disambiguation from Kysely: Knex uses knex.schema.createTable
        # (not Kysely<) and from 'knex'
        if (
            "knex.schema" in content
            or "require('knex')" in content
            or 'require("knex")' in content
            or "from 'knex'" in content
            or 'from "knex"' in content
        ) and "Kysely<" not in content:
            symbols.extend(_extract_knex(content, file_path))

        # Kysely — .ts only (strongly typed)
        if ext == ".ts" and (
            "Kysely<" in content
            or "from 'kysely'" in content
            or 'from "kysely"' in content
            or "new Kysely(" in content
        ):
            symbols.extend(_extract_kysely(content, file_path))

        # Objection.js — disambiguation from Sequelize: uses static tableName, not .init()
        if "extends Model" in content and (
            "static tableName" in content
            or "static get relationMappings" in content
            or "static relationMappings" in content
        ):
            symbols.extend(_extract_objection(content, file_path))

    elif ext == ".go":
        # GORM
        if "gorm" in content:
            symbols.extend(_extract_gorm(content, file_path))

        # Ent ORM
        if (
            "ent.Schema" in content
            or "entgo.io/ent" in content
            or "[]ent.Field" in content
            or "[]ent.Edge" in content
        ):
            symbols.extend(_extract_ent(content, file_path))

    elif ext == ".py":
        # SQLAlchemy — run first; SQLModel imports sqlmodel not sqlalchemy
        if "sqlalchemy" in content.lower() and "sqlmodel" not in content.lower():
            symbols.extend(_extract_sqlalchemy(content, file_path))

        # SQLModel — must have SQLModel in class declaration
        if "SQLModel" in content and (
            "from sqlmodel" in content
            or "import sqlmodel" in content
        ):
            symbols.extend(_extract_sqlmodel(content, file_path))

        # Tortoise ORM
        if "tortoise" in content.lower() and (
            "tortoise.models" in content
            or "from tortoise" in content
        ):
            symbols.extend(_extract_tortoise(content, file_path))

    elif ext == ".cs":
        # EF Core
        if (
            "DbContext" in content
            or "DbSet<" in content
            or "[Table(" in content
            or "[Key]" in content
            or "modelBuilder.Entity" in content
        ):
            symbols.extend(_extract_efcore(content, file_path))

        # Dapper — can coexist with EF Core
        if (
            "connection.Query<" in content
            or "connection.QueryAsync<" in content
            or "connection.Execute(" in content
            or "SqlMapper" in content
        ):
            symbols.extend(_extract_dapper(content, file_path))

    elif ext in (".kt", ".kts"):
        # Exposed ORM — disambiguation from JPA: Exposed uses object extends *IdTable/Table
        if (
            "IntIdTable" in content
            or "LongIdTable" in content
            or "UUIDIdTable" in content
            or "IdTable" in content
            or "transaction {" in content
            or "org.jetbrains.exposed" in content
        ):
            symbols.extend(_extract_exposed(content, file_path))

    elif ext == ".rs":
        # Diesel ORM
        if (
            "extern crate diesel" in content
            or "use diesel::" in content
            or "diesel::table!" in content
            or "Queryable" in content
            or "Insertable" in content
        ):
            symbols.extend(_extract_diesel(content, file_path, ext))

    elif ext == ".java":
        # Spring Data JPA / Hibernate
        if (
            "import javax.persistence." in content
            or "import jakarta.persistence." in content
            or "import org.springframework.data.jpa." in content
            or "@Entity" in content
            or "@Repository" in content
        ):
            symbols.extend(_extract_spring_data_jpa(content, file_path, ext))

    elif ext == ".rb":
        # ActiveRecord (Rails)
        if "< ApplicationRecord" in content or "< ActiveRecord::Base" in content:
            symbols.extend(_extract_activerecord(content, file_path, ext))

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

        # Knex.js: .references('other_table')
        for m in _KNEX_REFERENCES_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "knex",
                }
            )

        # Objection.js: modelClass: OtherModel inside relationMappings
        for m in _OBJECTION_MODELCLASS_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "objection",
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

        # Tortoise ORM: ForeignKeyField('app.ModelName')
        for m in _TORTOISE_FK_RE.finditer(content):
            # 'app.ModelName' → extract model part after dot if present
            ref = m.group(1)
            target = ref.split(".")[-1] if "." in ref else ref
            edges.append(
                {
                    "source": file_id,
                    "target_name": target,
                    "edge_type": "references",
                    "orm": "tortoise",
                }
            )

        # SQLModel: foreign_key='table.column' → extract table part
        for m in _SQLMODEL_FK_TABLE_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "sqlmodel",
                }
            )

    elif ext == ".cs":
        # EF Core: HasOne/HasMany fluent API
        for m in _EFCORE_HASONE_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "efcore",
                }
            )
        for m in _EFCORE_HASMANY_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "efcore",
                }
            )
        # EF Core: [ForeignKey("PropertyName")]
        for m in _EFCORE_FK_ATTR_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "efcore",
                }
            )

    elif ext in (".kt", ".kts"):
        # Exposed: .references(OtherTable.col)
        for m in _EXPOSED_REFERENCES_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "references",
                    "orm": "exposed",
                }
            )

    elif ext == ".go":
        # Ent ORM: edge.To / edge.From / edge.Through
        for pattern, orm_label in (
            (_ENT_EDGE_TO_RE, "ent"),
            (_ENT_EDGE_FROM_RE, "ent"),
            (_ENT_EDGE_THROUGH_RE, "ent"),
        ):
            for m in pattern.finditer(content):
                edges.append(
                    {
                        "source": file_id,
                        "target_name": m.group(1),
                        "edge_type": "references",
                        "orm": orm_label,
                    }
                )

    elif ext == ".rs":
        # Diesel: #[belongs_to(ParentStruct)] → use edge
        for m in _DIESEL_BELONGS_TO_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "use",
                    "orm": "diesel",
                }
            )

    elif ext == ".java":
        # JPA: @ManyToOne field type → use edge
        for m in _JPA_MANY_TO_ONE_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "use",
                    "orm": "jpa",
                }
            )
        # JPA: @OneToMany / @ManyToMany collection element type
        for pattern in (_JPA_ONE_TO_MANY_RE, _JPA_MANY_TO_MANY_RE):
            for m in pattern.finditer(content):
                edges.append(
                    {
                        "source": file_id,
                        "target_name": m.group(1),
                        "edge_type": "use",
                        "orm": "jpa",
                    }
                )
        # JPA: @OneToOne field type
        for m in _JPA_ONE_TO_ONE_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": m.group(1),
                    "edge_type": "use",
                    "orm": "jpa",
                }
            )

    elif ext == ".rb":
        # ActiveRecord: belongs_to :foo → use edge to Foo
        for m in _AR_BELONGS_TO_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": _singularize_capitalize(m.group(1)),
                    "edge_type": "use",
                    "orm": "activerecord",
                }
            )
        # ActiveRecord: has_many :bars → use edge to Bar
        for m in _AR_HAS_MANY_RE.finditer(content):
            edges.append(
                {
                    "source": file_id,
                    "target_name": _singularize_capitalize(m.group(1)),
                    "edge_type": "use",
                    "orm": "activerecord",
                }
            )

    return edges


# ---------------------------------------------------------------------------
# Phase-26 ORM tests: Diesel, Spring Data JPA, ActiveRecord
# ---------------------------------------------------------------------------


def _test_phase26_orm() -> None:
    # ── Diesel (Rust) ─────────────────────────────────────────────────────────
    rs_code = '''
use diesel::prelude::*;
table! {
    users (id) {
        id -> Integer,
        name -> Text,
    }
}
#[derive(Queryable)]
pub struct User {
    pub id: i32,
    pub name: String,
}
'''
    assert supports_orm(rs_code, "models.rs", ".rs"), "Diesel not detected"
    syms = extract_orm_symbols(rs_code, "models.rs", ".rs")
    names = [s["name"] for s in syms]
    assert "users" in names or "User" in names, f"Diesel symbols: {names}"
    # Check table! macro produces a model
    assert any(s["symbol_type"] == "model" for s in syms), f"Diesel model symbol missing: {syms}"

    # belongs_to edge from parse_orm_imports
    rs_with_belongs = '''
use diesel::prelude::*;
#[belongs_to(User)]
#[derive(Queryable, Identifiable, Associations)]
pub struct Post {
    pub id: i32,
    pub user_id: i32,
}
'''
    edges = parse_orm_imports(rs_with_belongs, "models.rs", ".rs")
    target_names = [e["target_name"] for e in edges]
    assert "User" in target_names, f"Diesel belongs_to edge missing: {edges}"

    # ── Spring Data JPA (Java) ────────────────────────────────────────────────
    java_code = '''
import javax.persistence.*;
@Entity
@Table(name = "products")
public class Product {
    @Id
    @GeneratedValue
    private Long id;

    @ManyToOne
    private Category category;
}
'''
    assert supports_orm(java_code, "Product.java", ".java"), "JPA not detected"
    syms2 = extract_orm_symbols(java_code, "Product.java", ".java")
    names2 = [s["name"] for s in syms2]
    assert "Product" in names2, f"JPA symbols: {names2}"
    # @ManyToOne → hook symbol
    assert any(s["symbol_type"] == "hook" for s in syms2), f"JPA hook missing: {syms2}"
    # table_name extra field populated
    product_sym = next(s for s in syms2 if s["name"] == "Product")
    assert product_sym.get("table_name") == "products", f"JPA table_name: {product_sym}"

    # @Repository
    java_repo = '''
import org.springframework.data.jpa.repository.JpaRepository;
public interface ProductRepository extends JpaRepository<Product, Long> {}
'''
    syms_repo = extract_orm_symbols(java_repo, "ProductRepository.java", ".java")
    repo_names = [s["name"] for s in syms_repo]
    assert "ProductRepository" in repo_names, f"JPA Repository: {repo_names}"
    assert any(s["symbol_type"] == "use_case" for s in syms_repo), f"JPA use_case: {syms_repo}"

    # ── ActiveRecord (Ruby) ───────────────────────────────────────────────────
    rb_code = '''
class Order < ApplicationRecord
  belongs_to :user
  has_many :line_items
  scope :recent, -> { order(created_at: :desc).limit(10) }
end
'''
    assert supports_orm(rb_code, "order.rb", ".rb"), "ActiveRecord not detected"
    syms3 = extract_orm_symbols(rb_code, "order.rb", ".rb")
    names3 = [s["name"] for s in syms3]
    assert "Order" in names3, f"ActiveRecord symbols: {names3}"
    # belongs_to, has_many → hook symbols
    assert any(s["symbol_type"] == "hook" for s in syms3), f"AR hook missing: {syms3}"
    # scope → use_case
    assert any(s["symbol_type"] == "use_case" for s in syms3), f"AR scope missing: {syms3}"

    # AR edges
    ar_edges = parse_orm_imports(rb_code, "order.rb", ".rb")
    ar_targets = [e["target_name"] for e in ar_edges]
    assert "User" in ar_targets, f"AR belongs_to edge: {ar_targets}"
    assert "LineItem" in ar_targets, f"AR has_many edge: {ar_targets}"

    print("PASS  _test_phase26_orm (Diesel + JPA + ActiveRecord)")


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

    # ── Test 7: EF Core ───────────────────────────────────────────────────────
    cs_efcore = """
using Microsoft.EntityFrameworkCore;
public class AppDbContext : DbContext {
    public DbSet<User> Users { get; set; }
    public DbSet<Order> Orders { get; set; }
}
public class User { [Key] public int Id { get; set; } }
"""
    syms = extract_orm_symbols(cs_efcore, "Data/AppDbContext.cs", ".cs")
    assert any(
        s["name"] == "AppDbContext" and "dbcontext" in s.get("keywords", [])
        for s in syms
    ), f"EF Core DbContext: {syms}"
    assert any(s["name"] == "User" for s in syms), f"EF Core entity: {syms}"
    print("PASS  EF Core")

    # ── Test 8: Dapper ────────────────────────────────────────────────────────
    cs_dapper = """
using Dapper;
public class UserRepository {
    public IEnumerable<User> GetAll(IDbConnection connection) {
        return connection.Query<User>("SELECT * FROM users");
    }
    public void Create(IDbConnection connection) {
        connection.Execute("INSERT INTO users (name) VALUES (@Name)", new { Name = "test" });
    }
}
"""
    syms = extract_orm_symbols(cs_dapper, "Repos/UserRepository.cs", ".cs")
    assert any(s["name"] == "User" and s["symbol_type"] == "model" for s in syms), f"Dapper model: {syms}"
    print("PASS  Dapper")

    # ── Test 9: Ent ORM ───────────────────────────────────────────────────────
    go_ent = """
package schema
import "entgo.io/ent"
type User struct { ent.Schema }
func (User) Fields() []ent.Field { return []ent.Field{} }
func (User) Edges() []ent.Edge { return []ent.Edge{} }
"""
    syms = extract_orm_symbols(go_ent, "ent/schema/user.go", ".go")
    assert any(s["name"] == "User" for s in syms), f"Ent schema: {syms}"
    print("PASS  Ent ORM")

    # ── Test 10: Tortoise ORM ─────────────────────────────────────────────────
    py_tortoise = """
from tortoise import fields
from tortoise.models import Model

class Tournament(Model):
    id = fields.IntField(pk=True)
    name = fields.TextField()

    class Meta:
        table = "tournament"

class Event(Model):
    tournament = fields.ForeignKeyField('models.Tournament', related_name='events')
"""
    syms = extract_orm_symbols(py_tortoise, "models/tournament.py", ".py")
    assert any(s["name"] == "Tournament" for s in syms), f"Tortoise model: {syms}"
    assert any(s["name"] == "Event" for s in syms), f"Tortoise Event model: {syms}"
    print("PASS  Tortoise ORM")

    # ── Test 11: SQLModel ─────────────────────────────────────────────────────
    py_sqlmodel = """
from sqlmodel import SQLModel, Field, Relationship
from typing import Optional

class Hero(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    team_id: Optional[int] = Field(default=None, foreign_key="team.id")
    team: Optional["Team"] = Relationship(back_populates="heroes")

class HeroCreate(SQLModel):
    name: str
"""
    syms = extract_orm_symbols(py_sqlmodel, "app/models.py", ".py")
    assert any(
        s["name"] == "Hero" and s["symbol_type"] == "model" for s in syms
    ), f"SQLModel table: {syms}"
    assert any(
        s["name"] == "HeroCreate" and s["symbol_type"] == "utility" for s in syms
    ), f"SQLModel data: {syms}"
    print("PASS  SQLModel")

    # ── Test 12: Exposed (Kotlin) ─────────────────────────────────────────────
    kt_exposed = """
object Users : IntIdTable() {
    val name = varchar("name", 50)
    val email = varchar("email", 100).uniqueIndex()
}
object Orders : Table() {
    val userId = integer("user_id").references(Users.id)
}
"""
    syms = extract_orm_symbols(kt_exposed, "db/Tables.kt", ".kt")
    assert any(s["name"] == "Users" for s in syms), f"Exposed table: {syms}"
    assert any(s["name"] == "Orders" for s in syms), f"Exposed Orders: {syms}"
    print("PASS  Exposed ORM")

    # ── Test 13: Knex.js ──────────────────────────────────────────────────────
    js_knex = """
const knex = require('knex')({ client: 'pg' })
exports.up = function(knex) {
    return knex.schema.createTable('users', function(table) {
        table.increments('id')
        table.string('name')
    })
}
exports.down = function(knex) {
    return knex.schema.dropTable('users')
}
"""
    syms = extract_orm_symbols(js_knex, "migrations/001_users.js", ".js")
    assert any(s["name"] == "users" for s in syms), f"Knex createTable: {syms}"
    print("PASS  Knex.js")

    # ── Test 14: Kysely ───────────────────────────────────────────────────────
    ts_kysely = """
import { Kysely, PostgresDialect } from 'kysely'
interface Database { users: UserTable; orders: OrderTable }
interface UserTable { id: number; name: string }
interface OrderTable { id: number; userId: number }
const db = new Kysely<Database>({ dialect: new PostgresDialect({ pool }) })
db.selectFrom('users').select(['id', 'name'])
"""
    syms = extract_orm_symbols(ts_kysely, "src/db.ts", ".ts")
    assert any(
        s["name"] == "Database" or "UserTable" in s["name"] for s in syms
    ), f"Kysely: {syms}"
    print("PASS  Kysely")

    # ── Test 15: Objection.js ─────────────────────────────────────────────────
    js_objection = """
const { Model } = require('objection')
class User extends Model {
    static tableName = 'users'
    static get relationMappings() {
        return {
            orders: {
                relation: Model.HasManyRelation,
                modelClass: Order,
                join: { from: 'users.id', to: 'orders.user_id' }
            }
        }
    }
}
"""
    syms = extract_orm_symbols(js_objection, "models/User.js", ".js")
    assert any(s["name"] == "User" for s in syms), f"Objection model: {syms}"
    print("PASS  Objection.js")

    print("\nALL ORM TESTS PASSED")
