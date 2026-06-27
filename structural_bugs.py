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

    # Total checks = 7 functional checks
    total_checks  = 7
    checks_failed = min(
        total_checks,
        (1 if n_critical else 0) + (1 if n_high else 0) +
        (1 if n_medium else 0) + (1 if n_low else 0),
    )
    checks_passed = total_checks - checks_failed

    critical_issues: list[str] = [
        issue["description"]
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
