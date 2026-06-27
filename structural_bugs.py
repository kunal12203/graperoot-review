#!/usr/bin/env python3
"""structural_bugs.py — Phase 6 of GrapeRoot Pro.

Detects structural bugs across a codebase by analyzing info_graph.json and
source files.  All public functions return lists/dicts that are safe to
serialize to JSON and return as MCP tool results.

Checks implemented
------------------
1.  Bean / Service name collisions          (Java Spring + Python Flask/FastAPI)
2.  Missing config siblings                 (Kafka, TLS, DB, JWT, AWS)
3.  Duplicate config keys                   (.env, yaml, properties, Python)
4.  Kafka consumer-group collisions         (Python, Java, JS/TS, Go)
5.  Unhandled error paths                   (Python async, Go, JS/TS, Java)
6.  Import cycles (enhanced)                (symbols + fix hint)
7.  Dead event handlers                     (Spring + JS EventEmitter patterns)
8.  Missing pagination                      (SQLAlchemy, Django ORM, TypeORM, Mongoose, GORM, raw SQL)
9.  Missing indexes on FK fields            (Prisma, SQLAlchemy, Django, TypeORM, ActiveRecord)
10. N+1 query risk                          (Python, TypeScript, Ruby)
11. Unused env vars                         (vars declared in .env but never referenced)
12. Missing env vars                        (vars referenced in code but absent from all .env files)
13. Port conflicts                          (same port bound in multiple files/services)
14. Race condition risk                     (goroutines/threads/Promise.all with shared mutable state)

Aggregator
----------
run_all_checks(graph, project_root)  -> combined report dict
get_health_summary(graph, project_root) -> score / grade / top issues
"""
from __future__ import annotations

import re
import os
import json
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CODE_EXTS: set[str] = {
    ".py", ".java", ".kt", ".scala",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go",
    ".rb", ".php", ".cs", ".swift", ".rs",
}

_CONFIG_EXTS: set[str] = {
    ".env", ".properties", ".yaml", ".yml", ".toml", ".ini", ".cfg",
}

_TEST_PAT = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs?)/|"
    r"(?:\.test\.|\.spec\.|_test\.|test_|spec_)",
    re.IGNORECASE,
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# ---------------------------------------------------------------------------
# Constants for new checks (11-14)
# ---------------------------------------------------------------------------

# Directories to skip when walking project files
_SKIP_DIRS_COMMON = {
    ".git", ".svn", ".hg", "node_modules", "vendor",
    "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", ".idea", ".vscode",
    "site-packages", "eggs", ".eggs", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".next", ".nuxt",
}

# .env file name patterns
_ENV_FILE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".env.test", ".env.example", ".env.staging",
}

# Source file extensions for env-var scan
_SRC_EXTS_ENV = {".ts", ".js", ".py", ".go", ".java", ".rb", ".cs", ".kt", ".php",
                 ".tsx", ".jsx", ".mjs", ".cjs"}

# Variables that are framework/OS-injected — never flag as unused/missing
_AUTO_VARS: set[str] = {
    "PATH", "HOME", "USER", "SHELL",
    "NODE_ENV", "RAILS_ENV", "APP_ENV", "FLASK_ENV", "GIN_MODE",
    "DEBUG", "PORT", "HOST", "LOG_LEVEL",
}

# Missing-env exclusions (framework-set at runtime)
_MISSING_ENV_SKIP: set[str] = {
    "NODE_ENV", "PORT", "HOST", "DEBUG", "RAILS_ENV",
    "APP_ENV", "FLASK_ENV", "LOG_LEVEL", "GIN_MODE",
}

# Parse declared var from .env line  (e.g. "DB_PASSWORD=secret")
_ENV_DECL_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=(.*)$")

# Usage patterns per language — compiled once
_USAGE_PATTERNS: list[re.Pattern] = [
    # Node/JS
    re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)"),
    re.compile(r"""process\.env\[['"]([A-Z_][A-Z0-9_]*)['"]"""),
    # Python
    re.compile(r"""os\.environ\[['"]([A-Z_][A-Z0-9_]*)['"]"""),
    re.compile(r"""os\.(?:environ\.get|getenv)\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
    re.compile(r"""env\.get\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
    # Go
    re.compile(r"""os\.Getenv\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
    re.compile(r"""os\.LookupEnv\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
    # Java
    re.compile(r"""System\.getenv\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
    # Ruby
    re.compile(r"""ENV\s*\[['"]([A-Z_][A-Z0-9_]*)['"]"""),
    re.compile(r"""ENV\.fetch\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
    # C#
    re.compile(r"""Environment\.GetEnvironmentVariable\s*\(\s*['"]([A-Z_][A-Z0-9_]*)['"]"""),
]

# Port detection patterns
# .env / config: PORT=8080  or  REDIS_PORT=6379
_PORT_ENV_RE   = re.compile(r"^([A-Z_]*PORT[A-Z_]*)\s*=\s*(\d{2,5})", re.MULTILINE)
# docker-compose ports: section "- 8080:8080"
_DC_PORT_RE    = re.compile(r"""['"]?(\d{2,5}):(\d{2,5})['"]?""")
# docker-compose environment PORT: 8080
_DC_ENV_PORT_RE = re.compile(r"PORT\s*:\s*(\d{2,5})")
# K8s containerPort / port / nodePort
_K8S_PORT_RE   = re.compile(r"(?:containerPort|nodePort|port)\s*:\s*(\d{2,5})")
# Python uvicorn / Flask
_PY_PORT_RE    = re.compile(
    r"(?:uvicorn\.run|app\.run)\s*\([^)]*?port\s*=\s*(\d{2,5})"
    r"|\.listen\s*\(\s*(\d{2,5})",
    re.DOTALL,
)
# Go ListenAndServe / net.Listen
_GO_PORT_RE    = re.compile(
    r'ListenAndServe\s*\(\s*["\'][^"\']*:(\d{2,5})["\']'
    r'|net\.Listen\s*\([^,]+,\s*["\'][^"\']*:(\d{2,5})["\']'
)
# Node.js .listen(port) or PORT || 3000
_JS_PORT_RE    = re.compile(
    r'\.listen\s*\(\s*(\d{2,5})'
    r'|PORT\s*\|\|\s*(\d{2,5})'
)
# Spring Boot application.properties
_SPRING_PORT_RE = re.compile(r"server\.port\s*=\s*(\d{2,5})")
# Ports to ignore (expected to repeat in proxy configs)
_SKIP_PORTS    = {80, 443, 0}

# Race condition patterns — Go
_GO_GOROUTINE_RE  = re.compile(r"\bgo\s+(?:func\s*\(|\w+\s*\()")
_GO_SYNC_RE       = re.compile(r"""["']sync["']|sync\.""")
_GO_MAP_WRITE_RE  = re.compile(r"\w+\[[\w\"']+\]\s*=")
_GO_SHARED_MUT_RE = re.compile(
    r"\bgo\s+func\s*\([^)]*\)\s*\{[^}]*?(\w+)\s*(?:\+\+|--|\+=|-=|=\s*[^=])",
    re.DOTALL,
)

# Race condition patterns — Python threading
_PY_THREAD_RE     = re.compile(r"threading\.Thread\s*\(")
_PY_LOCK_RE       = re.compile(r"threading\.(?:R?Lock)\s*\(\s*\)")

# Race condition patterns — JS/TS Promise.all
_JS_PROMISE_ALL_RE = re.compile(r"Promise\.all\s*\(")
_JS_SHARED_AGG_RE  = re.compile(
    r"\blet\s+(count|total|results|errors|data)\s*=\s*(?:0|\[\]|\{\})"
)
_JS_MUTATION_RE    = re.compile(
    r"\+\+\s*(?:count|total)|(?:results|errors|data)\.push\s*\("
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_source_file(file_path: str, project_root: str) -> str | None:
    """Read a source file relative to project_root.  Returns None on failure."""
    try:
        p = Path(project_root) / file_path
        if not p.exists():
            # Maybe file_path is already absolute
            p = Path(file_path)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _iter_source_files(
    project_root: str,
    exts: set[str],
    *,
    skip_tests: bool = True,
    max_files: int = 5000,
) -> Iterator[tuple[Path, str]]:
    """Yield (path, content) for every source file with a matching extension.

    Skips hidden dirs, node_modules, venv, .git, __pycache__, dist, build, etc.
    """
    _SKIP_DIRS = {
        ".git", ".svn", ".hg", "node_modules", "vendor",
        "__pycache__", ".venv", "venv", "env", ".env",
        "dist", "build", "out", "target", ".idea", ".vscode",
        "site-packages", "eggs", ".eggs", ".tox", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".next", ".nuxt",
    }
    root = Path(project_root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in-place so os.walk doesn't recurse into skipped dirs
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if count >= max_files:
                return
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() not in exts:
                continue
            rel = str(fpath.relative_to(root))
            if skip_tests and _TEST_PAT.search(rel):
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            count += 1
            yield fpath, content


def _normalize_bean_name(class_name: str) -> str:
    """Spring default bean name = first char lowercased."""
    if not class_name:
        return class_name
    return class_name[0].lower() + class_name[1:]


def _rel(path: Path, project_root: str) -> str:
    """Return path relative to project_root as a POSIX string."""
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _sort_issues(issues: list[dict]) -> list[dict]:
    return sorted(issues, key=lambda x: _SEVERITY_ORDER.get(x.get("severity", "low"), 3))


# ---------------------------------------------------------------------------
# 1. Bean / Service name collisions
# ---------------------------------------------------------------------------

# Spring annotations that register a bean
_SPRING_ANNO_RE = re.compile(
    r"@(?:Service|Component|Controller|RestController|Repository|Bean)"
    r'(?:\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']\s*\))?',
)
_JAVA_CLASS_RE = re.compile(r"(?:public\s+)?(?:class|interface|enum)\s+(\w+)")

# Flask/FastAPI route function duplicates
_FLASK_ROUTE_RE = re.compile(
    r'@(?:\w+\.)?(?:route|get|post|put|delete|patch|head|options)\s*\([^)]*\)\s*\n'
    r'\s*(?:async\s+)?def\s+(\w+)',
    re.MULTILINE,
)
_FASTAPI_ROUTE_RE = _FLASK_ROUTE_RE  # same pattern


def find_bean_collisions(graph: dict) -> list[dict]:  # noqa: ARG001
    """Detect duplicate Spring bean names and duplicate Flask/FastAPI endpoint names.

    Parameters
    ----------
    graph:
        Loaded info_graph.json dict (caller is responsible for loading it).

    Returns
    -------
    list[dict] with type="bean_collision".
    """
    # ------------------------------------------------------------------
    # Build file list from graph
    # ------------------------------------------------------------------
    file_ids: list[str] = []
    root_hint: str = graph.get("root", "")
    for node in graph.get("nodes", []):
        if node.get("kind") == "file":
            ext = node.get("ext", "").lower()
            if ext in {".java", ".kt", ".py"}:
                file_ids.append(node.get("id", ""))

    # bean_name -> list of (file, line)
    bean_registry: dict[str, list[dict]] = defaultdict(list)
    # endpoint_function_name -> list of (file, line)
    endpoint_registry: dict[str, list[dict]] = defaultdict(list)

    for fid in file_ids:
        content = _load_source_file(fid, root_hint)
        if content is None:
            continue
        ext = Path(fid).suffix.lower()
        lines = content.splitlines()

        if ext in {".java", ".kt"}:
            # Find @Service/@Component etc. and the following class name
            for i, line in enumerate(lines, 1):
                m = _SPRING_ANNO_RE.search(line)
                if not m:
                    continue
                explicit_name = m.group(1)  # may be None
                if explicit_name:
                    bean_registry[explicit_name].append({"file": fid, "line": i})
                else:
                    # Search forward for the class name (within next 5 lines)
                    for j in range(i, min(i + 6, len(lines) + 1)):
                        cm = _JAVA_CLASS_RE.search(lines[j - 1])
                        if cm:
                            bean_name = _normalize_bean_name(cm.group(1))
                            bean_registry[bean_name].append({"file": fid, "line": j})
                            break

        elif ext == ".py":
            # Flask / FastAPI endpoint names
            for m in _FLASK_ROUTE_RE.finditer(content):
                fn_name = m.group(1)
                # Find the line number
                line_no = content[: m.start()].count("\n") + 1
                endpoint_registry[fn_name].append({"file": fid, "line": line_no})

    issues: list[dict] = []

    for bean_name, locations in bean_registry.items():
        # Deduplicate by file — same class can appear multiple times in search
        seen_files: dict[str, int] = {}
        for loc in locations:
            if loc["file"] not in seen_files:
                seen_files[loc["file"]] = loc["line"]
        if len(seen_files) < 2:
            continue
        files = list(seen_files.keys())
        lns = list(seen_files.values())
        issues.append(
            {
                "type": "bean_collision",
                "severity": "high",
                "bean_name": bean_name,
                "files": files,
                "lines": lns,
                "description": (
                    f"Spring bean '{bean_name}' is defined in {len(files)} files. "
                    "One definition silently wins at runtime; the loser is ignored."
                ),
                "fix": (
                    f"Give each bean a unique explicit name via @Service(\"{bean_name}Impl\") "
                    "or ensure only one class carries this bean name."
                ),
            }
        )

    for fn_name, locations in endpoint_registry.items():
        seen_files: dict[str, int] = {}
        for loc in locations:
            if loc["file"] not in seen_files:
                seen_files[loc["file"]] = loc["line"]
        if len(seen_files) < 2:
            continue
        files = list(seen_files.keys())
        lns = list(seen_files.values())
        issues.append(
            {
                "type": "bean_collision",
                "severity": "high",
                "bean_name": fn_name,
                "files": files,
                "lines": lns,
                "description": (
                    f"Flask/FastAPI endpoint function '{fn_name}' is registered in "
                    f"{len(files)} files, which causes an AssertionError at startup "
                    "('View function mapping is overwriting an existing endpoint function')."
                ),
                "fix": (
                    f"Rename one of the '{fn_name}' view functions or use "
                    "Blueprint.endpoint= / APIRouter prefix to scope them separately."
                ),
            }
        )

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 2. Missing config siblings
# ---------------------------------------------------------------------------

# Patterns to extract key=value or key: value from config files / source constants
_KV_ENV_RE    = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*(.+)$", re.MULTILINE)
_KV_YAML_RE   = re.compile(r"^\s{0,8}([a-zA-Z][a-zA-Z0-9._\-]{1,})\s*:\s*(.+)$", re.MULTILINE)
_KV_PY_CONST  = re.compile(r"""^([A-Z][A-Z0-9_]{2,})\s*=\s*["']?([^"'\n]+)["']?""", re.MULTILINE)
_KV_PROPS_RE  = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9._\-]+)\s*=\s*(.+)$", re.MULTILINE)

# Normalise a key so both KAFKA_BOOTSTRAP_SERVERS and bootstrap.servers match
def _norm_key(k: str) -> str:
    return k.upper().replace(".", "_").replace("-", "_")

# Sibling rules: (required_key, sibling_key, severity, message_fragment)
_SIBLING_RULES: list[tuple[str, str, str, str]] = [
    # Kafka producer
    ("KEY_SERIALIZER",           "VALUE_SERIALIZER",           "high",   "Kafka serializer pair"),
    ("VALUE_SERIALIZER",         "KEY_SERIALIZER",             "high",   "Kafka serializer pair"),
    # Kafka consumer
    ("KEY_DESERIALIZER",         "VALUE_DESERIALIZER",         "high",   "Kafka deserializer pair"),
    ("VALUE_DESERIALIZER",       "KEY_DESERIALIZER",           "high",   "Kafka deserializer pair"),
    ("BOOTSTRAP_SERVERS",        "GROUP_ID",                   "medium", "Kafka consumer group"),
    # Kafka SASL
    ("SASL_MECHANISM",           "SASL_USERNAME",              "high",   "Kafka SASL credentials"),
    ("SASL_MECHANISM",           "SASL_PASSWORD",              "high",   "Kafka SASL credentials"),
    ("SASL_USERNAME",            "SASL_PASSWORD",              "high",   "Kafka SASL credentials"),
    # TLS/SSL
    ("SSL_CERT_FILE",            "SSL_KEY_FILE",               "high",   "TLS cert/key pair"),
    ("SSL_KEY_FILE",             "SSL_CERT_FILE",              "high",   "TLS cert/key pair"),
    ("SSL_CERT",                 "SSL_KEY",                    "high",   "TLS cert/key pair"),
    ("SSL_KEY",                  "SSL_CERT",                   "high",   "TLS cert/key pair"),
    ("TLS_CERT_PATH",            "TLS_KEY_PATH",               "high",   "TLS cert/key pair"),
    ("TLS_KEY_PATH",             "TLS_CERT_PATH",              "high",   "TLS cert/key pair"),
    # DB connection
    ("DB_HOST",                  "DB_PORT",                    "low",    "DB connection pair"),
    ("DB_USER",                  "DB_PASSWORD",                "high",   "DB credentials pair"),
    ("DB_PASSWORD",              "DB_USER",                    "high",   "DB credentials pair"),
    # JWT / Auth
    ("JWT_SECRET",               "JWT_ALGORITHM",              "medium", "JWT config pair"),
    ("ACCESS_TOKEN_SECRET",      "REFRESH_TOKEN_SECRET",       "low",    "token secret pair"),
    # AWS
    ("AWS_ACCESS_KEY_ID",        "AWS_SECRET_ACCESS_KEY",      "high",   "AWS credentials pair"),
    ("AWS_SECRET_ACCESS_KEY",    "AWS_ACCESS_KEY_ID",          "high",   "AWS credentials pair"),
]

# AWS services that imply AWS_REGION should be set
_AWS_SERVICE_RE = re.compile(
    r"(?:boto3\.|botocore\.|@aws-sdk/|aws-sdk|new\s+AWS\.|AmazonS3|AmazonDynamoDB"
    r"|AmazonSNS|AmazonSQS|AWSLambda|CloudWatch|SecretsManager)",
    re.IGNORECASE,
)

# Conflict: database_url alongside individual params
_DB_URL_RE = re.compile(
    r"\b(?:DATABASE_URL|DB_URL|SQLALCHEMY_DATABASE_URI)\b", re.IGNORECASE
)
_DB_PARTS_RE = re.compile(
    r"\b(?:DB_HOST|DB_PORT|DB_NAME|DB_USER|DATABASE_HOST)\b", re.IGNORECASE
)


def _collect_keys_from_file(content: str, path: Path) -> set[str]:
    """Return the set of normalised config keys found in a file."""
    keys: set[str] = set()
    ext = path.suffix.lower()
    if ext in {".env", ""}:
        for m in _KV_ENV_RE.finditer(content):
            keys.add(_norm_key(m.group(1)))
    if ext in {".yaml", ".yml"}:
        for m in _KV_YAML_RE.finditer(content):
            keys.add(_norm_key(m.group(1)))
    if ext in {".properties"}:
        for m in _KV_PROPS_RE.finditer(content):
            keys.add(_norm_key(m.group(1)))
    if ext == ".py":
        for m in _KV_PY_CONST.finditer(content):
            keys.add(_norm_key(m.group(1)))
    # Always try bare env-style for any file
    if ext not in {".yaml", ".yml", ".properties", ".py"}:
        for m in _KV_ENV_RE.finditer(content):
            keys.add(_norm_key(m.group(1)))
    return keys


def find_missing_config_siblings(graph: dict, project_root: str) -> list[dict]:
    """Detect config keys that require a sibling key which is absent.

    Scans .env files, docker-compose, K8s manifests, and Python constants.

    Parameters
    ----------
    graph:
        Loaded info_graph.json dict.
    project_root:
        Absolute path to the project root on disk.

    Returns
    -------
    list[dict] with type="missing_config_sibling".
    """
    issues: list[dict] = []

    # Gather all config files from filesystem
    _CFG_GLOBS = ["**/.env", "**/*.env", "**/docker-compose*.yml",
                  "**/docker-compose*.yaml", "**/*.properties",
                  "**/application.yml", "**/application.yaml",
                  "**/settings.py", "**/config.py", "**/constants.py",
                  "**/k8s/*.yaml", "**/kubernetes/*.yaml",
                  "**/helm/**/*.yaml", "**/manifests/**/*.yaml"]

    root = Path(project_root)

    # file_path -> set of normalised keys
    file_keys: dict[str, set[str]] = {}

    # Collect via graph nodes first
    for node in graph.get("nodes", []):
        if node.get("kind") != "file":
            continue
        fid = node.get("id", "")
        ext = Path(fid).suffix.lower()
        basename = Path(fid).name.lower()
        if ext not in _CONFIG_EXTS and basename not in {"settings.py", "config.py", "constants.py"}:
            continue
        content = _load_source_file(fid, project_root)
        if content is None:
            continue
        file_keys[fid] = _collect_keys_from_file(content, Path(fid))

    # Also walk filesystem for .env files not in graph
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {".git", "node_modules", "__pycache__", ".venv", "venv",
                         "dist", "build", "out", "target"}
            and not d.startswith(".")
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = str(fpath.relative_to(root))
            if rel in file_keys:
                continue
            bname = fname.lower()
            ext = fpath.suffix.lower()
            if (ext in _CONFIG_EXTS or bname.startswith(".env") or
                    bname in {"settings.py", "config.py", "constants.py"}):
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                file_keys[rel] = _collect_keys_from_file(content, fpath)

    # Build aggregate key set across all files
    all_keys: set[str] = set()
    for ks in file_keys.values():
        all_keys.update(ks)

    # Per-file check: if a file defines KEY but not its sibling
    already_reported: set[tuple[str, str]] = set()

    for fid, keys in file_keys.items():
        for present_key, required_sibling, severity, label in _SIBLING_RULES:
            norm_present = _norm_key(present_key)
            norm_sibling = _norm_key(required_sibling)
            if norm_present not in keys:
                continue
            # Missing in THIS file AND missing project-wide
            if norm_sibling in all_keys:
                continue
            pair_key = (norm_present, norm_sibling)
            if pair_key in already_reported:
                continue
            already_reported.add(pair_key)
            issues.append(
                {
                    "type": "missing_config_sibling",
                    "severity": severity,
                    "key_present": present_key,
                    "key_missing": required_sibling,
                    "files": [fid],
                    "lines": [],
                    "description": (
                        f"{label}: '{present_key}' is set but '{required_sibling}' "
                        "is not found anywhere in the project."
                    ),
                    "fix": f"Add '{required_sibling}' to the same config file or environment.",
                }
            )

    # AWS_REGION check: scan source files for AWS SDK usage
    aws_region_set = "AWS_REGION" in all_keys or "AWS_DEFAULT_REGION" in all_keys
    if not aws_region_set:
        for _, content in _iter_source_files(project_root, _CODE_EXTS, skip_tests=True, max_files=2000):
            if _AWS_SERVICE_RE.search(content):
                issues.append(
                    {
                        "type": "missing_config_sibling",
                        "severity": "medium",
                        "key_present": "AWS_SDK_USAGE",
                        "key_missing": "AWS_REGION",
                        "files": [],
                        "lines": [],
                        "description": (
                            "AWS SDK is used in source code but AWS_REGION / "
                            "AWS_DEFAULT_REGION is not set in any config file."
                        ),
                        "fix": "Set AWS_REGION in .env or environment before deploying.",
                    }
                )
                break

    # Conflict: DATABASE_URL + individual host/port/user
    db_url_files: list[str] = []
    db_parts_files: list[str] = []
    for fid, keys in file_keys.items():
        content_raw = _load_source_file(fid, project_root) or ""
        if _DB_URL_RE.search(content_raw):
            db_url_files.append(fid)
        if _DB_PARTS_RE.search(content_raw):
            db_parts_files.append(fid)
    if db_url_files and db_parts_files:
        issues.append(
            {
                "type": "missing_config_sibling",
                "severity": "medium",
                "key_present": "DATABASE_URL",
                "key_missing": "CONFLICT",
                "files": db_url_files[:3] + db_parts_files[:3],
                "lines": [],
                "description": (
                    "DATABASE_URL is set alongside individual DB_HOST/DB_USER/DB_PORT "
                    "parameters. One of the two styles should be removed to avoid confusion."
                ),
                "fix": (
                    "Choose one approach: either use DATABASE_URL exclusively, "
                    "or use individual DB_HOST / DB_PORT / DB_USER / DB_PASSWORD keys."
                ),
            }
        )

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 3. Duplicate config keys
# ---------------------------------------------------------------------------

def find_duplicate_config_keys(project_root: str) -> list[dict]:
    """Detect the same config key defined more than once in the same file.

    Covers:
    - .env files (KEY=VALUE lines)
    - application.properties (key=value)
    - application.yml / config.yaml (yaml key:)
    - Python settings/constants files (CONSTANT = ...)

    Parameters
    ----------
    project_root:
        Absolute path to the project root.

    Returns
    -------
    list[dict] with type="duplicate_config_key".
    """
    issues: list[dict] = []
    root = Path(project_root)

    _SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", "out", "target",
    }

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = str(fpath.relative_to(root))
            ext = fpath.suffix.lower()
            bname = fname.lower()

            is_env    = bname.startswith(".env") or ext == ".env"
            is_props  = ext == ".properties" or bname in {"application.properties"}
            is_yaml   = ext in {".yaml", ".yml"}
            is_py     = ext == ".py" and bname in {
                "settings.py", "config.py", "constants.py",
                "configuration.py", "conf.py", "env.py",
            }

            if not (is_env or is_props or is_yaml or is_py):
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # key -> list of line numbers
            key_lines: dict[str, list[int]] = defaultdict(list)

            if is_env or is_props:
                pat = _KV_ENV_RE if is_env else _KV_PROPS_RE
                for m in pat.finditer(content):
                    key = _norm_key(m.group(1))
                    # find line number
                    ln = content[: m.start()].count("\n") + 1
                    key_lines[key].append(ln)

            elif is_yaml:
                # Track indent-0 or indent-2 YAML keys for flat configs
                for i, line in enumerate(content.splitlines(), 1):
                    m = _KV_YAML_RE.match(line)
                    if m:
                        key = _norm_key(m.group(1))
                        key_lines[key].append(i)

            elif is_py:
                for m in _KV_PY_CONST.finditer(content):
                    key = _norm_key(m.group(1))
                    ln = content[: m.start()].count("\n") + 1
                    key_lines[key].append(ln)

            for key, lns in key_lines.items():
                if len(lns) < 2:
                    continue
                issues.append(
                    {
                        "type": "duplicate_config_key",
                        "severity": "medium",
                        "key": key,
                        "files": [rel],
                        "lines": lns,
                        "description": (
                            f"Config key '{key}' is defined {len(lns)} times in '{rel}' "
                            f"(lines {', '.join(str(l) for l in lns)}). "
                            "The last definition silently wins in most loaders."
                        ),
                        "fix": (
                            f"Remove duplicate definitions of '{key}' and keep only "
                            "the authoritative one."
                        ),
                    }
                )

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 4. Kafka consumer group collisions
# ---------------------------------------------------------------------------

# Python: group_id='name' + subscribe / topics
_PY_GROUP_ID_RE    = re.compile(r"""group_id\s*=\s*['"]([^'"]+)['"]""")
_PY_TOPIC_RE       = re.compile(r"""(?:subscribe|topics?)\s*[=(]\s*\[?\s*['"]([^'"]+)['"]""")

# Java: @KafkaListener(topics="t", groupId="g")
_JAVA_KAFKA_RE     = re.compile(
    r'@KafkaListener\s*\([^)]*topics\s*=\s*"([^"]+)"[^)]*groupId\s*=\s*"([^"]+)"'
    r'|@KafkaListener\s*\([^)]*groupId\s*=\s*"([^"]+)"[^)]*topics\s*=\s*"([^"]+)"',
    re.DOTALL,
)

# JS/TS: groupId: 'name'  near  topics: ['name']
_JS_GROUP_RE       = re.compile(r"""groupId\s*:\s*['"`]([^'"`]+)['"`]""")
_JS_TOPIC_RE       = re.compile(r"""topics\s*:\s*\[['"`]([^'"`]+)['"`]""")

# Go: GroupID: "name"  near  Topic: "name"
_GO_GROUP_RE       = re.compile(r"""GroupID\s*:\s*"([^"]+)"|group_id\s*=\s*"([^"]+)" """)
_GO_TOPIC_RE       = re.compile(r"""(?:Topic|Topics)\s*:\s*"([^"]+)"|topic\s*=\s*"([^"]+)" """)


def find_kafka_group_collisions(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect same Kafka group.id consuming from the same topic in multiple files.

    Unintentional group sharing causes silent partition rebalancing / data loss.

    Parameters
    ----------
    graph:
        Loaded info_graph.json dict.
    project_root:
        Absolute path to the project root.

    Returns
    -------
    list[dict] with type="kafka_group_collision".
    """
    # (group_id, topic) -> list of (file, line)
    registry: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for fpath, content in _iter_source_files(
        project_root, {".py", ".java", ".ts", ".js", ".go"}, skip_tests=False, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        ext = fpath.suffix.lower()
        lines = content.splitlines()

        if ext == ".py":
            # Find group_id in the file, then look for nearby topic
            for gm in _PY_GROUP_ID_RE.finditer(content):
                gname = gm.group(1)
                # Search within 30 lines of the group_id occurrence
                gline = content[: gm.start()].count("\n")
                start = max(0, gline - 5)
                end   = min(len(lines), gline + 30)
                snippet = "\n".join(lines[start:end])
                for tm in _PY_TOPIC_RE.finditer(snippet):
                    topic = tm.group(1)
                    line_no = gline + 1
                    registry[(gname, topic)].append({"file": rel, "line": line_no})

        elif ext in {".java", ".kt"}:
            for m in _JAVA_KAFKA_RE.finditer(content):
                # groups: (topics1, groupId1) or (groupId2, topics2)
                if m.group(1):
                    topic, gname = m.group(1), m.group(2)
                else:
                    gname, topic = m.group(3), m.group(4)
                line_no = content[: m.start()].count("\n") + 1
                registry[(gname, topic)].append({"file": rel, "line": line_no})

        elif ext in {".ts", ".js", ".mjs", ".cjs"}:
            for gm in _JS_GROUP_RE.finditer(content):
                gname = gm.group(1)
                gline = content[: gm.start()].count("\n")
                start = max(0, gline - 10)
                end   = min(len(lines), gline + 20)
                snippet = "\n".join(lines[start:end])
                for tm in _JS_TOPIC_RE.finditer(snippet):
                    topic = tm.group(1)
                    registry[(gname, topic)].append({"file": rel, "line": gline + 1})

        elif ext == ".go":
            for gm in _GO_GROUP_RE.finditer(content):
                gname = gm.group(1) or gm.group(2)
                if not gname:
                    continue
                gline = content[: gm.start()].count("\n")
                start = max(0, gline - 10)
                end   = min(len(lines), gline + 20)
                snippet = "\n".join(lines[start:end])
                for tm in _GO_TOPIC_RE.finditer(snippet):
                    topic = tm.group(1) or tm.group(2)
                    if topic:
                        registry[(gname, topic)].append({"file": rel, "line": gline + 1})

    issues: list[dict] = []
    for (gname, topic), locations in registry.items():
        # Deduplicate by file
        seen: dict[str, int] = {}
        for loc in locations:
            if loc["file"] not in seen:
                seen[loc["file"]] = loc["line"]
        if len(seen) < 2:
            continue
        files = list(seen.keys())
        lns   = list(seen.values())
        issues.append(
            {
                "type": "kafka_group_collision",
                "severity": "high",
                "group_id": gname,
                "topic": topic,
                "files": files,
                "lines": lns,
                "description": (
                    f"Kafka group_id='{gname}' consumes topic '{topic}' from "
                    f"{len(files)} different files. If unintentional, partitions are "
                    "shared between consumers — messages are NOT duplicated but each "
                    "message goes to only one consumer, which may cause silent data loss."
                ),
                "fix": (
                    f"If these are distinct services, give each a unique group_id. "
                    f"If they are intentional replicas, add a comment to clarify."
                ),
            }
        )

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 5. Unhandled error paths
# ---------------------------------------------------------------------------

# Python async: function with await but no try/except
_PY_ASYNC_DEF_RE  = re.compile(r"^(\s*)async\s+def\s+\w+", re.MULTILINE)
_PY_AWAIT_RE      = re.compile(r"\bawait\b")
_PY_TRY_RE        = re.compile(r"\btry\s*:")

# Go: function call whose result is discarded
_GO_IGNORE_ERR_RE = re.compile(
    r"^\s*_\s*(?:,\s*_)?\s*=\s*\w[\w.]*\s*\(",  # _ = func() or _, _ = func()
    re.MULTILINE,
)
# Go function returning error signature
_GO_FUNC_ERR_RE   = re.compile(r"func\s+\w+\([^)]*\)\s*(?:\([^)]*\bError\b[^)]*\)|\berror\b)")

# JS/TS: .then() without .catch() and no surrounding try/catch
_JS_THEN_RE       = re.compile(r"\.then\s*\(")
_JS_CATCH_RE      = re.compile(r"\.catch\s*\(|\bawait\b|try\s*{")

# Java: method throws checked exception, caller doesn't declare/catch
_JAVA_THROWS_RE   = re.compile(r"throws\s+(?!RuntimeException|Error)\w+(?:,\s*\w+)*")
_JAVA_TRY_RE      = re.compile(r"\btry\s*\{")
_JAVA_CATCH_RE    = re.compile(r"\bcatch\s*\(")


def _extract_python_function_body(content: str, def_start: int, indent: str) -> str:
    """Return lines of a Python function starting at def_start."""
    lines = content[def_start:].splitlines()
    body_lines: list[str] = []
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "":
            body_lines.append(line)
            continue
        # Dedent detection: if line starts with same or less indent it's outside
        stripped = line.lstrip()
        line_indent = line[: len(line) - len(stripped)]
        if stripped and len(line_indent) <= len(indent):
            break
        body_lines.append(line)
    return "\n".join(body_lines)


def find_unhandled_errors(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect unhandled error paths in Python, Go, JS/TS and Java source files.

    Parameters
    ----------
    graph:
        Loaded info_graph.json dict.
    project_root:
        Absolute path to the project root.

    Returns
    -------
    list[dict] with type="unhandled_error".
    """
    issues: list[dict] = []

    for fpath, content in _iter_source_files(
        project_root, _CODE_EXTS, skip_tests=True, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        ext = fpath.suffix.lower()

        if ext == ".py":
            # Find async def, check body for await without try/except
            for m in _PY_ASYNC_DEF_RE.finditer(content):
                indent = m.group(1)
                body   = _extract_python_function_body(content, m.start(), indent)
                if _PY_AWAIT_RE.search(body) and not _PY_TRY_RE.search(body):
                    fn_name_m = re.search(r"async\s+def\s+(\w+)", m.group(0))
                    fn_name   = fn_name_m.group(1) if fn_name_m else "?"
                    line_no   = content[: m.start()].count("\n") + 1
                    issues.append(
                        {
                            "type": "unhandled_error",
                            "severity": "medium",
                            "language": "python",
                            "files": [rel],
                            "lines": [line_no],
                            "description": (
                                f"Python async function '{fn_name}' uses 'await' but has "
                                "no try/except block. Unhandled coroutine exceptions become "
                                "unraisable and are silently swallowed in some frameworks."
                            ),
                            "fix": (
                                f"Wrap the body of '{fn_name}' in try/except "
                                "(or use a global exception handler / middleware)."
                            ),
                        }
                    )
                    if len(issues) >= 100:
                        break

        elif ext == ".go":
            # Find lines that discard error returns
            for m in _GO_IGNORE_ERR_RE.finditer(content):
                line_no = content[: m.start()].count("\n") + 1
                code_snippet = m.group(0).strip()
                issues.append(
                    {
                        "type": "unhandled_error",
                        "severity": "high",
                        "language": "go",
                        "files": [rel],
                        "lines": [line_no],
                        "description": (
                            f"Go: error return explicitly discarded with '_' at line {line_no}: "
                            f"`{code_snippet[:80]}`. This suppresses all failure signals from "
                            "the callee."
                        ),
                        "fix": (
                            "Assign the error to a named variable and handle it, or document "
                            "in a comment why the error is intentionally ignored."
                        ),
                    }
                )
                if len(issues) >= 100:
                    break

        elif ext in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            lines_list = content.splitlines()
            for i, line in enumerate(lines_list, 1):
                if not _JS_THEN_RE.search(line):
                    continue
                # Look at surrounding 5 lines for .catch or try
                start = max(0, i - 3)
                end   = min(len(lines_list), i + 8)
                window = "\n".join(lines_list[start:end])
                if not _JS_CATCH_RE.search(window):
                    issues.append(
                        {
                            "type": "unhandled_error",
                            "severity": "medium",
                            "language": "javascript/typescript",
                            "files": [rel],
                            "lines": [i],
                            "description": (
                                f"Promise chain '.then()' at line {i} in '{rel}' "
                                "has no '.catch()' and is not inside a try/await block. "
                                "Unhandled promise rejections can crash Node.js processes."
                            ),
                            "fix": (
                                "Add a .catch(err => ...) handler, or convert to "
                                "async/await with try/catch."
                            ),
                        }
                    )
                if len(issues) >= 100:
                    break

        elif ext in {".java", ".kt"}:
            # Find methods that declare 'throws' but callers in same file don't catch
            if _JAVA_THROWS_RE.search(content):
                # Heuristic: if throws is declared but no try block exists in the file
                if not _JAVA_TRY_RE.search(content) and not _JAVA_CATCH_RE.search(content):
                    for m in _JAVA_THROWS_RE.finditer(content):
                        line_no = content[: m.start()].count("\n") + 1
                        issues.append(
                            {
                                "type": "unhandled_error",
                                "severity": "medium",
                                "language": "java",
                                "files": [rel],
                                "lines": [line_no],
                                "description": (
                                    f"Java method at line {line_no} declares a checked exception "
                                    f"({m.group(0).strip()}) but no try/catch block exists in "
                                    "this file to handle it."
                                ),
                                "fix": (
                                    "Either catch the checked exception with try/catch, or "
                                    "propagate it by declaring 'throws' on the calling method."
                                ),
                            }
                        )
                    if len(issues) >= 100:
                        break

    return _sort_issues(issues[:200])


# ---------------------------------------------------------------------------
# 6. Import cycles (enhanced)
# ---------------------------------------------------------------------------

def find_import_cycles_detailed(graph: dict) -> list[dict]:
    """Find circular import chains with symbol context and fix hints.

    Builds a directed import graph from graph edges, runs DFS cycle detection,
    then annotates each cycle with the symbols crossing the cycle boundary.

    Parameters
    ----------
    graph:
        Loaded info_graph.json dict.

    Returns
    -------
    list[dict] with type="import_cycle".
    """
    edges = graph.get("edges", [])

    # Build: file -> {imported_file} adjacency (deduplicated)
    adj: dict[str, set[str]] = defaultdict(set)
    # Also track: (from_file, to_file) -> list of symbol names
    edge_symbols: dict[tuple[str, str], list[str]] = defaultdict(list)

    for e in edges:
        rel = e.get("rel", "")
        if rel not in {"imports", "references", "requires"}:
            continue
        frm = e.get("from", "")
        to  = e.get("to", "")
        if not frm or not to or frm == to:
            continue
        # Only follow file->file edges for cycle detection
        # (symbol->file edges introduce false positives)
        frm_ext = Path(frm).suffix.lower()
        to_ext  = Path(to).suffix.lower()
        if frm_ext not in _CODE_EXTS or to_ext not in _CODE_EXTS:
            continue
        adj[frm].add(to)
        sym = e.get("symbol") or e.get("name") or ""
        if sym:
            edge_symbols[(frm, to)].append(sym)

    # DFS cycle detection (limit to 15 unique cycles to avoid overwhelming output)
    cycles: list[list[str]] = []
    seen: set[str] = set()
    MAX_CYCLES = 15

    def dfs(node: str, path: list[str], on_stack: set[str]) -> None:
        if len(cycles) >= MAX_CYCLES:
            return
        for nb in adj.get(node, set()):
            if nb in on_stack:
                try:
                    idx = path.index(nb)
                except ValueError:
                    idx = 0
                cyc = path[idx:]
                fkey = frozenset(cyc)
                if not any(frozenset(c) == fkey for c in cycles):
                    cycles.append(cyc[:])
                return
            if nb not in seen:
                on_stack.add(nb)
                path.append(nb)
                dfs(nb, path, on_stack)
                path.pop()
                on_stack.discard(nb)

    for start_node in list(adj)[:500]:
        if start_node not in seen:
            dfs(start_node, [start_node], {start_node})
            seen.add(start_node)

    issues: list[dict] = []
    for cycle in cycles:
        # Collect symbols that cross cycle boundaries
        crossing_symbols: list[str] = []
        for i in range(len(cycle)):
            frm = cycle[i]
            to  = cycle[(i + 1) % len(cycle)]
            syms = edge_symbols.get((frm, to), [])
            crossing_symbols.extend(syms[:3])
        crossing_symbols = list(dict.fromkeys(crossing_symbols))[:8]  # dedup, keep order

        # Generate fix suggestion
        if len(cycle) == 2:
            fix = (
                f"Extract shared types / interfaces between '{Path(cycle[0]).name}' "
                f"and '{Path(cycle[1]).name}' into a new module (e.g. 'shared_types.py' "
                "or 'interfaces/') that neither file imports."
            )
        else:
            fix = (
                f"Break the cycle by extracting the shared abstractions into a new module. "
                f"Typically the smallest file in the cycle ({Path(min(cycle, key=lambda f: len(f))).name}) "
                "should be refactored to not import from the others."
            )

        # Severity depends on cycle length and whether test files are involved
        has_test = any(_TEST_PAT.search(f) for f in cycle)
        severity = "low" if has_test else ("high" if len(cycle) == 2 else "medium")

        issues.append(
            {
                "type": "import_cycle",
                "severity": severity,
                "cycle_length": len(cycle),
                "files": cycle,
                "lines": [],
                "crossing_symbols": crossing_symbols,
                "description": (
                    f"Circular import cycle of length {len(cycle)}: "
                    + " → ".join(Path(f).name for f in cycle)
                    + (f". Key symbols crossing boundary: {', '.join(crossing_symbols)}"
                       if crossing_symbols else "")
                ),
                "fix": fix,
            }
        )

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 7. Dead event handlers
# ---------------------------------------------------------------------------

# Spring @EventListener
_SPRING_LISTENER_RE = re.compile(
    r"@EventListener\s*(?:\([^)]*\))?\s*\n\s*(?:public\s+)?(?:void\s+)?(\w+)\s*\(\s*(\w+)\s+\w+\s*\)",
    re.MULTILINE,
)
# Spring ApplicationEventPublisher.publishEvent / context.publishEvent
_SPRING_PUBLISH_RE  = re.compile(r"\.publishEvent\s*\(\s*new\s+(\w+)\s*[(\)]")

# JS/TS EventEmitter patterns
_JS_EMIT_RE         = re.compile(r"""\.emit\s*\(\s*['"`]([^'"`]+)['"`]""")
_JS_ON_RE           = re.compile(r"""\.on\s*\(\s*['"`]([^'"`]+)['"`]""")
_JS_ONCE_RE         = re.compile(r"""\.once\s*\(\s*['"`]([^'"`]+)['"`]""")
_JS_ADDLISTENER_RE  = re.compile(r"""\.addEventListener\s*\(\s*['"`]([^'"`]+)['"`]""")

# Python: event bus patterns (blinker signals, Django signals)
_PY_SIGNAL_SEND_RE  = re.compile(r"""(?:\.send|\.emit|\.dispatch)\s*\(\s*['"]?(\w+)['"]?""")
_PY_SIGNAL_CONN_RE  = re.compile(r"""(?:\.connect|\.receiver|@receiver)\s*\(\s*['"]?(\w+)['"]?""")


def find_dead_event_handlers(graph: dict) -> list[dict]:
    """Find event listeners/handlers for events that are never published/emitted.

    Uses graph edges (publishes_to / subscribes_from) when available, otherwise
    scans source files for publish/emit vs. listen/on patterns.

    Parameters
    ----------
    graph:
        Loaded info_graph.json dict.

    Returns
    -------
    list[dict] with type="dead_event_handler".
    """
    # ------------------------------------------------------------------
    # Attempt fast path: use graph edges
    # ------------------------------------------------------------------
    published_events: set[str] = set()
    subscribed_events: set[str] = set()

    for e in graph.get("edges", []):
        rel = e.get("rel", "")
        if rel in {"publishes_to", "emits", "dispatches"}:
            ev = e.get("event") or e.get("to", "")
            if ev:
                published_events.add(ev)
        elif rel in {"subscribes_from", "listens_to", "handles"}:
            ev = e.get("event") or e.get("from", "")
            if ev:
                subscribed_events.add(ev)

    # ------------------------------------------------------------------
    # Scan source files for patterns
    # ------------------------------------------------------------------
    root_hint: str = graph.get("root", "")
    file_ids: list[str] = [
        n.get("id", "")
        for n in graph.get("nodes", [])
        if n.get("kind") == "file"
        and Path(n.get("ext", "")).suffix.lower() in _CODE_EXTS
    ]

    # (event_name -> list of files that listen for it)
    listener_files: dict[str, list[str]] = defaultdict(list)
    # (event_name -> list of files that emit it)
    emitter_files: dict[str, list[str]] = defaultdict(list)

    # Java event types registered via @EventListener(ClassName)
    java_listener_types: dict[str, list[str]] = defaultdict(list)
    java_published_types: set[str] = set()

    for fid in file_ids:
        if not fid:
            continue
        content = _load_source_file(fid, root_hint)
        if content is None:
            continue
        ext = Path(fid).suffix.lower()

        if ext in {".java", ".kt"}:
            for m in _SPRING_LISTENER_RE.finditer(content):
                event_type = m.group(2)
                java_listener_types[event_type].append(fid)
                subscribed_events.add(event_type)
            for m in _SPRING_PUBLISH_RE.finditer(content):
                java_published_types.add(m.group(1))
                published_events.add(m.group(1))

        elif ext in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            for m in _JS_EMIT_RE.finditer(content):
                emitter_files[m.group(1)].append(fid)
                published_events.add(m.group(1))
            for m in _JS_ON_RE.finditer(content):
                listener_files[m.group(1)].append(fid)
                subscribed_events.add(m.group(1))
            for m in _JS_ONCE_RE.finditer(content):
                listener_files[m.group(1)].append(fid)
                subscribed_events.add(m.group(1))
            for m in _JS_ADDLISTENER_RE.finditer(content):
                listener_files[m.group(1)].append(fid)
                subscribed_events.add(m.group(1))

        elif ext == ".py":
            for m in _PY_SIGNAL_SEND_RE.finditer(content):
                ev = m.group(1)
                if ev not in {"self", "sender", "cls"}:
                    emitter_files[ev].append(fid)
                    published_events.add(ev)
            for m in _PY_SIGNAL_CONN_RE.finditer(content):
                ev = m.group(1)
                if ev not in {"self", "sender", "cls"}:
                    listener_files[ev].append(fid)
                    subscribed_events.add(ev)

    issues: list[dict] = []

    # Dead listeners: subscribed but never published
    dead_sub = subscribed_events - published_events
    for ev in sorted(dead_sub):
        files = list(set(
            java_listener_types.get(ev, []) +
            listener_files.get(ev, [])
        ))
        if not files:
            continue
        issues.append(
            {
                "type": "dead_event_handler",
                "severity": "low",
                "event_name": ev,
                "direction": "listener_without_publisher",
                "files": files[:5],
                "lines": [],
                "description": (
                    f"Event '{ev}' has listener(s) registered "
                    f"({', '.join(Path(f).name for f in files[:3])}) "
                    "but is never published/emitted anywhere in the codebase."
                ),
                "fix": (
                    f"Either add a publisher for '{ev}', or remove the dead listener "
                    "to reduce confusion."
                ),
            }
        )

    # Orphan publishers: emitted but never listened to
    dead_pub = published_events - subscribed_events
    for ev in sorted(dead_pub):
        files = list(set(
            ([fid for fid in file_ids if fid and
              _SPRING_PUBLISH_RE.search(_load_source_file(fid, root_hint) or "")]
             if ev in java_published_types else []) +
            emitter_files.get(ev, [])
        ))
        if not files:
            continue
        issues.append(
            {
                "type": "dead_event_handler",
                "severity": "low",
                "event_name": ev,
                "direction": "publisher_without_listener",
                "files": files[:5],
                "lines": [],
                "description": (
                    f"Event '{ev}' is published/emitted "
                    f"({', '.join(Path(f).name for f in files[:3])}) "
                    "but no listener is registered for it anywhere in the codebase."
                ),
                "fix": (
                    f"Either add a listener for '{ev}', or remove the unused publish call "
                    "to reduce dead code."
                ),
            }
        )

    return _sort_issues(issues[:50])


# ---------------------------------------------------------------------------
# 8. Missing pagination
# ---------------------------------------------------------------------------

# Python — SQLAlchemy: .all() call (we check whether .limit( appears before it)
_SA_ALL_RE = re.compile(r"\.all\s*\(\s*\)")
_SA_LIMIT_BEFORE_RE = re.compile(r"\.limit\s*\(")

# Python — Django ORM: .objects.all() or .objects.filter() without slicing/iterator/paginator
_DJ_QUERY_RE = re.compile(r"\.objects\s*\.\s*(?:all|filter|exclude)\s*\(")
_DJ_SAFE_RE  = re.compile(
    r"(?:\.limit\s*\(|\[:\d|\[:[-\w]|\.iterator\s*\(|Paginator\s*\(|paginate\s*\()",
)

# TypeScript/JavaScript — TypeORM: .find() without take / limit
_TS_TYPEORM_FIND_RE   = re.compile(r"\brepository\b.*?\.find\s*\(|\.find\s*\(\s*\{")
_TS_TAKE_LIMIT_RE     = re.compile(r"\btake\s*:|\.limit\s*\(")

# TypeScript/JavaScript — Mongoose: Model.find() without .limit()
_TS_MONGOOSE_FIND_RE  = re.compile(r"""[A-Z]\w*\.find\s*\(""")
_TS_MONGOOSE_LIMIT_RE = re.compile(r"\.limit\s*\(")

# Go — GORM: .Find(&v) without .Limit(n)
_GO_GORM_FIND_RE  = re.compile(r"\.Find\s*\(&\w+\)")
_GO_GORM_LIMIT_RE = re.compile(r"\.Limit\s*\(")

# Raw SQL SELECT without LIMIT
_SQL_SELECT_RE = re.compile(
    r"""(?:["'`])?\s*SELECT\b.+?\bFROM\b""",
    re.IGNORECASE | re.DOTALL,
)
_SQL_LIMIT_RE  = re.compile(r"\bLIMIT\b", re.IGNORECASE)

# Window to look backward for a .limit() in a query chain (chars)
_CHAIN_WINDOW = 300


def find_missing_pagination(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect database query calls that fetch unbounded result sets.

    Patterns covered:
    - SQLAlchemy .all() without a preceding .limit()
    - Django ORM .objects.all/filter/exclude() without slicing / .iterator() / Paginator
    - TypeORM .find() without take: / .limit()
    - Mongoose .find() without .limit()
    - GORM .Find() without .Limit()
    - Raw SQL SELECT … FROM … without LIMIT

    Returns
    -------
    list[dict] with type="missing_pagination".
    """
    issues: list[dict] = []
    _PY_EXTS  = {".py"}
    _TS_EXTS  = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    _GO_EXTS  = {".go"}
    _ALL_EXTS = _PY_EXTS | _TS_EXTS | _GO_EXTS

    for fpath, content in _iter_source_files(
        project_root, _ALL_EXTS, skip_tests=True, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        ext = fpath.suffix.lower()
        lines_list = content.splitlines()

        if ext in _PY_EXTS:
            # SQLAlchemy: .all() without .limit() in preceding chain window
            for m in _SA_ALL_RE.finditer(content):
                line_no = content[: m.start()].count("\n") + 1
                # look back up to _CHAIN_WINDOW chars for .limit(
                window_start = max(0, m.start() - _CHAIN_WINDOW)
                window = content[window_start: m.start()]
                if not _SA_LIMIT_BEFORE_RE.search(window):
                    snippet = lines_list[line_no - 1].strip()
                    issues.append({
                        "type": "missing_pagination",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "pattern": "SQLAlchemy .all() without .limit()",
                        "severity": "medium",
                        "fix": "Add .limit(page_size) before .all() or use paginate().",
                    })

            # Django ORM
            for m in _DJ_QUERY_RE.finditer(content):
                line_no = content[: m.start()].count("\n") + 1
                # look forward and backward for safe pagination
                window_start = max(0, m.start() - 50)
                window_end   = min(len(content), m.end() + 300)
                window = content[window_start:window_end]
                if not _DJ_SAFE_RE.search(window):
                    snippet = lines_list[line_no - 1].strip()
                    issues.append({
                        "type": "missing_pagination",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "pattern": "Django ORM .objects.all/filter() without pagination",
                        "severity": "medium",
                        "fix": "Use Django Paginator, queryset slicing [:N], or .iterator().",
                    })

        elif ext in _TS_EXTS:
            # TypeORM .find()
            for m in _TS_TYPEORM_FIND_RE.finditer(content):
                line_no = content[: m.start()].count("\n") + 1
                window_start = max(0, m.start() - 50)
                window_end   = min(len(content), m.end() + 200)
                window = content[window_start:window_end]
                if not _TS_TAKE_LIMIT_RE.search(window):
                    snippet = lines_list[line_no - 1].strip()
                    issues.append({
                        "type": "missing_pagination",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "pattern": "TypeORM .find() without {take: N} or .limit()",
                        "severity": "medium",
                        "fix": "Pass {take: pageSize, skip: offset} to .find() or use .limit(N).",
                    })

            # Mongoose .find()
            for m in _TS_MONGOOSE_FIND_RE.finditer(content):
                line_no = content[: m.start()].count("\n") + 1
                window_start = max(0, m.start() - 50)
                window_end   = min(len(content), m.end() + 200)
                window = content[window_start:window_end]
                if not _TS_MONGOOSE_LIMIT_RE.search(window):
                    snippet = lines_list[line_no - 1].strip()
                    issues.append({
                        "type": "missing_pagination",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "pattern": "Mongoose .find() without .limit()",
                        "severity": "medium",
                        "fix": "Chain .limit(pageSize).skip(offset) on the Mongoose query.",
                    })

        elif ext in _GO_EXTS:
            # GORM .Find()
            for m in _GO_GORM_FIND_RE.finditer(content):
                line_no = content[: m.start()].count("\n") + 1
                window_start = max(0, m.start() - _CHAIN_WINDOW)
                window = content[window_start: m.start()]
                if not _GO_GORM_LIMIT_RE.search(window):
                    snippet = lines_list[line_no - 1].strip()
                    issues.append({
                        "type": "missing_pagination",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "pattern": "GORM .Find() without .Limit()",
                        "severity": "medium",
                        "fix": "Chain .Limit(pageSize).Offset(offset) before .Find().",
                    })

    # Raw SQL strings across all source files
    for fpath, content in _iter_source_files(
        project_root, _ALL_EXTS, skip_tests=True, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        lines_list = content.splitlines()
        for m in _SQL_SELECT_RE.finditer(content):
            # Check if LIMIT appears in the same string literal (up to 400 chars ahead)
            window_end = min(len(content), m.end() + 400)
            window = content[m.start():window_end]
            if not _SQL_LIMIT_RE.search(window):
                line_no = content[: m.start()].count("\n") + 1
                snippet = lines_list[line_no - 1].strip()[:100]
                issues.append({
                    "type": "missing_pagination",
                    "file": rel,
                    "line": line_no,
                    "snippet": snippet,
                    "pattern": "Raw SQL SELECT without LIMIT clause",
                    "severity": "medium",
                    "fix": "Add a LIMIT clause (and OFFSET for pagination) to the SQL query.",
                })

    # Deduplicate by (file, line) — raw SQL scan may overlap with ORM scan
    seen: set[tuple[str, int]] = set()
    unique: list[dict] = []
    for item in issues:
        key = (item["file"], item["line"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


# ---------------------------------------------------------------------------
# 9. Missing indexes on foreign key fields
# ---------------------------------------------------------------------------

# Prisma: @relation(...)  — field name from preceding line
_PRISMA_MODEL_RE    = re.compile(r"^model\s+(\w+)\s*\{", re.MULTILINE)
_PRISMA_RELATION_RE = re.compile(r"^\s+(\w+)\s+\w+.*?@relation", re.MULTILINE)
_PRISMA_INDEX_RE    = re.compile(r"@@index\s*\(\s*\[([^\]]+)\]")

# SQLAlchemy: Column(Integer, ForeignKey(...)) without index=True
_SA_FK_COL_RE     = re.compile(
    r"""^(\s*)(\w+)\s*=\s*(?:mapped_column|Column)\s*\([^)]*ForeignKey\s*\([^)]+\)""",
    re.MULTILINE,
)
_SA_INDEX_TRUE_RE = re.compile(r"\bindex\s*=\s*True")

# Django: ForeignKey(..., db_index=False)  — Django adds index by default, flag only explicit False
_DJ_FK_NO_IDX_RE  = re.compile(
    r"""(\w+)\s*=\s*models\.ForeignKey\s*\([^)]*db_index\s*=\s*False[^)]*\)""",
    re.MULTILINE,
)

# TypeORM: @ManyToOne or @JoinColumn without preceding @Index()
_TS_MANYTOONE_RE  = re.compile(r"@(?:ManyToOne|JoinColumn)\s*\(")
_TS_INDEX_RE      = re.compile(r"@Index\s*\(")

# ActiveRecord: belongs_to :model
_AR_BELONGS_TO_RE = re.compile(r"^\s*belongs_to\s+:(\w+)", re.MULTILINE)
# migration add_index
_AR_ADD_INDEX_RE  = re.compile(r"add_index\s+[:\w\"']+,\s*:(\w+)")


def find_missing_indexes(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect foreign key fields that likely lack a database index.

    Covers Prisma, SQLAlchemy, Django (explicit db_index=False only),
    TypeORM, and ActiveRecord.

    Returns
    -------
    list[dict] with type="missing_index".
    """
    issues: list[dict] = []
    root = Path(project_root)

    # ── Prisma ──────────────────────────────────────────────────────────────
    for fpath, content in _iter_source_files(
        root, {".prisma"}, skip_tests=False, max_files=500
    ):
        rel = _rel(fpath, project_root)
        # Find each model block
        model_starts = list(_PRISMA_MODEL_RE.finditer(content))
        for idx_m, model_m in enumerate(model_starts):
            model_name = model_m.group(1)
            block_start = model_m.end()
            block_end   = model_starts[idx_m + 1].start() if idx_m + 1 < len(model_starts) else len(content)
            block = content[block_start:block_end]

            # Collect @@index field names in this block
            indexed_fields: set[str] = set()
            for im in _PRISMA_INDEX_RE.finditer(block):
                for fname in im.group(1).split(","):
                    indexed_fields.add(fname.strip())

            # Check each @relation field
            for rm in _PRISMA_RELATION_RE.finditer(block):
                field_name = rm.group(1)
                if field_name not in indexed_fields:
                    line_no = content[:block_start + rm.start()].count("\n") + 1
                    issues.append({
                        "type": "missing_index",
                        "file": rel,
                        "line": line_no,
                        "field": field_name,
                        "target": model_name,
                        "severity": "medium",
                        "fix": "Add an index on the foreign key column to avoid full-table scans on joins.",
                    })

    # ── SQLAlchemy ───────────────────────────────────────────────────────────
    for fpath, content in _iter_source_files(
        root, {".py"}, skip_tests=False, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        for m in _SA_FK_COL_RE.finditer(content):
            # Check the full Column(...) expression for index=True
            col_end = content.find(")", m.end()) + 1
            col_expr = content[m.start():col_end] if col_end > m.start() else m.group(0)
            if not _SA_INDEX_TRUE_RE.search(col_expr):
                field_name = m.group(2)
                line_no = content[: m.start()].count("\n") + 1
                # extract target table from ForeignKey("table.col")
                fk_m = re.search(r'ForeignKey\s*\(\s*["\']([^"\']+)["\']', col_expr)
                target = fk_m.group(1).split(".")[0] if fk_m else "unknown"
                issues.append({
                    "type": "missing_index",
                    "file": rel,
                    "line": line_no,
                    "field": field_name,
                    "target": target,
                    "severity": "medium",
                    "fix": "Add an index on the foreign key column to avoid full-table scans on joins.",
                })

    # ── Django (explicit db_index=False only) ───────────────────────────────
    for fpath, content in _iter_source_files(
        root, {".py"}, skip_tests=False, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        for m in _DJ_FK_NO_IDX_RE.finditer(content):
            field_name = m.group(1)
            line_no = content[: m.start()].count("\n") + 1
            # extract target model
            tgt_m = re.search(r'ForeignKey\s*\(\s*["\']?(\w+)', m.group(0))
            target = tgt_m.group(1) if tgt_m else "unknown"
            issues.append({
                "type": "missing_index",
                "file": rel,
                "line": line_no,
                "field": field_name,
                "target": target,
                "severity": "medium",
                "fix": "Add an index on the foreign key column to avoid full-table scans on joins.",
            })

    # ── TypeORM ─────────────────────────────────────────────────────────────
    for fpath, content in _iter_source_files(
        root, {".ts", ".tsx"}, skip_tests=False, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        lines_list = content.splitlines()
        for m in _TS_MANYTOONE_RE.finditer(content):
            line_no = content[: m.start()].count("\n") + 1
            # Check within 5 preceding lines for @Index decorator
            start = max(0, line_no - 6)
            preceding = "\n".join(lines_list[start: line_no - 1])
            if not _TS_INDEX_RE.search(preceding):
                snippet_line = lines_list[line_no - 1].strip()
                # Try to extract field name from next non-empty line
                field_name = "unknown"
                for nxt in lines_list[line_no:line_no + 3]:
                    prop_m = re.match(r"\s*(\w+)\s*[!?:]", nxt)
                    if prop_m:
                        field_name = prop_m.group(1)
                        break
                issues.append({
                    "type": "missing_index",
                    "file": rel,
                    "line": line_no,
                    "field": field_name,
                    "target": snippet_line[:60],
                    "severity": "medium",
                    "fix": "Add an index on the foreign key column to avoid full-table scans on joins.",
                })

    # ── ActiveRecord ─────────────────────────────────────────────────────────
    # Collect all add_index calls from migration files
    migration_indexed: set[str] = set()
    migrations_root = root / "db" / "migrate"
    if migrations_root.exists():
        for fpath, content in _iter_source_files(
            str(migrations_root), {".rb"}, skip_tests=False, max_files=500
        ):
            for m in _AR_ADD_INDEX_RE.finditer(content):
                migration_indexed.add(m.group(1))

    for fpath, content in _iter_source_files(
        root, {".rb"}, skip_tests=False, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        for m in _AR_BELONGS_TO_RE.finditer(content):
            assoc_name = m.group(1)
            # Default FK column name is association_name + "_id"
            fk_col = assoc_name + "_id"
            if fk_col not in migration_indexed:
                line_no = content[: m.start()].count("\n") + 1
                issues.append({
                    "type": "missing_index",
                    "file": rel,
                    "line": line_no,
                    "field": fk_col,
                    "target": assoc_name,
                    "severity": "medium",
                    "fix": "Add an index on the foreign key column to avoid full-table scans on joins.",
                })

    # Deduplicate by (file, line, field)
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict] = []
    for item in issues:
        key = (item["file"], item["line"], item["field"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


# ---------------------------------------------------------------------------
# 10. N+1 query risk
# ---------------------------------------------------------------------------

# Loop markers
_PY_FOR_RE        = re.compile(r"^\s*for\s+\w", re.MULTILINE)
_PY_WHILE_RE      = re.compile(r"^\s*while\s+", re.MULTILINE)
# Python DB access inside loop
_PY_DB_CALL_RE    = re.compile(r"\.objects\.\s*(?:get|filter|exclude|all|first|last|aggregate)\s*\(|\.query\.")

# JS/TS loop patterns
_TS_LOOP_RE       = re.compile(r"(?:\.forEach\s*\(|\.map\s*\(|for\s*\(|for\s+\w)")
# JS/TS DB calls inside loop
_TS_DB_CALL_RE    = re.compile(r"\.find(?:One)?\s*\(|\.query\s*\(|\.execute\s*\(|await\s+\w+\.find")

# Ruby loop
_RB_LOOP_RE       = re.compile(r"\.each\s*(?:do|\{)|\.map\s*(?:do|\{)")
# Ruby DB call
_RB_DB_CALL_RE    = re.compile(r"\.find\s*\(|\.where\s*\(|[A-Z]\w*\.find\b")

_N1_PROXIMITY = 20  # max lines between loop and DB call to flag


def _lines_in_range(content: str, start_line: int, end_line: int) -> str:
    """Return content between start_line and end_line (1-indexed, inclusive)."""
    lines = content.splitlines()
    return "\n".join(lines[max(0, start_line - 1): min(len(lines), end_line)])


def find_n_plus_one_risk(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect patterns where a DB query is called inside a loop.

    Only flags when the loop and the query call are within 20 lines of each
    other (same function body heuristic).

    Returns
    -------
    list[dict] with type="n_plus_one_risk".
    """
    issues: list[dict] = []

    _PY_EXTS = {".py"}
    _TS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    _RB_EXTS = {".rb"}
    _ALL_EXTS = _PY_EXTS | _TS_EXTS | _RB_EXTS

    for fpath, content in _iter_source_files(
        project_root, _ALL_EXTS, skip_tests=True, max_files=3000
    ):
        rel = _rel(fpath, project_root)
        ext = fpath.suffix.lower()

        if ext in _PY_EXTS:
            loop_pats = [_PY_FOR_RE, _PY_WHILE_RE]
            db_re     = _PY_DB_CALL_RE
        elif ext in _TS_EXTS:
            loop_pats = [_TS_LOOP_RE]
            db_re     = _TS_DB_CALL_RE
        elif ext in _RB_EXTS:
            loop_pats = [_RB_LOOP_RE]
            db_re     = _RB_DB_CALL_RE
        else:
            continue

        lines_list = content.splitlines()

        for loop_pat in loop_pats:
            for lm in loop_pat.finditer(content):
                loop_line = content[: lm.start()].count("\n") + 1
                # Scan the next _N1_PROXIMITY lines for a DB query call
                end_line = min(len(lines_list), loop_line + _N1_PROXIMITY)
                window   = _lines_in_range(content, loop_line + 1, end_line)
                db_m     = db_re.search(window)
                if db_m:
                    db_line = loop_line + 1 + window[: db_m.start()].count("\n")
                    snippet = lines_list[db_line - 1].strip() if db_line <= len(lines_list) else ""
                    issues.append({
                        "type": "n_plus_one_risk",
                        "file": rel,
                        "line": loop_line,
                        "snippet": snippet,
                        "severity": "high",
                        "fix": (
                            "Use eager loading (.includes(), .eager_load(), joinedload()) "
                            "instead of querying inside loops."
                        ),
                    })

    # Deduplicate by (file, line)
    seen: set[tuple[str, int]] = set()
    unique: list[dict] = []
    for item in issues:
        key = (item["file"], item["line"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


# ---------------------------------------------------------------------------
# 11. Unused env vars
# ---------------------------------------------------------------------------

def _walk_project(project_root: str):
    """Yield (dirpath, dirnames, filenames) skipping common non-source dirs."""
    root = Path(project_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS_COMMON and not d.startswith(".")
        ]
        yield dirpath, dirnames, filenames


def _collect_env_declarations(project_root: str) -> dict[str, list[tuple[str, int, str]]]:
    """Return {var_name: [(rel_path, line_no, snippet), ...]} from .env* files."""
    root = Path(project_root)
    declared: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fname not in _ENV_FILE_NAMES and not fname.startswith(".env."):
                continue
            rel = str(fpath.relative_to(root))
            try:
                lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                m = _ENV_DECL_RE.match(stripped)
                if m:
                    var_name = m.group(1)
                    value = m.group(2).strip()
                    snippet = stripped[:80]
                    declared[var_name].append((rel, i, snippet))
    return declared


def _collect_env_usages(project_root: str) -> set[str]:
    """Return set of env var names referenced in source code."""
    used: set[str] = set()
    root = Path(project_root)
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() not in _SRC_EXTS_ENV:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for pat in _USAGE_PATTERNS:
                for m in pat.finditer(content):
                    used.add(m.group(1))
    return used


def find_unused_env_vars(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Find variables declared in .env files but never referenced in source code.

    Returns
    -------
    list[dict] with type="unused_env_var".
    """
    declared = _collect_env_declarations(project_root)
    used_vars = _collect_env_usages(project_root)

    # Also collect all vars declared across .env.example — these are templates, skip
    example_vars: set[str] = set()
    root = Path(project_root)
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            if fname == ".env.example":
                fpath = Path(dirpath) / fname
                try:
                    lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    continue
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("#") or not stripped:
                        continue
                    m = _ENV_DECL_RE.match(stripped)
                    if m:
                        example_vars.add(m.group(1))

    issues: list[dict] = []
    for var_name, occurrences in declared.items():
        if var_name in _AUTO_VARS:
            continue
        if var_name in example_vars:
            continue
        if var_name in used_vars:
            continue
        # Skip if value is empty (template placeholder)
        # occurrences: list of (rel_path, line_no, snippet)
        for rel_path, line_no, snippet in occurrences:
            # Check if this specific file is a .env.example
            if rel_path.endswith(".env.example"):
                continue
            # Extract value from snippet "VAR=value"
            m = _ENV_DECL_RE.match(snippet)
            if m and m.group(2).strip() == "":
                continue
            issues.append({
                "type": "unused_env_var",
                "file": rel_path,
                "line": line_no,
                "snippet": snippet,
                "var_name": var_name,
                "severity": "low",
                "fix": "Remove this variable from .env or add it to .env.example if it's a template.",
            })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 12. Missing env vars
# ---------------------------------------------------------------------------

def find_missing_env_vars(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Find environment variables referenced in source code but absent from all .env files.

    Returns
    -------
    list[dict] with type="missing_env_var".
    """
    root = Path(project_root)

    # Build declared_vars across ALL .env* files (including .env.example)
    declared_vars: set[str] = set()
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fname not in _ENV_FILE_NAMES and not fname.startswith(".env."):
                continue
            try:
                lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                m = _ENV_DECL_RE.match(stripped)
                if m:
                    declared_vars.add(m.group(1))

    # Also scan docker-compose and K8s YAML for env: section defined vars
    docker_k8s_vars: set[str] = set()
    _DC_ENV_DEFINED_RE = re.compile(r"^\s{0,10}([A-Z_][A-Z0-9_]*)\s*:", re.MULTILINE)
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            if fname.lower() in {"docker-compose.yml", "docker-compose.yaml"} or \
               (fname.endswith((".yml", ".yaml")) and
                    any(k in fname.lower() for k in ("k8s", "kube", "deploy", "manifest"))):
                fpath = Path(dirpath) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for m in _DC_ENV_DEFINED_RE.finditer(content):
                    docker_k8s_vars.add(m.group(1))
    declared_vars.update(docker_k8s_vars)

    # Build code_vars: var -> [(file, line, snippet)] — limit to 3 occurrences per var
    code_vars: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() not in _SRC_EXTS_ENV:
                continue
            rel = str(fpath.relative_to(root))
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines_list = content.splitlines()
            for pat in _USAGE_PATTERNS:
                for m in pat.finditer(content):
                    var_name = m.group(1)
                    if var_name in _MISSING_ENV_SKIP:
                        continue
                    if len(code_vars[var_name]) >= 3:
                        continue
                    line_no = content[:m.start()].count("\n") + 1
                    snippet = lines_list[line_no - 1].strip()[:80] if line_no <= len(lines_list) else m.group(0)
                    code_vars[var_name].append((rel, line_no, snippet))

    issues: list[dict] = []
    for var_name, occurrences in code_vars.items():
        if var_name in declared_vars:
            continue
        for rel, line_no, snippet in occurrences:
            issues.append({
                "type": "missing_env_var",
                "file": rel,
                "line": line_no,
                "snippet": snippet,
                "var_name": var_name,
                "severity": "high",
                "fix": "Add this variable to .env.example and all relevant .env files.",
            })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 13. Port conflicts
# ---------------------------------------------------------------------------

def _extract_ports_from_file(
    fpath: Path,
    content: str,
    rel: str,
) -> list[tuple[int, str, int, str]]:
    """Return list of (port, rel_path, line_no, snippet) from a single file."""
    results: list[tuple[int, str, int, str]] = []
    fname = fpath.name.lower()
    ext   = fpath.suffix.lower()
    lines = content.splitlines()

    def _add(port_str: str, pos: int, raw_snippet: str) -> None:
        try:
            port = int(port_str)
        except ValueError:
            return
        if port in _SKIP_PORTS:
            return
        line_no = content[:pos].count("\n") + 1
        snippet = (lines[line_no - 1].strip()[:80] if line_no <= len(lines) else raw_snippet)
        results.append((port, rel, line_no, snippet))

    # .env / config files
    if fname.startswith(".env") or ext in {".env", ".properties", ".cfg", ".ini"}:
        for m in _PORT_ENV_RE.finditer(content):
            _add(m.group(2), m.start(), m.group(0))

    # docker-compose
    if "docker-compose" in fname and ext in {".yml", ".yaml"}:
        # ports: section
        in_ports = False
        for i, line in enumerate(lines):
            if re.match(r"\s*ports\s*:", line):
                in_ports = True
                continue
            if in_ports:
                dm = _DC_PORT_RE.search(line)
                if dm:
                    pos = sum(len(l) + 1 for l in lines[:i])
                    _add(dm.group(1), pos, line.strip())
                elif not line.strip().startswith("-") and line.strip():
                    in_ports = False
        # environment section PORT:
        for m in _DC_ENV_PORT_RE.finditer(content):
            _add(m.group(1), m.start(), m.group(0))

    # Kubernetes YAML
    if ext in {".yml", ".yaml"} and "docker-compose" not in fname:
        for m in _K8S_PORT_RE.finditer(content):
            _add(m.group(1), m.start(), m.group(0))

    # Python
    if ext == ".py":
        for m in _PY_PORT_RE.finditer(content):
            port_str = m.group(1) or m.group(2)
            if port_str:
                _add(port_str, m.start(), m.group(0))

    # Go
    if ext == ".go":
        for m in _GO_PORT_RE.finditer(content):
            port_str = m.group(1) or m.group(2)
            if port_str:
                _add(port_str, m.start(), m.group(0))

    # JavaScript / TypeScript
    if ext in {".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}:
        for m in _JS_PORT_RE.finditer(content):
            port_str = m.group(1) or m.group(2)
            if port_str:
                _add(port_str, m.start(), m.group(0))

    # Spring Boot application.properties
    if fname in {"application.properties", "application.yml", "application.yaml"}:
        for m in _SPRING_PORT_RE.finditer(content):
            _add(m.group(1), m.start(), m.group(0))

    return results


def find_port_conflicts(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Find cases where two different files/services bind the same port.

    Returns
    -------
    list[dict] with type="port_conflict".
    """
    root = Path(project_root)
    # port -> list of (rel_path, line_no, snippet)
    port_map: dict[int, list[tuple[str, int, str]]] = defaultdict(list)

    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext   = fpath.suffix.lower()
            bname = fname.lower()
            # Only scan relevant file types
            if not (
                bname.startswith(".env") or
                ext in {".env", ".properties", ".cfg", ".ini",
                        ".yml", ".yaml", ".py", ".go",
                        ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
            ):
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = str(fpath.relative_to(root))
            for port, _rel, line_no, snippet in _extract_ports_from_file(fpath, content, rel):
                port_map[port].append((_rel, line_no, snippet))

    issues: list[dict] = []
    for port, entries in port_map.items():
        # Deduplicate by file — same file can mention port multiple times
        seen_files: dict[str, tuple[int, str]] = {}
        for rel_path, line_no, snippet in entries:
            if rel_path not in seen_files:
                seen_files[rel_path] = (line_no, snippet)
        if len(seen_files) < 2:
            continue
        files = list(seen_files.keys())
        first_file   = files[0]
        first_line, first_snippet = seen_files[first_file]
        issues.append({
            "type": "port_conflict",
            "file": first_file,
            "line": first_line,
            "snippet": first_snippet,
            "port": port,
            "conflicting_files": files,
            "severity": "high",
            "fix": f"Port {port} is declared in multiple files. Assign unique ports per service.",
        })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# 14. Race condition risk
# ---------------------------------------------------------------------------

def find_race_conditions(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Heuristic detection of potential race conditions.

    Covers:
    - Go: goroutines + shared map/slice mutations without sync import
    - Python: threading.Thread + shared mutable state without Lock
    - JavaScript/TypeScript: Promise.all + shared mutable aggregator variable

    Returns
    -------
    list[dict] with type="race_condition_risk".
    """
    issues: list[dict] = []
    root = Path(project_root)

    _EXTS = {".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext   = fpath.suffix.lower()
            if ext not in _EXTS:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel   = str(fpath.relative_to(root))
            lines = content.splitlines()

            # ── Go ──────────────────────────────────────────────────────────
            if ext == ".go":
                goroutine_matches = list(_GO_GOROUTINE_RE.finditer(content))
                if not goroutine_matches:
                    continue
                has_sync = bool(_GO_SYNC_RE.search(content))
                if has_sync:
                    continue
                # Check for shared map/slice writes in the same file
                has_shared_write = bool(_GO_MAP_WRITE_RE.search(content))
                # Check for variable mutations inside goroutine bodies
                shared_mut_m = _GO_SHARED_MUT_RE.search(content)
                if has_shared_write or shared_mut_m:
                    # Use the first goroutine launch as the issue location
                    m = goroutine_matches[0]
                    line_no = content[:m.start()].count("\n") + 1
                    snippet = lines[line_no - 1].strip()[:80] if line_no <= len(lines) else m.group(0)
                    issues.append({
                        "type": "race_condition_risk",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "language": "go",
                        "pattern": "goroutine writing to shared variable without mutex",
                        "severity": "high",
                        "fix": "Use sync.Mutex/atomic (Go), threading.Lock (Python), or avoid shared state in concurrent code.",
                    })

            # ── Python ──────────────────────────────────────────────────────
            elif ext == ".py":
                thread_matches = list(_PY_THREAD_RE.finditer(content))
                if not thread_matches:
                    continue
                has_lock = bool(_PY_LOCK_RE.search(content))
                if has_lock:
                    continue
                # Check for shared mutable variable mutations (simple heuristic)
                # Look for list/dict appends or counter increments outside function defs
                has_shared_mut = bool(re.search(
                    r"(?:\.append\s*\(|\.extend\s*\(|\+\+|\+=\s*1|count\s*\+=)",
                    content,
                ))
                if has_shared_mut:
                    m = thread_matches[0]
                    line_no = content[:m.start()].count("\n") + 1
                    snippet = lines[line_no - 1].strip()[:80] if line_no <= len(lines) else m.group(0)
                    issues.append({
                        "type": "race_condition_risk",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "language": "python",
                        "pattern": "threading.Thread with shared mutable state and no Lock",
                        "severity": "high",
                        "fix": "Use sync.Mutex/atomic (Go), threading.Lock (Python), or avoid shared state in concurrent code.",
                    })

            # ── JavaScript / TypeScript ──────────────────────────────────────
            elif ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                pa_matches = list(_JS_PROMISE_ALL_RE.finditer(content))
                if not pa_matches:
                    continue
                for pa_m in pa_matches:
                    # Look at 20 lines before Promise.all for shared aggregator declaration
                    pa_line = content[:pa_m.start()].count("\n") + 1
                    start_line = max(0, pa_line - 20)
                    window_before = "\n".join(lines[start_line:pa_line])
                    agg_m = _JS_SHARED_AGG_RE.search(window_before)
                    if not agg_m:
                        continue
                    # Look at 40 lines after Promise.all for mutations
                    end_line = min(len(lines), pa_line + 40)
                    window_after = "\n".join(lines[pa_line:end_line])
                    if not _JS_MUTATION_RE.search(window_after):
                        continue
                    line_no = pa_line
                    snippet = lines[line_no - 1].strip()[:80] if line_no <= len(lines) else pa_m.group(0)
                    issues.append({
                        "type": "race_condition_risk",
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                        "language": "javascript",
                        "pattern": "Promise.all with shared mutable aggregator variable",
                        "severity": "high",
                        "fix": "Use sync.Mutex/atomic (Go), threading.Lock (Python), or avoid shared state in concurrent code.",
                    })
                    break  # one finding per file is enough

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# Phase 23 — Idempotency Gaps
# ---------------------------------------------------------------------------

# Consumer detection patterns
_GO_CONSUMER_PARAM_RE = re.compile(
    r"func\s+\w+\s*\([^)]*\*(?:kafka\.Message|sqs\.Message|amqp\.Delivery|ConsumerRecord|nats\.Msg)[^)]*\)",
)
_PY_CONSUMER_DECO_RE = re.compile(r"@(?:app|celery|shared_task)\.task|@shared_task")
_PY_CONSUMER_PARAM_RE = re.compile(
    r"def\s+\w+\s*\([^)]*\b(?:message|msg|record|event)\b[^)]*\).*:",
)
_JS_CONSUMER_RE = re.compile(
    r"channel\.consume\s*\(|\.subscribe\s*\(|queue\.process\s*\(|\.on\s*\(\s*['\"]message['\"]",
)

# Insert patterns (flag when found without dedup)
_INSERT_RE = re.compile(
    r'db\.Exec\s*\(\s*["\']INSERT|cursor\.execute\s*\(\s*["\']INSERT|'
    r'\.query\s*\(\s*["\']INSERT|repository\.save\s*\(|(?<!\w)\.save\s*\(',
    re.IGNORECASE,
)
_ON_CONFLICT_RE = re.compile(r"ON CONFLICT", re.IGNORECASE)

# Deduplication guard patterns (check within preceding 15 lines)
_DEDUP_GUARD_RE = re.compile(
    r"redis\.get\s*\(|cache\.has\s*\(|seen\.has\s*\(|processed|idempotency",
    re.IGNORECASE,
)

# Payment / notification API patterns
_PAYMENT_NOTIF_RE = re.compile(
    r"stripe\.charges\.create\s*\(|stripe\.paymentIntents\.create\s*\(|"
    r"Stripe\.Charge\.create\s*\(|client\.Charges\.New\s*\(|"
    r"twilio\.messages\.create\s*\(|sg\.send\s*\(|"
    r"transporter\.sendMail\s*\(|mailer\.Send\s*\(",
    re.IGNORECASE,
)
_IDEM_KEY_RE = re.compile(
    r"IdempotencyKey|idempotency_key|x-idempotency-key|Idempotency-Key",
    re.IGNORECASE,
)

# Batch function patterns
_BATCH_FUNC_RE = re.compile(
    r"(?:def|func)\s+(?:batch_\w+|\w+Batch|\w+Cron|\w+Scheduled|send_all_\w+|process_all_\w+)\s*[(\[]|"
    r"@(?:Scheduled|app\.task|shared_task)\s",
    re.IGNORECASE,
)
_NO_PAGINATION_RE = re.compile(
    r'\.find(?:All)?\s*\(\s*\)|SELECT\s+\*\s+FROM\s+\w+\s*(?:WHERE[^;]*)?;',
    re.IGNORECASE,
)
_PAGINATION_CHECK_RE = re.compile(
    r"\bLIMIT\b|\blimit\b|\bper_page\b|\bPageRequest\b|\boffset\b",
    re.IGNORECASE,
)
_CHECKPOINT_RE = re.compile(
    r"checkpoint|last_processed|offset|cursor",
    re.IGNORECASE,
)

# Bull/BullMQ without jobId
_BULL_ADD_RE = re.compile(r"queue\.add\s*\(")
_JOB_ID_RE = re.compile(r"\{\s*jobId\s*:")


def find_idempotency_gaps(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect MQ consumers and batch jobs that perform non-idempotent side effects
    without deduplication.

    Covers four patterns:
    - IDEM-001: MQ consumer + INSERT without ON CONFLICT or dedup guard
    - IDEM-002: MQ consumer + Stripe/Twilio/SendGrid without idempotency key
    - IDEM-003: Batch job fetching all records + side effect without checkpoint
    - IDEM-004: Bull/BullMQ queue.add() without jobId when job does payment/email

    Returns
    -------
    list[dict] with type="idempotency_gap".
    """
    issues: list[dict] = []
    root = Path(project_root)
    _EXTS = {".go", ".py", ".js", ".ts", ".jsx", ".tsx", ".java"}

    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()
            if ext not in _EXTS:
                continue
            # Skip test files
            rel = str(fpath.relative_to(root))
            if _TEST_PAT.search(rel):
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines = content.splitlines()

            # ── IDEM-001 / IDEM-002: consumer functions ──────────────────────
            consumer_line_indices: list[int] = []

            if ext == ".go":
                for m in _GO_CONSUMER_PARAM_RE.finditer(content):
                    consumer_line_indices.append(content[:m.start()].count("\n"))
            elif ext == ".py":
                for m in _PY_CONSUMER_DECO_RE.finditer(content):
                    consumer_line_indices.append(content[:m.start()].count("\n"))
                for m in _PY_CONSUMER_PARAM_RE.finditer(content):
                    idx = content[:m.start()].count("\n")
                    if idx not in consumer_line_indices:
                        consumer_line_indices.append(idx)
            elif ext in {".js", ".ts", ".jsx", ".tsx"}:
                for m in _JS_CONSUMER_RE.finditer(content):
                    consumer_line_indices.append(content[:m.start()].count("\n"))

            for c_idx in consumer_line_indices:
                # Look at next 30 lines for INSERT pattern (IDEM-001)
                window30 = lines[c_idx: c_idx + 30]
                window30_text = "\n".join(window30)
                insert_m = _INSERT_RE.search(window30_text)
                if insert_m:
                    # Flag if no ON CONFLICT in the insert line/nearby
                    has_conflict = bool(_ON_CONFLICT_RE.search(window30_text))
                    if not has_conflict:
                        # Also check preceding 15 lines for dedup guard
                        pre_text = "\n".join(lines[max(0, c_idx - 15): c_idx])
                        has_dedup = bool(_DEDUP_GUARD_RE.search(pre_text + window30_text))
                        if not has_dedup:
                            insert_line_no = c_idx + window30_text[:insert_m.start()].count("\n") + 1
                            snippet = lines[insert_line_no - 1].strip()[:80] if insert_line_no <= len(lines) else insert_m.group(0)
                            issues.append({
                                "type": "idempotency_gap",
                                "rule_id": "IDEM-001",
                                "severity": "high",
                                "file": rel,
                                "line": insert_line_no,
                                "message": (
                                    f"MQ consumer performs INSERT without ON CONFLICT or "
                                    f"deduplication guard — duplicate messages cause duplicate rows."
                                ),
                                "snippet": snippet,
                                "fix": (
                                    "Add ON CONFLICT DO NOTHING / ON CONFLICT DO UPDATE, or check "
                                    "a deduplication key (Redis, DB unique constraint) before inserting."
                                ),
                            })

                # Look at next 40 lines for payment/notification call (IDEM-002)
                window40 = lines[c_idx: c_idx + 40]
                window40_text = "\n".join(window40)
                pay_m = _PAYMENT_NOTIF_RE.search(window40_text)
                if pay_m:
                    has_idem_key = bool(_IDEM_KEY_RE.search(window40_text))
                    if not has_idem_key:
                        pay_line_no = c_idx + window40_text[:pay_m.start()].count("\n") + 1
                        snippet = lines[pay_line_no - 1].strip()[:80] if pay_line_no <= len(lines) else pay_m.group(0)
                        issues.append({
                            "type": "idempotency_gap",
                            "rule_id": "IDEM-002",
                            "severity": "critical",
                            "file": rel,
                            "line": pay_line_no,
                            "message": (
                                "MQ consumer calls Stripe/Twilio/SendGrid without an idempotency key — "
                                "duplicate messages cause duplicate charges or duplicate emails."
                            ),
                            "snippet": snippet,
                            "fix": (
                                "Pass an idempotency_key / IdempotencyKey derived from the message ID "
                                "to all payment and notification API calls."
                            ),
                        })

            # ── IDEM-003: batch jobs without checkpoint ───────────────────────
            for m in _BATCH_FUNC_RE.finditer(content):
                b_idx = content[:m.start()].count("\n")
                # Check next 60 lines for fetch-all + side-effect without checkpoint
                window60 = lines[b_idx: b_idx + 60]
                window60_text = "\n".join(window60)
                if not _NO_PAGINATION_RE.search(window60_text):
                    continue
                if _PAGINATION_CHECK_RE.search(window60_text):
                    continue
                if not _PAYMENT_NOTIF_RE.search(window60_text) and not _INSERT_RE.search(window60_text):
                    continue
                if _CHECKPOINT_RE.search(window60_text):
                    continue
                fn_line_no = b_idx + 1
                snippet = lines[fn_line_no - 1].strip()[:80] if fn_line_no <= len(lines) else m.group(0)
                issues.append({
                    "type": "idempotency_gap",
                    "rule_id": "IDEM-003",
                    "severity": "medium",
                    "file": rel,
                    "line": fn_line_no,
                    "message": (
                        "Batch job fetches all records without pagination and performs side effects "
                        "without checkpoint tracking — a crash mid-run will re-process already-handled items."
                    ),
                    "snippet": snippet,
                    "fix": (
                        "Track a last_processed cursor/offset and paginate (LIMIT/OFFSET) so the "
                        "job can resume safely after a failure."
                    ),
                })

            # ── IDEM-004: Bull/BullMQ queue.add without jobId (JS/TS only) ───
            if ext in {".js", ".ts", ".jsx", ".tsx"}:
                for m in _BULL_ADD_RE.finditer(content):
                    add_idx = content[:m.start()].count("\n")
                    window50 = lines[add_idx: add_idx + 50]
                    window50_text = "\n".join(window50)
                    if not _PAYMENT_NOTIF_RE.search(window50_text) and not re.search(
                        r"stripe|twilio|sendgrid|sendMail|sg\.send", window50_text, re.IGNORECASE
                    ):
                        continue
                    if _JOB_ID_RE.search(window50_text[:200]):
                        continue
                    add_line_no = add_idx + 1
                    snippet = lines[add_line_no - 1].strip()[:80] if add_line_no <= len(lines) else m.group(0)
                    issues.append({
                        "type": "idempotency_gap",
                        "rule_id": "IDEM-004",
                        "severity": "high",
                        "file": rel,
                        "line": add_line_no,
                        "message": (
                            "Bull/BullMQ queue.add() called without a jobId — duplicate jobs can be "
                            "enqueued, leading to duplicate charges or duplicate emails."
                        ),
                        "snippet": snippet,
                        "fix": (
                            "Pass a stable jobId derived from a business key: "
                            "queue.add(data, { jobId: `payment:${orderId}` })"
                        ),
                    })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# Phase 24 — Resource Leaks
# ---------------------------------------------------------------------------

# Go file-open patterns
_GO_FILE_OPEN_RE = re.compile(r"\bos\.(?:Open|OpenFile|Create)\s*\(")
_GO_DEFER_CLOSE_RE = re.compile(r"\bdefer\s+\w+\.Close\s*\(\s*\)")

# Go HTTP response patterns
_GO_HTTP_CALL_RE = re.compile(
    r"\b(?:resp|res|r|response)\s*,\s*(?:err|_)\s*:=\s*http\.(?:Get|Post|Head)\s*\("
)
_GO_RESP_BODY_CLOSE_RE = re.compile(r"\bdefer\s+\w+\.Body\.Close\s*\(\s*\)")

# Go SQL rows patterns
_GO_ROWS_QUERY_RE = re.compile(
    r"\b(?:rows|rs)\s*,\s*(?:err|_)\s*:=\s*\w+\.(?:Query|QueryContext)\s*\("
)
_GO_ROWS_CLOSE_RE = re.compile(r"\bdefer\s+rows\.Close\s*\(\s*\)")

# Python bare open
_PY_BARE_OPEN_RE = re.compile(
    r"^(?!.*\bwith\b)\s*(?:f|file|fp|fh|handle)\s*=\s*open\s*\(",
    re.MULTILINE,
)
_PY_WITH_OPEN_RE = re.compile(r"\bwith\s+open\s*\(")

# Python DB connection without context manager
_PY_DB_CONNECT_RE = re.compile(
    r"(?:conn|connection|db|cnx|cursor)\s*=\s*(?:psycopg2|pymysql|MySQLdb|sqlite3)\.connect\s*\("
)
_PY_CONN_CLOSE_RE = re.compile(r"conn\.close\s*\(\s*\)|connection\.close\s*\(\s*\)")
_PY_FINALLY_RE = re.compile(r"\bfinally\s*:")

# Python test file patterns
_PY_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_test)\.py$|/conftest\.py$")
# Go test file pattern
_GO_TEST_FILE_RE = re.compile(r"_test\.go$")


def find_resource_leaks(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect resource leaks: unclosed file handles, HTTP response bodies, and DB rows.

    Covers:
    - LEAK-001 (Go): os.Open/OpenFile/Create without defer Close()
    - LEAK-002 (Go): http.Get/Post/Head resp without defer Body.Close()
    - LEAK-003 (Go): rows from .Query/.QueryContext without defer rows.Close()
    - LEAK-004 (Python): bare open() not in a with block and no explicit .close()
    - LEAK-005 (Python): psycopg2/pymysql connect() without conn.close() or finally

    Returns
    -------
    list[dict] with type="resource_leak".
    """
    issues: list[dict] = []
    root = Path(project_root)
    _GO_EXTS = {".go"}
    _PY_EXTS = {".py"}

    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()
            rel = str(fpath.relative_to(root))

            # ── Go ──────────────────────────────────────────────────────────
            if ext in _GO_EXTS:
                if _GO_TEST_FILE_RE.search(rel):
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                lines = content.splitlines()

                # LEAK-001: os.Open / os.OpenFile / os.Create without defer Close
                for m in _GO_FILE_OPEN_RE.finditer(content):
                    open_idx = content[:m.start()].count("\n")
                    window = "\n".join(lines[open_idx: open_idx + 20])
                    if not _GO_DEFER_CLOSE_RE.search(window):
                        line_no = open_idx + 1
                        snippet = lines[open_idx].strip()[:80]
                        issues.append({
                            "type": "resource_leak",
                            "rule_id": "LEAK-001",
                            "severity": "high",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                f"Go file opened with {m.group(0).split('(')[0].strip()} "
                                "but no 'defer <var>.Close()' found within 20 lines — file descriptor leak."
                            ),
                            "snippet": snippet,
                            "fix": "Add 'defer f.Close()' immediately after the error check.",
                        })

                # LEAK-002: http.Get/Post/Head without defer resp.Body.Close()
                for m in _GO_HTTP_CALL_RE.finditer(content):
                    http_idx = content[:m.start()].count("\n")
                    window = "\n".join(lines[http_idx: http_idx + 20])
                    if not _GO_RESP_BODY_CLOSE_RE.search(window):
                        line_no = http_idx + 1
                        snippet = lines[http_idx].strip()[:80]
                        issues.append({
                            "type": "resource_leak",
                            "rule_id": "LEAK-002",
                            "severity": "high",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "Go HTTP response body not closed — "
                                "missing 'defer resp.Body.Close()' causes connection pool exhaustion."
                            ),
                            "snippet": snippet,
                            "fix": "Add 'defer resp.Body.Close()' immediately after checking the error.",
                        })

                # LEAK-003: rows from .Query/.QueryContext without defer rows.Close()
                for m in _GO_ROWS_QUERY_RE.finditer(content):
                    rows_idx = content[:m.start()].count("\n")
                    window = "\n".join(lines[rows_idx: rows_idx + 10])
                    if not _GO_ROWS_CLOSE_RE.search(window):
                        line_no = rows_idx + 1
                        snippet = lines[rows_idx].strip()[:80]
                        issues.append({
                            "type": "resource_leak",
                            "rule_id": "LEAK-003",
                            "severity": "high",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "Go sql.Rows not closed — missing 'defer rows.Close()' "
                                "holds the DB connection open until GC."
                            ),
                            "snippet": snippet,
                            "fix": "Add 'defer rows.Close()' immediately after the nil error check.",
                        })

            # ── Python ──────────────────────────────────────────────────────
            elif ext in _PY_EXTS:
                if _PY_TEST_FILE_RE.search(rel):
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                lines = content.splitlines()

                # LEAK-004: bare open() not wrapped in 'with'
                for m in _PY_BARE_OPEN_RE.finditer(content):
                    open_idx = content[:m.start()].count("\n")
                    # Extract variable name
                    var_match = re.match(r"\s*(\w+)\s*=\s*open\s*\(", lines[open_idx] if open_idx < len(lines) else "")
                    var_name = var_match.group(1) if var_match else "f"
                    window = "\n".join(lines[open_idx: open_idx + 20])
                    close_re = re.compile(rf"\b{re.escape(var_name)}\.close\s*\(\s*\)")
                    if not close_re.search(window):
                        line_no = open_idx + 1
                        snippet = lines[open_idx].strip()[:80] if open_idx < len(lines) else m.group(0)
                        issues.append({
                            "type": "resource_leak",
                            "rule_id": "LEAK-004",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                f"Python file opened with bare open() (not 'with open(...)') "
                                f"and no '{var_name}.close()' found — file handle may leak."
                            ),
                            "snippet": snippet,
                            "fix": "Use 'with open(...) as f:' to ensure the file is always closed.",
                        })

                # LEAK-005: psycopg2/pymysql connect without close/finally
                for m in _PY_DB_CONNECT_RE.finditer(content):
                    conn_idx = content[:m.start()].count("\n")
                    window = "\n".join(lines[conn_idx: conn_idx + 30])
                    has_close = bool(_PY_CONN_CLOSE_RE.search(window))
                    has_finally = bool(_PY_FINALLY_RE.search(window))
                    # Also accept 'with psycopg2.connect' pattern on the same line
                    if "with " in (lines[conn_idx] if conn_idx < len(lines) else ""):
                        continue
                    if not has_close and not has_finally:
                        line_no = conn_idx + 1
                        snippet = lines[conn_idx].strip()[:80] if conn_idx < len(lines) else m.group(0)
                        issues.append({
                            "type": "resource_leak",
                            "rule_id": "LEAK-005",
                            "severity": "high",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "DB connection opened without conn.close() or a finally block — "
                                "connection pool exhaustion under load."
                            ),
                            "snippet": snippet,
                            "fix": (
                                "Use 'with psycopg2.connect(...) as conn:' or ensure "
                                "conn.close() is called in a finally block."
                            ),
                        })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# Phase 25 — Migration Safety
# ---------------------------------------------------------------------------

# Migration file detection
_MIGRATION_PATH_RE = re.compile(
    r"(?:migration|migrate|schema|db/)|(?:_migration\.|_schema\.|V\d+__)",
    re.IGNORECASE,
)
_MIGRATION_FNAME_RE = re.compile(
    r"(?:_migration|_schema)\.[a-z]+$|^V\d+.*\.sql$|^[0-9]+_.*\.(?:sql|rb|py)$",
    re.IGNORECASE,
)

# ALTER TABLE detection
_ALTER_TABLE_RE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
_ALTER_SAFE_RE = re.compile(
    r"ALGORITHM\s*=|LOCK\s*=|--\s*safe|--\s*online",
    re.IGNORECASE,
)
# Exclude patterns: RENAME TO and ADD INDEX (safe in MySQL 8+)
_ALTER_RENAME_RE = re.compile(r"ALTER\s+TABLE\s+\w+\s+RENAME\s+TO\b", re.IGNORECASE)
_ALTER_ADD_INDEX_RE = re.compile(r"ALTER\s+TABLE\s+\w+\s+ADD\s+INDEX\b", re.IGNORECASE)

# ADD COLUMN with non-null DEFAULT on large tables
_ADD_COL_DEFAULT_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+\w+[^;]+DEFAULT\s+(?!NULL\b)(\S+)",
    re.IGNORECASE,
)
_LARGE_TABLE_RE = re.compile(
    r"(?:events|logs|audit|history|usage|analytics|metrics|sessions|transactions|orders|payments)",
    re.IGNORECASE,
)

# lock_wait_timeout before ALTER
_LOCK_TIMEOUT_RE = re.compile(
    r"SET\s+(?:lock_wait_timeout|innodb_lock_wait_timeout|statement_timeout)\s*=",
    re.IGNORECASE,
)

# TRUNCATE / DROP TABLE in application code
_TRUNCATE_DROP_APP_RE = re.compile(
    r'db\.Exec\s*\(\s*["\']TRUNCATE|db\.Exec\s*\(\s*["\']DROP\s+TABLE|'
    r'cursor\.execute\s*\(\s*["\']TRUNCATE|cursor\.execute\s*\(\s*["\']DROP\s+TABLE|'
    r'\.query\s*\(\s*["\']TRUNCATE|\.query\s*\(\s*["\']DROP\s+TABLE',
    re.IGNORECASE,
)

# Application code extensions (non-migration)
_APP_CODE_EXTS = {".go", ".py", ".js", ".ts", ".java", ".rb", ".php"}
# Migration-scannable extensions
_MIGR_EXTS = {".sql", ".py", ".rb", ".java", ".go", ".ts", ".js"}


def _is_migration_file(rel: str) -> bool:
    """Return True if the file path looks like a migration file."""
    return bool(_MIGRATION_PATH_RE.search(rel)) or bool(_MIGRATION_FNAME_RE.search(rel))


def find_unsafe_migrations(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect unsafe database migration patterns.

    Covers:
    - MIGR-001: ALTER TABLE without ALGORITHM= or LOCK= (MySQL safety)
    - MIGR-002: ALTER TABLE ADD COLUMN with non-NULL DEFAULT on large tables
    - MIGR-003: ALTER TABLE not preceded by lock_wait_timeout setting
    - MIGR-004: TRUNCATE or DROP TABLE in application (non-migration) code

    Returns
    -------
    list[dict] with type="unsafe_migration".
    """
    issues: list[dict] = []
    root = Path(project_root)

    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()
            rel = str(fpath.relative_to(root))
            is_migration = _is_migration_file(rel)

            # ── MIGR-004: TRUNCATE/DROP TABLE in application code ─────────────
            if not is_migration and ext in _APP_CODE_EXTS:
                if _TEST_PAT.search(rel):
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for m in _TRUNCATE_DROP_APP_RE.finditer(content):
                    line_no = content[:m.start()].count("\n") + 1
                    lines = content.splitlines()
                    snippet = lines[line_no - 1].strip()[:80] if line_no <= len(lines) else m.group(0)
                    issues.append({
                        "type": "unsafe_migration",
                        "rule_id": "MIGR-004",
                        "severity": "high",
                        "file": rel,
                        "line": line_no,
                        "message": (
                            "TRUNCATE or DROP TABLE found in application code (non-migration file) — "
                            "this can silently destroy production data."
                        ),
                        "snippet": snippet,
                        "fix": (
                            "Move destructive DDL to a proper migration file with a rollback plan. "
                            "Never call TRUNCATE/DROP from application request handlers."
                        ),
                    })
                continue  # done with non-migration files for this path

            # ── Migration files ──────────────────────────────────────────────
            if not is_migration:
                continue
            if ext not in _MIGR_EXTS:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines = content.splitlines()

            # Skip SQLite files for MIGR-001/003
            is_sqlite = "sqlite" in rel.lower()

            for i, line in enumerate(lines):
                uline = line.upper()
                if "ALTER" not in uline and "TABLE" not in uline:
                    continue
                if not _ALTER_TABLE_RE.search(line):
                    continue

                # Exclude RENAME TO and ADD INDEX (generally safe)
                if _ALTER_RENAME_RE.search(line):
                    continue
                if _ALTER_ADD_INDEX_RE.search(line):
                    continue

                line_no = i + 1

                # MIGR-001: missing ALGORITHM= / LOCK= (MySQL, non-SQLite)
                if not is_sqlite:
                    # Check same line plus next 2 lines
                    context_lines = lines[i: i + 3]
                    context_text = "\n".join(context_lines)
                    if not _ALTER_SAFE_RE.search(context_text):
                        snippet = line.strip()[:80]
                        issues.append({
                            "type": "unsafe_migration",
                            "rule_id": "MIGR-001",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "ALTER TABLE without ALGORITHM= or LOCK= hint — "
                                "may take a full table lock on MySQL, blocking writes."
                            ),
                            "snippet": snippet,
                            "fix": (
                                "Add ALGORITHM=INPLACE, LOCK=NONE (or LOCK=SHARED) where possible, "
                                "or add a '-- safe' comment to suppress this warning."
                            ),
                        })

                # MIGR-002: ADD COLUMN with non-NULL DEFAULT on large table
                add_col_m = _ADD_COL_DEFAULT_RE.search(line)
                if add_col_m:
                    table_name = add_col_m.group(1)
                    if _LARGE_TABLE_RE.search(table_name):
                        snippet = line.strip()[:80]
                        issues.append({
                            "type": "unsafe_migration",
                            "rule_id": "MIGR-002",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                f"ALTER TABLE {table_name} ADD COLUMN with a non-NULL DEFAULT on a "
                                "potentially large table — triggers a full table rewrite on older MySQL."
                            ),
                            "snippet": snippet,
                            "fix": (
                                "Use a multi-step migration: (1) ADD COLUMN NULL, (2) backfill in batches, "
                                "(3) ADD NOT NULL constraint. Or use pt-online-schema-change / gh-ost."
                            ),
                        })

                # MIGR-003: no lock_wait_timeout before ALTER (SQL files only)
                if ext == ".sql" and not is_sqlite:
                    pre_text = "\n".join(lines[max(0, i - 10): i])
                    if not _LOCK_TIMEOUT_RE.search(pre_text):
                        snippet = line.strip()[:80]
                        issues.append({
                            "type": "unsafe_migration",
                            "rule_id": "MIGR-003",
                            "severity": "low",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "ALTER TABLE not preceded by SET lock_wait_timeout / "
                                "SET statement_timeout within 10 lines — "
                                "long-running schema changes can hold locks indefinitely."
                            ),
                            "snippet": snippet,
                            "fix": (
                                "Prepend: SET lock_wait_timeout = 5; "
                                "SET innodb_lock_wait_timeout = 5; before the ALTER TABLE."
                            ),
                        })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# Phase 26 — Connection Pool Misconfiguration
# ---------------------------------------------------------------------------

# POOL-001: Go — sql.Open without SetMaxOpenConns
_GO_SQL_OPEN_RE        = re.compile(r"\bsql\.Open\s*\(")
_GO_CONN_LIFETIME_RE   = re.compile(r"\bSetConnMaxLifetime\s*\(")
_GO_MAX_OPEN_RE        = re.compile(r"\bSetMaxOpenConns\s*\(")
_GO_SQLX_OPEN_RE       = re.compile(r"\bsqlx\.Open\s*\(")
_GO_DB_DOT_RE          = re.compile(r"\bdb\.")

# POOL-002: Python SQLAlchemy — create_engine without pool_size
_PY_CREATE_ENGINE_RE   = re.compile(r"\bcreate_engine\s*\(")
_PY_POOL_SIZE_RE       = re.compile(r"\bpool_size\s*=")
_PY_POOL_CLASS_RE      = re.compile(r"\bpool_class\s*=")
_PY_NULL_POOL_RE       = re.compile(r"\bNullPool\b")
_PY_STATIC_POOL_RE     = re.compile(r"\bStaticPool\b")
_PY_POOL_SIZE_ZERO_RE  = re.compile(r"\bpool_size\s*=\s*0\b")

# POOL-003: Node.js pg Pool / mysql2 createPool without max / connectionLimit
_JS_PG_POOL_RE         = re.compile(r"\bnew\s+Pool\s*\(\s*\{")
_JS_PG_MAX_RE          = re.compile(r"\bmax\s*:")
_JS_MYSQL_POOL_RE      = re.compile(r"\bcreatePool\s*\(\s*\{")
_JS_CONN_LIMIT_RE      = re.compile(r"\bconnectionLimit\s*:")

# POOL-004: Go — hardcoded connection string (not os.Getenv)
_GO_HARDCODED_DSN_RE   = re.compile(
    r'sql\.Open\s*\(\s*["\'](?:postgres|mysql|sqlite3|pgx)["\'],\s*["\'](?:host=|[^"\']*://)',
)
_GO_GETENV_RE          = re.compile(r"\bos\.Getenv\s*\(")

# POOL-005: Python psycopg2 pool with maxconn=1
_PY_PSYCOPG2_POOL_RE   = re.compile(
    r"psycopg2\.pool\.(?:Simple|Threaded)ConnectionPool\s*\("
)


def find_connection_pool_misconfigs(graph: dict, project_root: str) -> list[dict]:  # noqa: ARG001
    """Detect database connection pool configurations that will cause exhaustion under load.

    Rule IDs: POOL-001 through POOL-005.

    Returns
    -------
    list[dict] with type="connection_pool_misconfig".
    """
    issues: list[dict] = []
    root = Path(project_root)

    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext   = fpath.suffix.lower()
            rel   = str(fpath.relative_to(root))

            # ── Go checks (POOL-001, POOL-004) ────────────────────────────────
            if ext == ".go":
                # Skip test files
                if fname.endswith("_test.go"):
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                # POOL-001: sql.Open or SetConnMaxLifetime present, but no SetMaxOpenConns
                has_sql_open      = bool(_GO_SQL_OPEN_RE.search(content))
                has_conn_lifetime = bool(_GO_CONN_LIFETIME_RE.search(content))
                has_max_open      = bool(_GO_MAX_OPEN_RE.search(content))
                has_sqlx          = bool(_GO_SQLX_OPEN_RE.search(content))
                has_db_calls      = bool(_GO_DB_DOT_RE.search(content))

                if (has_sql_open or has_conn_lifetime) and not has_max_open:
                    # Exclude: sqlx.Open without any db. calls after
                    if has_sqlx and not has_db_calls:
                        pass
                    else:
                        # Find the line of sql.Open or SetConnMaxLifetime
                        trigger = _GO_SQL_OPEN_RE.search(content) or _GO_CONN_LIFETIME_RE.search(content)
                        line_no = content[:trigger.start()].count("\n") + 1 if trigger else 1
                        lines   = content.splitlines()
                        snippet = lines[line_no - 1].strip()[:80] if line_no <= len(lines) else ""
                        issues.append({
                            "type": "connection_pool_misconfig",
                            "rule_id": "POOL-001",
                            "severity": "high",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "database/sql opened without SetMaxOpenConns "
                                "— pool size defaults to unlimited"
                            ),
                            "snippet": snippet,
                            "fix": "Call db.SetMaxOpenConns(N) after sql.Open to cap the pool size.",
                        })

                # POOL-004: hardcoded connection string (not os.Getenv)
                for m in _GO_HARDCODED_DSN_RE.finditer(content):
                    # Check that the connection string arg is not os.Getenv(...)
                    # The match itself captures the raw string literal — that's proof enough
                    line_no = content[:m.start()].count("\n") + 1
                    lines   = content.splitlines()
                    snippet = lines[line_no - 1].strip()[:80] if line_no <= len(lines) else m.group(0)
                    issues.append({
                        "type": "connection_pool_misconfig",
                        "rule_id": "POOL-004",
                        "severity": "medium",
                        "file": rel,
                        "line": line_no,
                        "message": "Hardcoded database connection string (use env var)",
                        "snippet": snippet,
                        "fix": "Replace the inline DSN with os.Getenv(\"DATABASE_URL\").",
                    })

            # ── Python checks (POOL-002, POOL-005) ───────────────────────────
            elif ext == ".py":
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                lines_list = content.splitlines()

                # POOL-002: create_engine without pool_size (and not NullPool/StaticPool)
                for m in _PY_CREATE_ENGINE_RE.finditer(content):
                    line_no = content[:m.start()].count("\n") + 1
                    # Look at the same line and the next 5 lines for pool_size / pool_class
                    end_line = min(len(lines_list), line_no + 5)
                    window   = "\n".join(lines_list[line_no - 1: end_line])

                    if _PY_NULL_POOL_RE.search(window):
                        continue
                    if _PY_STATIC_POOL_RE.search(window):
                        continue
                    if _PY_POOL_CLASS_RE.search(window):
                        continue

                    # pool_size=0 → flag (zero means unlimited in some drivers)
                    if _PY_POOL_SIZE_ZERO_RE.search(window):
                        snippet = lines_list[line_no - 1].strip()[:80]
                        issues.append({
                            "type": "connection_pool_misconfig",
                            "rule_id": "POOL-002",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "SQLAlchemy create_engine without pool_size "
                                "— defaults to 5 connections"
                            ),
                            "snippet": snippet,
                            "fix": "Set pool_size= to a positive integer in create_engine().",
                        })
                        continue

                    if not _PY_POOL_SIZE_RE.search(window):
                        snippet = lines_list[line_no - 1].strip()[:80]
                        issues.append({
                            "type": "connection_pool_misconfig",
                            "rule_id": "POOL-002",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "SQLAlchemy create_engine without pool_size "
                                "— defaults to 5 connections"
                            ),
                            "snippet": snippet,
                            "fix": "Add pool_size=N (e.g. pool_size=10) to create_engine().",
                        })

                # POOL-005: psycopg2.pool.SimpleConnectionPool/ThreadedConnectionPool with maxconn=1
                for m in _PY_PSYCOPG2_POOL_RE.finditer(content):
                    line_no = content[:m.start()].count("\n") + 1
                    snippet = lines_list[line_no - 1].strip()[:80] if line_no <= len(lines_list) else m.group(0)
                    # Extract the args: SimpleConnectionPool(minconn, maxconn, ...)
                    # Look for pattern like (1, 1, or (1,1,
                    call_end = content.find(")", m.end())
                    call_text = content[m.end(): call_end + 1] if call_end > m.end() else ""
                    arg_m = re.match(r"\s*(\d+)\s*,\s*(\d+)", call_text)
                    if arg_m:
                        minconn = int(arg_m.group(1))
                        maxconn = int(arg_m.group(2))
                        if maxconn <= 1:
                            issues.append({
                                "type": "connection_pool_misconfig",
                                "rule_id": "POOL-005",
                                "severity": "low",
                                "file": rel,
                                "line": line_no,
                                "message": (
                                    "Connection pool maxconn=1 won't scale under concurrent requests"
                                ),
                                "snippet": snippet,
                                "fix": "Increase maxconn to match expected concurrent DB connections (e.g. 10-20).",
                            })

            # ── JS/TS checks (POOL-003) ────────────────────────────────────────
            elif ext in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}:
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                lines_list = content.splitlines()

                # new Pool({ without max: within next 10 lines
                for m in _JS_PG_POOL_RE.finditer(content):
                    line_no = content[:m.start()].count("\n") + 1
                    end_line = min(len(lines_list), line_no + 10)
                    window   = "\n".join(lines_list[line_no - 1: end_line])
                    if not _JS_PG_MAX_RE.search(window):
                        snippet = lines_list[line_no - 1].strip()[:80]
                        issues.append({
                            "type": "connection_pool_misconfig",
                            "rule_id": "POOL-003",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "pg Pool created without max — defaults to 10 connections"
                            ),
                            "snippet": snippet,
                            "fix": "Add max: N to the Pool config object (e.g. max: 20).",
                        })

                # createPool({ without connectionLimit: within next 10 lines
                for m in _JS_MYSQL_POOL_RE.finditer(content):
                    line_no = content[:m.start()].count("\n") + 1
                    end_line = min(len(lines_list), line_no + 10)
                    window   = "\n".join(lines_list[line_no - 1: end_line])
                    if not _JS_CONN_LIMIT_RE.search(window):
                        snippet = lines_list[line_no - 1].strip()[:80]
                        issues.append({
                            "type": "connection_pool_misconfig",
                            "rule_id": "POOL-003",
                            "severity": "medium",
                            "file": rel,
                            "line": line_no,
                            "message": (
                                "pg Pool created without max — defaults to 10 connections"
                            ),
                            "snippet": snippet,
                            "fix": "Add connectionLimit: N to the createPool config object.",
                        })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# Phase 27 — Missing Health Checks
# ---------------------------------------------------------------------------

# HTTP framework detection patterns
_HTTP_FRAMEWORK_RE = re.compile(
    r"\b(?:express|fastapi|gin|echo|actix|flask|django|fiber|spring)\b",
    re.IGNORECASE,
)

# Health check route string literals
_HEALTH_ROUTE_STRINGS = re.compile(
    r"""['"/](?:_health|health(?:z)?|ping|ready|live|status|api/health)['"/ ]""",
    re.IGNORECASE,
)

# Kubernetes Deployment kind
_K8S_DEPLOYMENT_RE   = re.compile(r"^kind\s*:\s*Deployment\b", re.MULTILINE)
# Container definition marker in K8s spec
_K8S_CONTAINERS_RE   = re.compile(r"^\s{0,12}containers\s*:", re.MULTILINE)
# Probe presence
_K8S_READINESS_RE    = re.compile(r"\breadinessProbe\s*:")
_K8S_LIVENESS_RE     = re.compile(r"\blivenessProbe\s*:")

_HTTP_FW_EXTS = {".py", ".go", ".js", ".ts", ".java", ".rs"}


def find_missing_health_checks(graph: dict, project_root: str) -> list[dict]:
    """Detect missing health check endpoints and missing K8s readiness/liveness probes.

    Rule IDs: HLTH-001, HLTH-002.

    Returns
    -------
    list[dict] with type="missing_health_check".
    """
    issues: list[dict] = []
    root = Path(project_root)

    # ── HLTH-001: project-level check ─────────────────────────────────────────
    has_http_framework = False
    has_health_route   = False

    # Check graph nodes for route_path attributes first (fast path)
    for node in graph.get("nodes", []):
        route = node.get("route_path", "") or node.get("path", "")
        if route and _HEALTH_ROUTE_STRINGS.search(f'"{route}"'):
            has_health_route = True
            break

    for dirpath, _dirs, filenames in _walk_project(project_root):
        if has_health_route:
            break
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext   = fpath.suffix.lower()
            if ext not in _HTTP_FW_EXTS:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if not has_http_framework and _HTTP_FRAMEWORK_RE.search(content):
                has_http_framework = True
            if _HEALTH_ROUTE_STRINGS.search(content):
                has_health_route = True
                break

    if has_http_framework and not has_health_route:
        issues.append({
            "type": "missing_health_check",
            "rule_id": "HLTH-001",
            "severity": "medium",
            "file": "project-level",
            "line": 0,
            "message": (
                "No health check endpoint found (/health, /healthz, /ping)"
            ),
            "fix": (
                "Add a GET /health (or /healthz, /ping) endpoint that returns HTTP 200 "
                "so load balancers and orchestrators can verify the service is alive."
            ),
        })

    # ── HLTH-002: K8s Deployment without readinessProbe / livenessProbe ───────
    for dirpath, _dirs, filenames in _walk_project(project_root):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext   = fpath.suffix.lower()
            if ext not in {".yaml", ".yml"}:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            if not _K8S_DEPLOYMENT_RE.search(content):
                continue

            rel = str(fpath.relative_to(root))

            # Find the containers: section and check 50 lines after it
            containers_m = _K8S_CONTAINERS_RE.search(content)
            if containers_m:
                container_start = containers_m.start()
                # Scan up to 50 lines after containers: for probes
                lines_list = content.splitlines()
                container_line = content[:container_start].count("\n")
                end_line = min(len(lines_list), container_line + 50)
                window   = "\n".join(lines_list[container_line: end_line])
            else:
                window = content

            if not _K8S_READINESS_RE.search(window):
                line_no = content[:containers_m.start()].count("\n") + 1 if containers_m else 1
                issues.append({
                    "type": "missing_health_check",
                    "rule_id": "HLTH-002",
                    "severity": "medium",
                    "file": rel,
                    "line": line_no,
                    "message": (
                        "Kubernetes Deployment without readinessProbe "
                        "— pod receives traffic before ready"
                    ),
                    "fix": (
                        "Add a readinessProbe (httpGet /health or exec) to the container spec "
                        "so Kubernetes withholds traffic until the pod passes the probe."
                    ),
                })

            if not _K8S_LIVENESS_RE.search(window):
                line_no = content[:containers_m.start()].count("\n") + 1 if containers_m else 1
                issues.append({
                    "type": "missing_health_check",
                    "rule_id": "HLTH-002",
                    "severity": "medium",
                    "file": rel,
                    "line": line_no,
                    "message": (
                        "Kubernetes Deployment without livenessProbe "
                        "— stuck pods are never restarted automatically"
                    ),
                    "fix": (
                        "Add a livenessProbe (httpGet /health or exec) to the container spec "
                        "so Kubernetes can restart pods that become unresponsive."
                    ),
                })

    return _sort_issues(issues)


# ---------------------------------------------------------------------------
# Aggregator + health summary
# ---------------------------------------------------------------------------

def run_all_checks(graph: dict, project_root: str) -> dict:
    """Run all structural bug checks and return a combined report.

    Parameters
    ----------
    graph:
        Already-loaded info_graph.json content (dict).
    project_root:
        Absolute path to the project root on disk.

    Returns
    -------
    dict with keys: ok, total_issues, by_severity, issues, checked_at.
    """
    checks = [
        ("bean_collisions",        lambda: find_bean_collisions(graph)),
        ("missing_config_siblings",lambda: find_missing_config_siblings(graph, project_root)),
        ("duplicate_config_keys",  lambda: find_duplicate_config_keys(project_root)),
        ("kafka_group_collisions", lambda: find_kafka_group_collisions(graph, project_root)),
        ("unhandled_errors",       lambda: find_unhandled_errors(graph, project_root)),
        ("import_cycles",          lambda: find_import_cycles_detailed(graph)),
        ("dead_event_handlers",    lambda: find_dead_event_handlers(graph)),
        ("missing_pagination",     lambda: find_missing_pagination(graph, project_root)),
        ("missing_indexes",        lambda: find_missing_indexes(graph, project_root)),
        ("n_plus_one_risk",        lambda: find_n_plus_one_risk(graph, project_root)),
        ("unused_env_vars",        lambda: find_unused_env_vars(graph, project_root)),
        ("missing_env_vars",       lambda: find_missing_env_vars(graph, project_root)),
        ("port_conflicts",         lambda: find_port_conflicts(graph, project_root)),
        ("race_conditions",        lambda: find_race_conditions(graph, project_root)),
        ("idempotency_gaps",       lambda: find_idempotency_gaps(graph, project_root)),
        ("resource_leaks",         lambda: find_resource_leaks(graph, project_root)),
        ("unsafe_migrations",      lambda: find_unsafe_migrations(graph, project_root)),
        ("connection_pool_misconfigs", lambda: find_connection_pool_misconfigs(graph, project_root)),
        ("missing_health_checks",  lambda: find_missing_health_checks(graph, project_root)),
    ]

    all_issues: list[dict] = []
    for _name, fn in checks:
        try:
            results = fn()
            all_issues.extend(results)
        except Exception as exc:
            # A failing check must not crash the whole report
            all_issues.append(
                {
                    "type": "check_error",
                    "severity": "low",
                    "check": _name,
                    "files": [],
                    "lines": [],
                    "description": f"Check '{_name}' raised an exception: {exc}",
                    "fix": "Investigate the structural_bugs.py check for this type.",
                }
            )

    all_issues = _sort_issues(all_issues)

    by_severity: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in all_issues:
        sev = issue.get("severity", "low")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    return {
        "ok": len(all_issues) == 0,
        "total_issues": len(all_issues),
        "by_severity": by_severity,
        "issues": all_issues,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def get_health_summary(graph: dict, project_root: str) -> dict:
    """Quick summary suitable for the graph_system_health() MCP tool.

    Computes a 0-100 score and letter grade based on the structural bug report.

    Parameters
    ----------
    graph:
        Already-loaded info_graph.json content (dict).
    project_root:
        Absolute path to the project root on disk.

    Returns
    -------
    dict with keys: ok, score, grade, critical_issues, total_issues,
    checks_passed, checks_failed.
    """
    report = run_all_checks(graph, project_root)

    bys = report["by_severity"]
    n_critical = bys.get("critical", 0)
    n_high     = bys.get("high", 0)
    n_medium   = bys.get("medium", 0)
    n_low      = bys.get("low", 0)

    # Penalty model:  critical=-20, high=-10, medium=-4, low=-1  (floor 0)
    penalty = (n_critical * 20) + (n_high * 10) + (n_medium * 4) + (n_low * 1)

    # Additional per-finding penalties for new checks (each capped)
    n_plus_one_findings   = len([i for i in report["issues"] if i.get("type") == "n_plus_one_risk"])
    missing_pag_findings  = len([i for i in report["issues"] if i.get("type") == "missing_pagination"])
    missing_idx_findings  = len([i for i in report["issues"] if i.get("type") == "missing_index"])

    penalty += min(20, n_plus_one_findings  * 5)
    penalty += min(20, missing_pag_findings * 3)
    penalty += min(20, missing_idx_findings * 2)

    # Checks 11-14 penalties
    unused_env_findings   = len([i for i in report["issues"] if i.get("type") == "unused_env_var"])
    missing_env_findings  = len([i for i in report["issues"] if i.get("type") == "missing_env_var"])
    port_conflict_findings = len([i for i in report["issues"] if i.get("type") == "port_conflict"])
    race_cond_findings    = len([i for i in report["issues"] if i.get("type") == "race_condition_risk"])

    penalty += min(5,  unused_env_findings   * 1)   # low severity, cap -5
    penalty += min(20, missing_env_findings  * 4)   # high severity, cap -20
    penalty += min(15, port_conflict_findings * 5)  # high severity, cap -15
    penalty += min(20, race_cond_findings    * 5)   # high severity, cap -20

    # Checks 15-17 penalties
    idem_findings      = [i for i in report["issues"] if i.get("type") == "idempotency_gap"]
    idem_critical      = [i for i in idem_findings if i.get("severity") == "critical"]
    idem_non_critical  = [i for i in idem_findings if i.get("severity") != "critical"]
    leak_findings      = len([i for i in report["issues"] if i.get("type") == "resource_leak"])
    migr_findings      = len([i for i in report["issues"] if i.get("type") == "unsafe_migration"])

    penalty += min(15, len(idem_non_critical) * 3)  # -3 each, cap -15
    penalty += min(24, len(idem_critical) * 8)       # -8 each (critical), cap -24
    penalty += min(10, leak_findings * 2)             # -2 each, cap -10
    penalty += min(12, migr_findings * 3)             # -3 each, cap -12

    # Checks 18-19 penalties
    pool_findings   = len([i for i in report["issues"] if i.get("type") == "connection_pool_misconfig"])
    health_findings = len([i for i in report["issues"] if i.get("type") == "missing_health_check"])

    penalty += min(12, pool_findings   * 3)   # -3 each, cap -12
    penalty += min(8,  health_findings * 4)   # -4 each, cap -8

    score   = max(0, 100 - penalty)

    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    # Total checks = 19 functional checks
    total_checks  = 19
    checks_failed = min(
        total_checks,
        (1 if n_critical else 0) + (1 if n_high else 0) +
        (1 if n_medium else 0) + (1 if n_low else 0),
    )
    checks_passed = total_checks - checks_failed

    critical_issues: list[str] = [
        issue.get("description") or issue.get("snippet", "")
        for issue in report["issues"]
        if issue.get("severity") in {"critical", "high"}
    ][:3]

    return {
        "ok":             score >= 75,
        "score":          score,
        "grade":          grade,
        "critical_issues": critical_issues,
        "total_issues":   report["total_issues"],
        "checks_passed":  checks_passed,
        "checks_failed":  checks_failed,
    }


# ---------------------------------------------------------------------------
# MCP tool exports (registration happens in mcp_tools_integration.py)
# ---------------------------------------------------------------------------

def graph_unused_env_vars_tool(project_root: str) -> dict:
    findings = find_unused_env_vars({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_missing_env_vars_tool(project_root: str) -> dict:
    findings = find_missing_env_vars({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_port_conflicts_tool(project_root: str) -> dict:
    findings = find_port_conflicts({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_race_conditions_tool(project_root: str) -> dict:
    findings = find_race_conditions({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_idempotency_gaps_tool(project_root: str) -> dict:
    findings = find_idempotency_gaps({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_resource_leaks_tool(project_root: str) -> dict:
    findings = find_resource_leaks({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_unsafe_migrations_tool(project_root: str) -> dict:
    findings = find_unsafe_migrations({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_pool_misconfigs_tool(project_root: str) -> dict:
    findings = find_connection_pool_misconfigs({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


def graph_health_checks_tool(project_root: str) -> dict:
    findings = find_missing_health_checks({}, project_root)
    return {"ok": True, "total": len(findings), "findings": findings}


# ---------------------------------------------------------------------------
# Test suite for new structural checks
# ---------------------------------------------------------------------------

def _test_new_structural():
    import tempfile, pathlib, os

    tmpdir = tempfile.mkdtemp()

    # Setup: .env file with some vars
    (pathlib.Path(tmpdir) / ".env").write_text(
        "DB_PASSWORD=secret123\nSTRIPE_KEY=sk_live_xxx\nNODE_ENV=production\nUNUSED_VAR=unused\n"
    )
    # Setup: source file using some vars
    (pathlib.Path(tmpdir) / "app.js").write_text(
        "const db = process.env.DB_PASSWORD\nconst stripe = process.env.STRIPE_KEY\nconst missing = process.env.SENDGRID_API_KEY\n"
    )

    # Test unused env vars
    unused = find_unused_env_vars({}, tmpdir)
    assert any(f['var_name'] == 'UNUSED_VAR' for f in unused), f"Unused: {unused}"
    print("[PASS] find_unused_env_vars")

    # Test missing env vars
    missing = find_missing_env_vars({}, tmpdir)
    assert any(f['var_name'] == 'SENDGRID_API_KEY' for f in missing), f"Missing: {missing}"
    print("[PASS] find_missing_env_vars")

    # Test port conflicts
    (pathlib.Path(tmpdir) / "service-a").mkdir()
    (pathlib.Path(tmpdir) / "service-a" / ".env").write_text("PORT=8080\n")
    (pathlib.Path(tmpdir) / "service-b").mkdir()
    (pathlib.Path(tmpdir) / "service-b" / ".env").write_text("PORT=8080\n")
    conflicts = find_port_conflicts({}, tmpdir)
    assert any(f['port'] == 8080 for f in conflicts), f"Port: {conflicts}"
    print("[PASS] find_port_conflicts")

    # Test race conditions (Go)
    (pathlib.Path(tmpdir) / "server.go").write_text(
        'package main\nimport "fmt"\nvar counter int\nfunc main() {\n    for i := 0; i < 10; i++ {\n        go func() { counter++ }()\n    }\n}\n'
    )
    races = find_race_conditions({}, tmpdir)
    assert any(f['language'] == 'go' for f in races), f"Race Go: {races}"
    print("[PASS] find_race_conditions (Go)")

    print("\n=== All new structural tests PASSED ===")


def _test_new_structural_2():
    import tempfile, pathlib

    tmpdir = tempfile.mkdtemp()
    root = pathlib.Path(tmpdir)

    # ── Idempotency: Go Kafka consumer with INSERT sans ON CONFLICT → flagged ──
    (root / "consumer_bad.go").write_text(
        'package main\n'
        'import "github.com/confluentinc/confluent-kafka-go/kafka"\n'
        '\n'
        'func handleMessage(msg *kafka.Message) {\n'
        '    db.Exec("INSERT INTO orders (id) VALUES (?)", msg.Value)\n'
        '}\n'
    )

    # ── Idempotency: Go Kafka consumer with ON CONFLICT → NOT flagged ──────────
    (root / "consumer_good.go").write_text(
        'package main\n'
        'import "github.com/confluentinc/confluent-kafka-go/kafka"\n'
        '\n'
        'func handleMessage(msg *kafka.Message) {\n'
        '    db.Exec("INSERT INTO orders (id) VALUES (?) ON CONFLICT DO NOTHING", msg.Value)\n'
        '}\n'
    )

    idem = find_idempotency_gaps({}, tmpdir)
    bad_files = [f["file"] for f in idem]
    assert any("consumer_bad.go" in f for f in bad_files), \
        f"IDEM-001 should flag consumer_bad.go, got: {bad_files}"
    assert not any("consumer_good.go" in f for f in bad_files), \
        f"IDEM-001 should NOT flag consumer_good.go, got: {bad_files}"
    print("[PASS] IDEM-001: INSERT without ON CONFLICT flagged; ON CONFLICT variant not flagged")

    # ── Resource Leak: Go os.Open without defer → flagged ─────────────────────
    (root / "io_bad.go").write_text(
        'package main\n'
        'import "os"\n'
        '\n'
        'func readFile() {\n'
        '    f, err := os.Open("data.txt")\n'
        '    if err != nil { return }\n'
        '    // do something with f\n'
        '    _ = f\n'
        '}\n'
    )

    # ── Resource Leak: Go os.Open with defer → NOT flagged ────────────────────
    (root / "io_good.go").write_text(
        'package main\n'
        'import "os"\n'
        '\n'
        'func readFile() {\n'
        '    f, err := os.Open("data.txt")\n'
        '    if err != nil { return }\n'
        '    defer f.Close()\n'
        '    _ = f\n'
        '}\n'
    )

    leaks = find_resource_leaks({}, tmpdir)
    leak_files = [f["file"] for f in leaks]
    assert any("io_bad.go" in f for f in leak_files), \
        f"LEAK-001 should flag io_bad.go, got: {leak_files}"
    assert not any("io_good.go" in f for f in leak_files), \
        f"LEAK-001 should NOT flag io_good.go, got: {leak_files}"
    print("[PASS] LEAK-001: os.Open without defer flagged; with defer not flagged")

    # ── Resource Leak: Python bare open without close → flagged ───────────────
    (root / "io_bad.py").write_text(
        'def read_config():\n'
        '    f = open("config.txt", "r")\n'
        '    data = f.read()\n'
        '    return data\n'
    )

    # ── Resource Leak: Python with open → NOT flagged ─────────────────────────
    (root / "io_good.py").write_text(
        'def read_config():\n'
        '    with open("config.txt", "r") as f:\n'
        '        data = f.read()\n'
        '    return data\n'
    )

    leaks2 = find_resource_leaks({}, tmpdir)
    leak_files2 = [f["file"] for f in leaks2]
    assert any("io_bad.py" in f for f in leak_files2), \
        f"LEAK-004 should flag io_bad.py, got: {leak_files2}"
    assert not any("io_good.py" in f for f in leak_files2), \
        f"LEAK-004 should NOT flag io_good.py, got: {leak_files2}"
    print("[PASS] LEAK-004: bare open() flagged; with open() not flagged")

    # ── Migration: SQL with ALTER TABLE ADD COLUMN → flagged for MIGR-001 ─────
    migr_dir = root / "db" / "migrations"
    migr_dir.mkdir(parents=True)
    (migr_dir / "V001__add_col.sql").write_text(
        "ALTER TABLE users ADD COLUMN email VARCHAR(255) NOT NULL DEFAULT '';\n"
    )

    # ── Migration: same with ALGORITHM=INPLACE → NOT flagged for MIGR-001 ─────
    (migr_dir / "V002__add_col_safe.sql").write_text(
        "ALTER TABLE users ADD COLUMN phone VARCHAR(20) DEFAULT NULL, ALGORITHM=INPLACE, LOCK=NONE;\n"
    )

    migr = find_unsafe_migrations({}, tmpdir)
    migr_files = [f["file"] for f in migr]
    migr001_files = [f["file"] for f in migr if f.get("rule_id") == "MIGR-001"]
    assert any("V001__add_col.sql" in f for f in migr001_files), \
        f"MIGR-001 should flag V001__add_col.sql, got: {migr001_files}"
    assert not any("V002__add_col_safe.sql" in f for f in migr001_files), \
        f"MIGR-001 should NOT flag V002__add_col_safe.sql, got: {migr001_files}"
    print("[PASS] MIGR-001: ALTER TABLE without ALGORITHM flagged; ALGORITHM=INPLACE not flagged")

    # ── Migration: MIGR-003 missing lock_wait_timeout ─────────────────────────
    migr003_files = [f["file"] for f in migr if f.get("rule_id") == "MIGR-003"]
    assert any("V001__add_col.sql" in f for f in migr003_files), \
        f"MIGR-003 should flag V001__add_col.sql for missing lock_wait_timeout, got: {migr003_files}"
    print("[PASS] MIGR-003: missing lock_wait_timeout before ALTER flagged")

    # ── Migration: MIGR-004 TRUNCATE in application code ─────────────────────
    (root / "admin.py").write_text(
        'import db\n'
        'def reset_table():\n'
        '    cursor.execute("TRUNCATE TABLE sessions")\n'
    )
    migr_app = find_unsafe_migrations({}, tmpdir)
    migr004_files = [f["file"] for f in migr_app if f.get("rule_id") == "MIGR-004"]
    assert any("admin.py" in f for f in migr004_files), \
        f"MIGR-004 should flag admin.py, got: {migr004_files}"
    print("[PASS] MIGR-004: TRUNCATE in application code flagged")

    # ── Verify total_checks in get_health_summary ─────────────────────────────
    summary = get_health_summary({}, tmpdir)
    assert summary["checks_passed"] + summary["checks_failed"] == 19, \
        f"Expected total_checks=19, got {summary['checks_passed'] + summary['checks_failed']}"
    print("[PASS] get_health_summary: total_checks == 19")

    # ── Verify MCP stubs return correct shape ─────────────────────────────────
    result = graph_idempotency_gaps_tool(tmpdir)
    assert "ok" in result and "total" in result and "findings" in result, \
        f"graph_idempotency_gaps_tool bad shape: {result}"
    result2 = graph_resource_leaks_tool(tmpdir)
    assert "ok" in result2 and "total" in result2 and "findings" in result2, \
        f"graph_resource_leaks_tool bad shape: {result2}"
    result3 = graph_unsafe_migrations_tool(tmpdir)
    assert "ok" in result3 and "total" in result3 and "findings" in result3, \
        f"graph_unsafe_migrations_tool bad shape: {result3}"
    print("[PASS] MCP export stubs return correct shape")

    print("\n=== All _test_new_structural_2 tests PASSED ===")


def _test_new_structural_3():
    import tempfile, os

    # ── POOL-001: Go sql.Open without SetMaxOpenConns ─────────────────────────
    go_bad  = 'import "database/sql"\ndb, err := sql.Open("postgres", dsn)\ndb.SetConnMaxLifetime(time.Minute)\n'
    go_good = 'import "database/sql"\ndb, err := sql.Open("postgres", dsn)\ndb.SetMaxOpenConns(25)\n'
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "db.go")
        open(f, "w").write(go_bad)
        r = find_connection_pool_misconfigs({}, d)
        assert any(x["rule_id"] == "POOL-001" for x in r), f"POOL-001 not flagged: {r}"
        print("[PASS] POOL-001: flagged when SetMaxOpenConns absent")
        open(f, "w").write(go_good)
        r2 = find_connection_pool_misconfigs({}, d)
        assert not any(x["rule_id"] == "POOL-001" for x in r2), f"POOL-001 false positive: {r2}"
        print("[PASS] POOL-001: not flagged when SetMaxOpenConns present")

    # ── POOL-002: SQLAlchemy without pool_size ─────────────────────────────────
    py_bad  = "from sqlalchemy import create_engine\nengine = create_engine(DATABASE_URL)\n"
    py_good = "from sqlalchemy import create_engine\nengine = create_engine(DATABASE_URL, pool_size=10)\n"
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "db.py")
        open(f, "w").write(py_bad)
        r3 = find_connection_pool_misconfigs({}, d)
        assert any(x["rule_id"] == "POOL-002" for x in r3), f"POOL-002 not flagged: {r3}"
        print("[PASS] POOL-002: flagged when pool_size absent")
        open(f, "w").write(py_good)
        r4 = find_connection_pool_misconfigs({}, d)
        assert not any(x["rule_id"] == "POOL-002" for x in r4), f"POOL-002 false positive: {r4}"
        print("[PASS] POOL-002: not flagged when pool_size present")

    # ── POOL-002: NullPool exclusion ──────────────────────────────────────────
    py_null = "from sqlalchemy import create_engine, NullPool\nengine = create_engine(DATABASE_URL, pool_class=NullPool)\n"
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "db.py")
        open(f, "w").write(py_null)
        r_null = find_connection_pool_misconfigs({}, d)
        assert not any(x["rule_id"] == "POOL-002" for x in r_null), f"POOL-002 false positive on NullPool: {r_null}"
        print("[PASS] POOL-002: not flagged when pool_class=NullPool")

    # ── HLTH-002: K8s Deployment without readinessProbe ───────────────────────
    k8s_bad = (
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
        "  template:\n    spec:\n      containers:\n"
        "      - name: app\n        image: myapp:latest\n"
        "        ports:\n        - containerPort: 8080\n"
    )
    k8s_good = (
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
        "  template:\n    spec:\n      containers:\n"
        "      - name: app\n        image: myapp:latest\n"
        "        readinessProbe:\n          httpGet:\n            path: /health\n            port: 8080\n"
        "        livenessProbe:\n          httpGet:\n            path: /health\n            port: 8080\n"
    )
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "deploy.yaml")
        open(f, "w").write(k8s_bad)
        r5 = find_missing_health_checks({}, d)
        assert any(x["rule_id"] == "HLTH-002" for x in r5), f"HLTH-002 not flagged: {r5}"
        print("[PASS] HLTH-002: flagged Deployment without readinessProbe")
        open(f, "w").write(k8s_good)
        r6 = find_missing_health_checks({}, d)
        hlth002 = [x for x in r6 if x["rule_id"] == "HLTH-002"]
        assert not hlth002, f"HLTH-002 false positive with probes present: {hlth002}"
        print("[PASS] HLTH-002: not flagged when probes present")

    # ── MCP stubs shape check ─────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        res_pool   = graph_pool_misconfigs_tool(d)
        res_health = graph_health_checks_tool(d)
        assert "ok" in res_pool   and "total" in res_pool   and "findings" in res_pool,   f"pool tool bad shape: {res_pool}"
        assert "ok" in res_health and "total" in res_health and "findings" in res_health, f"health tool bad shape: {res_health}"
        print("[PASS] MCP stubs graph_pool_misconfigs_tool / graph_health_checks_tool return correct shape")

    # ── total_checks updated to 19 ────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        summary = get_health_summary({}, d)
        assert summary["checks_passed"] + summary["checks_failed"] == 19, (
            f"Expected total_checks=19, got {summary['checks_passed'] + summary['checks_failed']}"
        )
        print("[PASS] get_health_summary: total_checks == 19")

    print("\n=== All _test_new_structural_3 tests PASSED ===")


# ---------------------------------------------------------------------------
# CLI entry point (convenience — not required for MCP usage)
# ---------------------------------------------------------------------------

def _load_graph_from_root(project_root: str) -> dict:
    """Load info_graph.json from a project root.  Tries common locations."""
    candidates = [
        Path(project_root) / ".dual-graph" / "info_graph.json",
        Path(project_root) / ".dual-graph-pro" / "info_graph.json",
        Path(project_root) / "data" / "info_graph.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
    return {}


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="GrapeRoot Pro — Structural Bug Detector")
    ap.add_argument("root", nargs="?", default=".", help="Project root path")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of text")
    ap.add_argument("--summary", action="store_true", help="Print health summary only")
    args = ap.parse_args()

    proj_root = str(Path(args.root).expanduser().resolve())
    g = _load_graph_from_root(proj_root)
    if not g:
        print(
            f"[structural_bugs] No info_graph.json found under {proj_root}. "
            "Run dgc/graperoot to build the graph first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.summary:
        result = get_health_summary(g, proj_root)
    else:
        result = run_all_checks(g, proj_root)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if args.summary:
            print(f"\nHealth Score : {result['score']}/100  (grade {result['grade']})")
            print(f"Total issues : {result['total_issues']}")
            print(f"Checks passed: {result['checks_passed']}/{result['checks_passed'] + result['checks_failed']}")
            if result["critical_issues"]:
                print("\nTop issues:")
                for ci in result["critical_issues"]:
                    print(f"  · {ci}")
        else:
            bys = result["by_severity"]
            print(
                f"\n{result['total_issues']} structural issue(s) found  "
                f"[critical={bys['critical']}  high={bys['high']}  "
                f"medium={bys['medium']}  low={bys['low']}]"
            )
            for issue in result["issues"]:
                sev = issue.get("severity", "?").upper()
                typ = issue.get("type", "?")
                desc = issue.get("description", "")
                print(f"\n  [{sev}] {typ}")
                print(f"  {desc}")
                fix = issue.get("fix", "")
                if fix:
                    print(f"  Fix: {fix}")
