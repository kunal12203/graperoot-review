"""
Observability configuration and instrumentation parser for GrapeRoot Pro.

Phase 8 — Observability module. Parses:
  8A  OpenTelemetry topology (collector config YAML + SDK call sites)
  8B  Prometheus alert rules and scrape configs
  8C  Grafana dashboard JSON (panels + queries)
  8D  Logging pattern detection

Each public function follows the standard GrapeRoot symbol contract:
    id, name, symbol_type, line_start, line_end, body_hash,
    confidence, exported, keywords

Symbol types: model | use_case | api_route | utility | hook
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

OBSERVABILITY_EXTS = {".yaml", ".yml", ".json"}

OBSERVABILITY_SOURCE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt",
}

# File-name patterns that identify observability configs
_OTEL_FILENAME_PAT = re.compile(
    r"(?i)(otel|otelcol|opentelemetry|collector)[^/\\]*\.(ya?ml)$"
)
_PROMETHEUS_FILENAME_PAT = re.compile(
    r"(?i)(alert|rule|recording_rule|prometheus)[^/\\]*\.(ya?ml)$"
)
_GRAFANA_FILENAME_PAT = re.compile(
    r"(?i)dashboard[^/\\]*\.json$"
)

# Content hints (first 512 chars) for ambiguous filenames
_OTEL_CONTENT_HINTS = re.compile(
    r"receivers:|processors:|exporters:|otel(col)?:|opentelemetry:"
)
_PROMETHEUS_CONTENT_HINTS = re.compile(
    r"groups:|alerting:|rule_files:|scrape_configs:|alert:|record:"
)
_GRAFANA_CONTENT_HINTS = re.compile(
    r'"panels"\s*:|"templating"\s*:|"datasource"\s*:|"uid"\s*:'
)


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _body_hash(lines: list[str], start: int, end: int) -> str:
    """MD5 (8 hex chars) of the text block [start, end] inclusive."""
    body = "\n".join(lines[start : end + 1])
    return hashlib.md5(body.encode()).hexdigest()[:8]


def _split_camel(name: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [p.lower() for p in parts.split() if len(p) >= 3]


def _name_keywords(name: str) -> list[str]:
    """Tokenise a symbol name into searchable lowercase keywords."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(w: str) -> None:
        w = w.lower().strip("_-.")
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            tokens.append(w)

    add(name)
    for p in _split_camel(name):
        add(p)
    for p in re.split(r"[_\-./]", name):
        add(p)
    return tokens[:10]


def _is_observability_config(file_path: str, content_hint: str = "") -> bool:
    """
    Return True when file_path looks like an observability config.

    Checks: filename patterns first, then content hints on the first 512 chars.
    """
    basename = os.path.basename(file_path)
    if _OTEL_FILENAME_PAT.search(file_path):
        return True
    if _PROMETHEUS_FILENAME_PAT.search(basename):
        return True
    if _GRAFANA_FILENAME_PAT.search(basename):
        return True
    if content_hint:
        snippet = content_hint[:512]
        if (_OTEL_CONTENT_HINTS.search(snippet)
                or _PROMETHEUS_CONTENT_HINTS.search(snippet)
                or _GRAFANA_CONTENT_HINTS.search(snippet)):
            return True
    return False


def _safe_read(file_path: str) -> tuple[bool, str, list[str]]:
    """
    Read a file safely.  Returns (ok, content, lines).
    On any IO/encoding error returns (False, "", []).
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return True, content, content.splitlines()
    except Exception:
        return False, "", []


# ═══════════════════════════════════════════════════════════════════════════════
# 8A  OPENTELEMETRY
# ═══════════════════════════════════════════════════════════════════════════════

# ── YAML block scanner helpers ──────────────────────────────────────────────

def _yaml_indent(line: str) -> int:
    """Return leading-space count (tabs treated as 2 spaces)."""
    stripped = line.lstrip()
    return len(line) - len(stripped)


def _yaml_key(line: str) -> Optional[str]:
    """Return the key name from a YAML mapping line, or None."""
    m = re.match(r"^\s*([A-Za-z0-9_\-./]+)\s*:", line)
    return m.group(1) if m else None


def _collect_top_level_keys(
    lines: list[str],
    section_start: int,
    base_indent: int,
) -> list[tuple[str, int, int]]:
    """
    Starting just after a section header line at indent `base_indent`,
    collect direct children (keys at base_indent + 2 spaces / 1 level deeper).
    Returns [(key_name, first_line_idx, last_line_idx), ...].
    """
    entries: list[tuple[str, int, int]] = []
    child_indent: Optional[int] = None
    current_key: Optional[str] = None
    current_start: int = 0

    for i in range(section_start, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _yaml_indent(line)
        key = _yaml_key(line)

        # Determine first-child indent level
        if child_indent is None and indent > base_indent and key:
            child_indent = indent

        if child_indent is None:
            continue

        if indent <= base_indent:
            # Reached a sibling or ancestor section — this section has ended
            if current_key is not None:
                entries.append((current_key, current_start, i - 1))
                current_key = None
            break

        if indent == child_indent and key:
            if current_key is not None:
                entries.append((current_key, current_start, i - 1))
            current_key = key
            current_start = i

    if current_key is not None:
        entries.append((current_key, current_start, len(lines) - 1))

    return entries


def _find_section_line(lines: list[str], section: str, parent_indent: int = 0) -> int:
    """
    Return line index of `section:` at indent >= parent_indent, or -1.
    """
    for i, line in enumerate(lines):
        if _yaml_indent(line) >= parent_indent and _yaml_key(line) == section:
            return i
    return -1


def _extract_inline_list(lines: list[str], start_line: int) -> list[str]:
    """
    Extract items from either:
      key: [a, b, c]              (inline)
      key:
        - a
        - b
    Returns list of strings.
    """
    line = lines[start_line]
    # Inline: key: [a, b, c]
    m = re.search(r"\[([^\]]*)\]", line)
    if m:
        raw = m.group(1)
        return [x.strip().strip("'\"") for x in raw.split(",") if x.strip()]

    # Multi-line dash list
    items: list[str] = []
    base_indent = _yaml_indent(line)
    for i in range(start_line + 1, len(lines)):
        ln = lines[i]
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ind = _yaml_indent(ln)
        if ind <= base_indent and not stripped.startswith("-"):
            break
        m2 = re.match(r"^\s*-\s*(.+)", ln)
        if m2:
            items.append(m2.group(1).strip().strip("'\""))
    return items


def _endpoint_for_block(lines: list[str], start: int, end: int) -> str:
    """Scan lines[start:end+1] for an `endpoint:` value."""
    for i in range(start, min(end + 1, len(lines))):
        m = re.match(r"^\s*endpoint\s*:\s*(.+)", lines[i])
        if m:
            return m.group(1).strip().strip("'\"")
    return ""


def _config_keys_for_block(lines: list[str], start: int, end: int) -> list[str]:
    """Return direct child YAML keys inside a block."""
    block_indent: Optional[int] = None
    keys: list[str] = []
    for i in range(start + 1, min(end + 1, len(lines))):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ind = _yaml_indent(line)
        key = _yaml_key(line)
        if block_indent is None and key:
            block_indent = ind
        if block_indent is not None and ind == block_indent and key:
            keys.append(key)
    return keys


# ── Main OTel config parser ─────────────────────────────────────────────────

def parse_otel_config(file_path: str) -> dict:
    """
    Parse an OTel Collector config YAML and extract topology.

    Returns a dict with keys:
        ok, file, receivers, processors, exporters, pipelines, extensions, symbols
    """
    ok, content, lines = _safe_read(file_path)
    if not ok:
        return {"ok": False, "file": file_path, "receivers": [], "processors": [],
                "exporters": [], "pipelines": [], "extensions": [], "symbols": []}

    receivers: list[dict] = []
    processors: list[dict] = []
    exporters: list[dict] = []
    pipelines: list[dict] = []
    extensions: list[str] = []
    symbols: list[dict] = []

    # ── Receivers ────────────────────────────────────────────────────────────
    recv_line = _find_section_line(lines, "receivers")
    if recv_line >= 0:
        base_ind = _yaml_indent(lines[recv_line])
        for name, s, e in _collect_top_level_keys(lines, recv_line + 1, base_ind):
            # type = part before "/" or the whole name
            rtype = name.split("/")[0]
            cfg_keys = _config_keys_for_block(lines, s, e)
            receivers.append({"name": name, "type": rtype, "config_keys": cfg_keys,
                               "line_start": s, "line_end": e})
            symbols.append({
                "id": f"{file_path}::receiver:{name}",
                "name": f"receiver:{name}",
                "symbol_type": "hook",
                "line_start": s,
                "line_end": e,
                "body_hash": _body_hash(lines, s, e),
                "confidence": "high",
                "exported": True,
                "keywords": list(dict.fromkeys(
                    _name_keywords(name) + [rtype, "otel", "receiver"]
                ))[:10],
            })

    # ── Processors ───────────────────────────────────────────────────────────
    proc_line = _find_section_line(lines, "processors")
    if proc_line >= 0:
        base_ind = _yaml_indent(lines[proc_line])
        for name, s, e in _collect_top_level_keys(lines, proc_line + 1, base_ind):
            ptype = name.split("/")[0]
            processors.append({"name": name, "type": ptype,
                                "line_start": s, "line_end": e})
            symbols.append({
                "id": f"{file_path}::processor:{name}",
                "name": f"processor:{name}",
                "symbol_type": "utility",
                "line_start": s,
                "line_end": e,
                "body_hash": _body_hash(lines, s, e),
                "confidence": "high",
                "exported": True,
                "keywords": list(dict.fromkeys(
                    _name_keywords(name) + [ptype, "otel", "processor"]
                ))[:10],
            })

    # ── Exporters ────────────────────────────────────────────────────────────
    exp_line = _find_section_line(lines, "exporters")
    if exp_line >= 0:
        base_ind = _yaml_indent(lines[exp_line])
        for name, s, e in _collect_top_level_keys(lines, exp_line + 1, base_ind):
            etype = name.split("/")[0]
            endpoint = _endpoint_for_block(lines, s, e)
            exporters.append({"name": name, "type": etype, "endpoint": endpoint,
                               "line_start": s, "line_end": e})
            symbols.append({
                "id": f"{file_path}::exporter:{name}",
                "name": f"exporter:{name}",
                "symbol_type": "use_case",
                "line_start": s,
                "line_end": e,
                "body_hash": _body_hash(lines, s, e),
                "confidence": "high",
                "exported": True,
                "keywords": list(dict.fromkeys(
                    _name_keywords(name) + [etype, "otel", "exporter"]
                ))[:10],
            })

    # ── Extensions ───────────────────────────────────────────────────────────
    # service: extensions: [health_check, pprof]  — or multi-line
    svc_line = _find_section_line(lines, "service")
    if svc_line >= 0:
        svc_ind = _yaml_indent(lines[svc_line])
        ext_line = _find_section_line(lines, "extensions")
        # Search only within service block
        for i in range(svc_line + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and not ln.strip().startswith("#"):
                if _yaml_indent(ln) <= svc_ind and i > svc_line:
                    break
            if _yaml_key(ln) == "extensions":
                extensions = _extract_inline_list(lines, i)
                break

        # ── Pipelines ────────────────────────────────────────────────────────
        pip_line = -1
        for i in range(svc_line + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and not ln.strip().startswith("#"):
                if _yaml_indent(ln) <= svc_ind and i > svc_line:
                    break
            if _yaml_key(ln) == "pipelines":
                pip_line = i
                break

        if pip_line >= 0:
            pip_ind = _yaml_indent(lines[pip_line])
            for pname, ps, pe in _collect_top_level_keys(lines, pip_line + 1, pip_ind):
                # type = the pipeline signal (traces / metrics / logs) — part before "/"
                ptype = pname.split("/")[0]
                p_recv: list[str] = []
                p_proc: list[str] = []
                p_exp: list[str] = []
                for i in range(ps + 1, min(pe + 1, len(lines))):
                    sub_key = _yaml_key(lines[i])
                    if sub_key == "receivers":
                        p_recv = _extract_inline_list(lines, i)
                    elif sub_key == "processors":
                        p_proc = _extract_inline_list(lines, i)
                    elif sub_key == "exporters":
                        p_exp = _extract_inline_list(lines, i)
                pipelines.append({
                    "name": pname,
                    "type": ptype,
                    "receivers": p_recv,
                    "processors": p_proc,
                    "exporters": p_exp,
                    "line_start": ps,
                    "line_end": pe,
                })
                symbols.append({
                    "id": f"{file_path}::pipeline:{pname}",
                    "name": f"pipeline:{pname}",
                    "symbol_type": "model",
                    "line_start": ps,
                    "line_end": pe,
                    "body_hash": _body_hash(lines, ps, pe),
                    "confidence": "high",
                    "exported": True,
                    "keywords": list(dict.fromkeys(
                        _name_keywords(pname) + [ptype, "otel", "pipeline"]
                    ))[:10],
                })

    return {
        "ok": True,
        "file": file_path,
        "receivers": receivers,
        "processors": processors,
        "exporters": exporters,
        "pipelines": pipelines,
        "extensions": extensions,
        "symbols": symbols,
    }


# ── OTel config file discovery ───────────────────────────────────────────────

_OTEL_FILE_NAMES = re.compile(
    r"(?i)(otel|otelcol|opentelemetry|collector|\.otel)[\w\-.]*\.(ya?ml)$"
)


def find_otel_configs(project_root: str) -> list[str]:
    """
    Walk project_root and return paths to all OTel collector config files.

    Matches: otel*.yaml, otelcol*.yaml, collector*.yaml,
             opentelemetry*.yaml, .otel.yaml, otel-config.yaml
    """
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        # Prune common noise directories
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            if _OTEL_FILE_NAMES.search(fname):
                results.append(os.path.join(dirpath, fname))
    return sorted(results)


# ── SDK instrumentation extraction ──────────────────────────────────────────

# Patterns per language: (regex, span_group, metric_group_or_None)
_OTEL_SDK_PATTERNS: dict[str, list[tuple[re.Pattern, int, Optional[int]]]] = {
    ".py": [
        (re.compile(r"tracer\.start_as_current_span\(\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r"tracer\.start_span\(\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r"meter\.create_counter\(\s*['\"]([^'\"]+)['\"]"), None, 1),
        (re.compile(r"meter\.create_histogram\(\s*['\"]([^'\"]+)['\"]"), None, 1),
        (re.compile(r"meter\.create_gauge\(\s*['\"]([^'\"]+)['\"]"), None, 1),
        (re.compile(r"meter\.create_up_down_counter\(\s*['\"]([^'\"]+)['\"]"), None, 1),
    ],
    ".ts": [
        (re.compile(r"tracer\.startActiveSpan\(\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r"tracer\.startSpan\(\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r"meter\.createCounter\(\s*['\"]([^'\"]+)['\"]"), None, 1),
        (re.compile(r"meter\.createHistogram\(\s*['\"]([^'\"]+)['\"]"), None, 1),
        (re.compile(r"meter\.createObservableGauge\(\s*['\"]([^'\"]+)['\"]"), None, 1),
    ],
    ".js": [],  # same as .ts — filled below
    ".tsx": [], # same as .ts
    ".jsx": [], # same as .js
    ".go": [
        (re.compile(r"tracer\.Start\(\s*\w+\s*,\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r"otel\.Tracer\(\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r"meter\.(?:Int64|Float64)Counter\(\s*['\"]([^'\"]+)['\"]"), None, 1),
    ],
    ".java": [
        (re.compile(r"tracer\.spanBuilder\(\s*['\"]([^'\"]+)['\"]"), 1, None),
        (re.compile(r'@WithSpan\(\s*"([^"]+)"'), 1, None),
        (re.compile(r"tracer\.spanBuilder\(\s*\"([^\"]+)\""), 1, None),
        (re.compile(r"LongCounter\s+\w+\s*=\s*meter\.counterBuilder\(\s*\"([^\"]+)\""), None, 1),
    ],
    ".kt": [],  # same as .java — filled below
}
_OTEL_SDK_PATTERNS[".js"] = _OTEL_SDK_PATTERNS[".ts"]
_OTEL_SDK_PATTERNS[".tsx"] = _OTEL_SDK_PATTERNS[".ts"]
_OTEL_SDK_PATTERNS[".jsx"] = _OTEL_SDK_PATTERNS[".js"]
_OTEL_SDK_PATTERNS[".kt"] = _OTEL_SDK_PATTERNS[".java"]


def extract_otel_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract OTel SDK span and metric creation calls from source code.

    Returns symbol dicts with symbol_type="hook" (spans) or symbol_type="use_case"
    (metrics).  Line numbers are 0-based internally but stored 1-based.
    """
    patterns = _OTEL_SDK_PATTERNS.get(ext, [])
    if not patterns:
        return []

    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for lineno, line in enumerate(lines):
        for pat, span_grp, metric_grp in patterns:
            m = pat.search(line)
            if not m:
                continue
            if span_grp is not None:
                label = m.group(span_grp)
                sym_type = "hook"
                kind = "span"
            else:
                label = m.group(metric_grp)  # type: ignore[arg-type]
                sym_type = "use_case"
                kind = "metric"
            sym_id = f"{file_path}::otel_{kind}:{label}:{lineno}"
            if sym_id in seen:
                continue
            seen.add(sym_id)
            symbols.append({
                "id": sym_id,
                "name": f"otel_{kind}:{label}",
                "symbol_type": sym_type,
                "line_start": lineno,
                "line_end": lineno,
                "body_hash": _body_hash(lines, lineno, lineno),
                "confidence": "high",
                "exported": True,
                "keywords": list(dict.fromkeys(
                    _name_keywords(label) + ["otel", kind]
                ))[:10],
            })

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8B  PROMETHEUS
# ═══════════════════════════════════════════════════════════════════════════════

_PROMETHEUS_FILE_NAMES = re.compile(
    r"(?i)(alert|rule|recording_rule|prometheus)[\w\-\.]*\.(ya?ml)$"
)


def find_prometheus_configs(project_root: str) -> list[str]:
    """
    Walk project_root and return paths to all Prometheus rule/alert YAML files.
    """
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            if _PROMETHEUS_FILE_NAMES.search(fname):
                results.append(os.path.join(dirpath, fname))
    return sorted(results)


def parse_prometheus_alerts(file_path: str) -> dict:
    """
    Parse a Prometheus alert/recording-rule YAML file.

    Returns:
        ok, file, groups, alerts, recording_rules, symbols
    """
    ok, content, lines = _safe_read(file_path)
    if not ok:
        return {"ok": False, "file": file_path, "groups": [], "alerts": [],
                "recording_rules": [], "symbols": []}

    groups: list[dict] = []
    alerts: list[dict] = []
    recording_rules: list[dict] = []
    symbols: list[dict] = []

    # groups: is the top-level section
    # Structure (YAML list):
    #   groups:
    #     - name: <group_name>
    #       rules:
    #         - alert: <name>      OR   record: <name>
    #           expr: <promql>
    #           for: <duration>
    #           labels: {severity: warning}
    #           annotations: {summary: ..., description: ...}

    # We use regex scanning — no YAML library.
    # State machine: track current group, then iterate rules.

    # Find each group entry by scanning for "- name:" under "groups:"
    in_groups = False
    current_group_name: Optional[str] = None
    current_group_start: int = 0
    current_rule_start: int = 0
    current_rule: Optional[dict] = None
    in_rules = False
    base_groups_indent: Optional[int] = None
    rule_indent: Optional[int] = None
    group_item_indent: Optional[int] = None

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        ind = _yaml_indent(line)

        # Detect start of groups section
        if not in_groups:
            if re.match(r"^\s*groups\s*:", line):
                in_groups = True
                base_groups_indent = ind
            i += 1
            continue

        # We are inside groups section
        # Detect group list items: "  - name: something"
        m_group = re.match(r"^(\s*)-\s+name\s*:\s*(.+)", line)
        if m_group:
            # Finalise previous rule if any
            if current_rule is not None:
                _finalise_rule(current_rule, current_rule_start, i - 1,
                               lines, alerts, recording_rules, symbols, file_path)
                current_rule = None

            # Finalise previous group
            if current_group_name is not None:
                groups.append({
                    "name": current_group_name,
                    "line_start": current_group_start,
                    "line_end": i - 1,
                })

            current_group_name = m_group.group(2).strip().strip("'\"")
            current_group_start = i
            group_item_indent = len(m_group.group(1)) + 2  # indent of keys inside this group item
            in_rules = False
            rule_indent = None
            i += 1
            continue

        # Detect "rules:" under a group
        if current_group_name is not None and re.match(r"^\s*rules\s*:", line):
            if current_rule is not None:
                _finalise_rule(current_rule, current_rule_start, i - 1,
                               lines, alerts, recording_rules, symbols, file_path)
                current_rule = None
            in_rules = True
            rule_indent = None
            i += 1
            continue

        if not in_rules:
            i += 1
            continue

        # Detect rule list items: "  - alert: name" or "  - record: name"
        m_alert = re.match(r"^\s*-\s+(?:alert|record)\s*:", line)
        if m_alert:
            # Finalise previous rule
            if current_rule is not None:
                _finalise_rule(current_rule, current_rule_start, i - 1,
                               lines, alerts, recording_rules, symbols, file_path)

            current_rule_start = i
            current_rule = {"_group": current_group_name}
            rule_indent = ind
            # Parse the key on this line
            m_key = re.match(r"^\s*-\s+(alert|record)\s*:\s*(.+)", line)
            if m_key:
                current_rule[m_key.group(1)] = m_key.group(2).strip().strip("'\"")
            i += 1
            continue

        if current_rule is not None:
            # Parse rule sub-keys
            m_kv = re.match(r"^\s+(\w+)\s*:\s*(.*)", line)
            if m_kv:
                key = m_kv.group(1)
                val = m_kv.group(2).strip().strip("'\"")
                if key in ("expr", "for", "severity"):
                    current_rule[key] = val
                elif key == "labels":
                    # Collect labels block inline or as sub-dict
                    if val:
                        current_rule["_labels_inline"] = val
                    else:
                        current_rule["_labels_block_start"] = i
                elif key == "annotations":
                    if val:
                        current_rule["_annotations_inline"] = val
                    else:
                        current_rule["_annotations_block_start"] = i
                elif key == "severity":
                    current_rule["severity"] = val

            # Parse severity from labels block
            m_sev = re.match(r"^\s+severity\s*:\s*(.+)", line)
            if m_sev and current_rule is not None:
                current_rule.setdefault("severity", m_sev.group(1).strip().strip("'\""))

        i += 1

    # Finalise last rule and group
    if current_rule is not None:
        _finalise_rule(current_rule, current_rule_start, len(lines) - 1,
                       lines, alerts, recording_rules, symbols, file_path)
    if current_group_name is not None:
        groups.append({
            "name": current_group_name,
            "line_start": current_group_start,
            "line_end": len(lines) - 1,
        })

    return {
        "ok": True,
        "file": file_path,
        "groups": groups,
        "alerts": alerts,
        "recording_rules": recording_rules,
        "symbols": symbols,
    }


def _finalise_rule(
    rule: dict,
    start: int,
    end: int,
    lines: list[str],
    alerts: list[dict],
    recording_rules: list[dict],
    symbols: list[dict],
    file_path: str,
) -> None:
    """Commit a parsed rule dict into alerts / recording_rules / symbols."""
    # severity may live under labels: key
    severity = rule.get("severity", "")
    if not severity:
        # scan block for severity
        for i in range(start, min(end + 1, len(lines))):
            m = re.match(r"^\s+severity\s*:\s*(.+)", lines[i])
            if m:
                severity = m.group(1).strip().strip("'\"")
                break

    # Annotations: scan for summary / description
    annotations: dict[str, str] = {}
    labels: dict[str, str] = {}
    for i in range(start, min(end + 1, len(lines))):
        m_ann = re.match(r"^\s+(summary|description|runbook_url)\s*:\s*(.*)", lines[i])
        if m_ann:
            annotations[m_ann.group(1)] = m_ann.group(2).strip().strip("'\"")
        m_lbl = re.match(r"^\s+(severity|team|env)\s*:\s*(.*)", lines[i])
        if m_lbl:
            labels[m_lbl.group(1)] = m_lbl.group(2).strip().strip("'\"")

    if "alert" in rule:
        name = rule["alert"]
        alerts.append({
            "name": name,
            "group": rule.get("_group", ""),
            "severity": severity or labels.get("severity", ""),
            "expr": rule.get("expr", ""),
            "for": rule.get("for", ""),
            "annotations": annotations,
            "labels": labels,
            "line_start": start,
            "line_end": end,
        })
        kw_sev = severity or labels.get("severity", "")
        symbols.append({
            "id": f"{file_path}::alert:{name}",
            "name": f"alert:{name}",
            "symbol_type": "hook",
            "line_start": start,
            "line_end": end,
            "body_hash": _body_hash(lines, start, end),
            "confidence": "high",
            "exported": True,
            "keywords": list(dict.fromkeys(
                _name_keywords(name) + ([kw_sev] if kw_sev else []) + ["alert", "prometheus"]
            ))[:10],
        })

    elif "record" in rule:
        name = rule["record"]
        recording_rules.append({
            "name": name,
            "group": rule.get("_group", ""),
            "expr": rule.get("expr", ""),
            "labels": labels,
            "line_start": start,
            "line_end": end,
        })
        symbols.append({
            "id": f"{file_path}::record:{name}",
            "name": f"record:{name}",
            "symbol_type": "utility",
            "line_start": start,
            "line_end": end,
            "body_hash": _body_hash(lines, start, end),
            "confidence": "high",
            "exported": True,
            "keywords": list(dict.fromkeys(
                _name_keywords(name) + ["recording_rule", "prometheus"]
            ))[:10],
        })


def parse_prometheus_scrape_config(file_path: str) -> dict:
    """
    Parse scrape_configs from a prometheus.yml.

    Returns:
        {
            "ok": bool,
            "file": str,
            "jobs": [{"job_name", "targets", "metrics_path", "scheme"}],
        }
    """
    ok, content, lines = _safe_read(file_path)
    if not ok:
        return {"ok": False, "file": file_path, "jobs": []}

    jobs: list[dict] = []
    in_scrape = False
    current_job: Optional[dict] = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if re.match(r"^\s*scrape_configs\s*:", line):
            in_scrape = True
            continue

        if not in_scrape:
            continue

        # Top-level key that isn't a list item => end of scrape_configs
        if re.match(r"^[A-Za-z]", line):
            if current_job:
                jobs.append(current_job)
                current_job = None
            in_scrape = False
            continue

        m_job = re.match(r"^\s*-\s+job_name\s*:\s*(.+)", line)
        if m_job:
            if current_job:
                jobs.append(current_job)
            current_job = {
                "job_name": m_job.group(1).strip().strip("'\""),
                "targets": [],
                "metrics_path": "/metrics",
                "scheme": "http",
            }
            continue

        if current_job is None:
            continue

        m_path = re.match(r"^\s+metrics_path\s*:\s*(.+)", line)
        if m_path:
            current_job["metrics_path"] = m_path.group(1).strip().strip("'\"")
            continue

        m_scheme = re.match(r"^\s+scheme\s*:\s*(.+)", line)
        if m_scheme:
            current_job["scheme"] = m_scheme.group(1).strip().strip("'\"")
            continue

        # Static targets
        m_target = re.match(r"^\s+-\s+['\"]?([A-Za-z0-9._\-:]+)['\"]?", line)
        if m_target:
            val = m_target.group(1)
            if ":" in val or "." in val:  # looks like host:port
                current_job["targets"].append(val)
            continue

        # Inline targets list: targets: ['host:port', ...]
        if re.match(r"^\s+targets\s*:", line):
            current_job["targets"] = _extract_inline_list(lines, i)
            continue

    if current_job:
        jobs.append(current_job)

    return {"ok": True, "file": file_path, "jobs": jobs}


# ═══════════════════════════════════════════════════════════════════════════════
# 8C  GRAFANA DASHBOARDS
# ═══════════════════════════════════════════════════════════════════════════════

# Simple regex-based JSON extraction (no json library needed, but we use it
# only for structured key scanning — not full AST parsing).
import json as _json


def parse_grafana_dashboard(file_path: str) -> dict:
    """
    Parse a Grafana dashboard JSON file.

    Returns:
        {
            "ok": bool,
            "file": str,
            "title": str,
            "uid": str,
            "panels": [{"title", "id", "type", "queries", "datasource"}],
            "variables": [{"name", "type", "query"}],
            "datasources": [str],
            "symbols": list[dict],
        }
    """
    ok, content, lines = _safe_read(file_path)
    if not ok:
        return {"ok": False, "file": file_path, "title": "", "uid": "",
                "panels": [], "variables": [], "datasources": [], "symbols": []}

    # Try JSON parse; fall back to regex extraction on malformed JSON
    try:
        data = _json.loads(content)
    except Exception:
        data = None

    title = ""
    uid = ""
    panels: list[dict] = []
    variables: list[dict] = []
    datasources: list[str] = []
    symbols: list[dict] = []

    if data is not None:
        title = data.get("title", "")
        uid = data.get("uid", "")

        # Datasource references at dashboard level
        if isinstance(data.get("datasource"), dict):
            ds = data["datasource"].get("uid") or data["datasource"].get("type", "")
            if ds:
                datasources.append(ds)
        elif isinstance(data.get("datasource"), str) and data["datasource"]:
            datasources.append(data["datasource"])

        # Template/variable extraction
        templating = data.get("templating", {})
        if isinstance(templating, dict):
            for var in templating.get("list", []):
                if not isinstance(var, dict):
                    continue
                variables.append({
                    "name": var.get("name", ""),
                    "type": var.get("type", ""),
                    "query": _extract_var_query(var),
                })

        # Panels (may be nested in rows)
        all_panels = _flatten_panels(data.get("panels", []))
        for panel in all_panels:
            if not isinstance(panel, dict):
                continue
            panel_title = panel.get("title", "Untitled")
            panel_id = panel.get("id", 0)
            panel_type = panel.get("type", "")
            queries: list[str] = []
            panel_ds = _panel_datasource(panel, datasources)

            targets = panel.get("targets", [])
            for target in targets:
                if not isinstance(target, dict):
                    continue
                # PromQL / expr
                expr = target.get("expr", "") or target.get("query", "")
                if expr:
                    queries.append(str(expr))
                # Loki LogQL
                lq = target.get("logQL", "") or target.get("logQuery", "")
                if lq:
                    queries.append(str(lq))

            panels.append({
                "title": panel_title,
                "id": panel_id,
                "type": panel_type,
                "queries": queries,
                "datasource": panel_ds,
            })

            # Build symbol for this panel
            # Line number: scan for the panel title in source
            panel_line = _find_str_in_lines(lines, panel_title)
            symbols.append({
                "id": f"{file_path}::panel:{panel_id}:{panel_title}",
                "name": f"panel:{panel_title}",
                "symbol_type": "model",
                "line_start": panel_line,
                "line_end": panel_line,
                "body_hash": hashlib.md5(
                    (panel_title + "".join(queries)).encode()
                ).hexdigest()[:8],
                "confidence": "high",
                "exported": True,
                "keywords": list(dict.fromkeys(
                    _name_keywords(panel_title) + ["panel", "grafana", panel_type]
                ))[:10],
            })

    else:
        # Regex-based fallback when JSON is malformed
        title = _re_json_str(content, "title") or ""
        uid = _re_json_str(content, "uid") or ""

        # Extract panel titles via regex
        for m in re.finditer(r'"title"\s*:\s*"([^"]+)"', content):
            panels.append({
                "title": m.group(1),
                "id": len(panels),
                "type": "unknown",
                "queries": [],
                "datasource": "",
            })

        # Extract PromQL expressions
        for m in re.finditer(r'"expr"\s*:\s*"([^"]+)"', content):
            if panels:
                panels[-1]["queries"].append(m.group(1))

    return {
        "ok": True,
        "file": file_path,
        "title": title,
        "uid": uid,
        "panels": panels,
        "variables": variables,
        "datasources": list(dict.fromkeys(datasources)),
        "symbols": symbols,
    }


def _flatten_panels(panels: list) -> list[dict]:
    """Recursively flatten rows + nested panels."""
    result: list[dict] = []
    for p in panels:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":
            result.extend(_flatten_panels(p.get("panels", [])))
        else:
            result.append(p)
    return result


def _panel_datasource(panel: dict, datasources: list[str]) -> str:
    ds = panel.get("datasource")
    if isinstance(ds, dict):
        val = ds.get("uid") or ds.get("type", "")
        if val and val not in datasources:
            datasources.append(val)
        return str(val)
    if isinstance(ds, str) and ds:
        if ds not in datasources:
            datasources.append(ds)
        return ds
    return ""


def _extract_var_query(var: dict) -> str:
    q = var.get("query", "")
    if isinstance(q, dict):
        return q.get("query", "")
    return str(q)


def _re_json_str(content: str, key: str) -> Optional[str]:
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]+)"', content)
    return m.group(1) if m else None


def _find_str_in_lines(lines: list[str], needle: str) -> int:
    """Return first line index containing needle, or 0."""
    for i, ln in enumerate(lines):
        if needle in ln:
            return i
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# 8D  LOGGING PATTERN DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

# Log frameworks detected by file content
_LOG_FRAMEWORK_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    ".py": [
        ("structlog", re.compile(r"import structlog|structlog\.get_logger")),
        ("loguru", re.compile(r"from loguru import|loguru")),
        ("logging", re.compile(r"import logging|logging\.getLogger")),
    ],
    ".ts": [
        ("pino", re.compile(r"from ['\"]pino['\"]|require\(['\"]pino['\"]")),
        ("winston", re.compile(r"from ['\"]winston['\"]|require\(['\"]winston['\"]")),
        ("console", re.compile(r"console\.(?:log|error|warn|info)")),
    ],
    ".js": [],  # filled below
    ".tsx": [], # filled below
    ".jsx": [], # filled below
    ".go": [
        ("zap", re.compile(r"go\.uber\.org/zap|zap\.NewProduction|zap\.NewDevelopment")),
        ("logrus", re.compile(r"github\.com/sirupsen/logrus|logrus\.")),
        ("zerolog", re.compile(r"github\.com/rs/zerolog")),
        ("slog", re.compile(r"log/slog|slog\.")),
        ("log", re.compile(r"\bimport\s+\"log\"|log\.(?:Printf|Println|Fatal)")),
    ],
    ".java": [
        ("slf4j", re.compile(r"import org\.slf4j|LoggerFactory\.getLogger|@Slf4j")),
        ("log4j2", re.compile(r"import org\.apache\.logging\.log4j")),
        ("jul", re.compile(r"java\.util\.logging")),
    ],
    ".kt": [],  # filled below
}
_LOG_FRAMEWORK_PATTERNS[".js"] = _LOG_FRAMEWORK_PATTERNS[".ts"]
_LOG_FRAMEWORK_PATTERNS[".tsx"] = _LOG_FRAMEWORK_PATTERNS[".ts"]
_LOG_FRAMEWORK_PATTERNS[".jsx"] = _LOG_FRAMEWORK_PATTERNS[".js"]
_LOG_FRAMEWORK_PATTERNS[".kt"] = _LOG_FRAMEWORK_PATTERNS[".java"]

# Log emission site patterns per language: (level, regex)
_LOG_CALL_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    ".py": [
        ("error",   re.compile(r"(?:logger|logging|log)\.error\s*\(")),
        ("warning", re.compile(r"(?:logger|logging|log)\.warn(?:ing)?\s*\(")),
        ("info",    re.compile(r"(?:logger|logging|log)\.info\s*\(")),
        ("debug",   re.compile(r"(?:logger|logging|log)\.debug\s*\(")),
        ("critical",re.compile(r"(?:logger|logging|log)\.critical\s*\(")),
        ("exception",re.compile(r"(?:logger|logging|log)\.exception\s*\(")),
    ],
    ".ts": [
        ("error",   re.compile(r"(?:logger|log|console)\.error\s*\(")),
        ("warn",    re.compile(r"(?:logger|log|console)\.warn\s*\(")),
        ("info",    re.compile(r"(?:logger|log|console)\.info\s*\(")),
        ("debug",   re.compile(r"(?:logger|log|console)\.debug\s*\(")),
        ("fatal",   re.compile(r"(?:logger|log)\.fatal\s*\(")),
    ],
    ".js": [],  # filled below
    ".tsx": [], # filled below
    ".jsx": [], # filled below
    ".go": [
        ("error",   re.compile(r"(?:log|logger|zap|logrus|slog)\s*\.(?:Error|Errorf|WithError)\s*\(")),
        ("warn",    re.compile(r"(?:log|logger|zap|logrus|slog)\s*\.(?:Warn|Warnf|Warning)\s*\(")),
        ("info",    re.compile(r"(?:log|logger|zap|logrus|slog)\s*\.(?:Info|Infof)\s*\(")),
        ("debug",   re.compile(r"(?:log|logger|zap|logrus|slog)\s*\.(?:Debug|Debugf)\s*\(")),
        ("fatal",   re.compile(r"(?:log|logger|zap|logrus)\s*\.(?:Fatal|Fatalf|Panic|Panicf)\s*\(")),
    ],
    ".java": [
        ("error",   re.compile(r"(?:log|logger|LOG)\s*\.error\s*\(")),
        ("warn",    re.compile(r"(?:log|logger|LOG)\s*\.warn\s*\(")),
        ("info",    re.compile(r"(?:log|logger|LOG)\s*\.info\s*\(")),
        ("debug",   re.compile(r"(?:log|logger|LOG)\s*\.debug\s*\(")),
        ("fatal",   re.compile(r"(?:log|logger|LOG)\s*\.fatal\s*\(")),
    ],
    ".kt": [],  # filled below
}
_LOG_CALL_PATTERNS[".js"] = _LOG_CALL_PATTERNS[".ts"]
_LOG_CALL_PATTERNS[".tsx"] = _LOG_CALL_PATTERNS[".ts"]
_LOG_CALL_PATTERNS[".jsx"] = _LOG_CALL_PATTERNS[".js"]
_LOG_CALL_PATTERNS[".kt"] = _LOG_CALL_PATTERNS[".java"]

# Framework setup detection (one symbol per file)
_LOG_SETUP_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    ".py": [
        ("structlog_setup", re.compile(r"structlog\.configure\s*\(")),
        ("logging_basicConfig", re.compile(r"logging\.basicConfig\s*\(")),
        ("logging_dictConfig", re.compile(r"logging\.config\.dictConfig\s*\(")),
    ],
    ".ts": [
        ("winston_createLogger", re.compile(r"winston\.createLogger\s*\(")),
        ("pino_create", re.compile(r"\bpino\s*\(\s*\{")),
    ],
    ".js": [],
    ".tsx": [],
    ".jsx": [],
    ".go": [
        ("zap_NewProduction", re.compile(r"zap\.NewProduction\s*\(")),
        ("zap_NewDevelopment", re.compile(r"zap\.NewDevelopment\s*\(")),
        ("zap_NewNop", re.compile(r"zap\.NewNop\s*\(")),
        ("logrus_WithFields", re.compile(r"logrus\.WithFields\s*\(")),
        ("zerolog_New", re.compile(r"zerolog\.New\s*\(")),
    ],
    ".java": [
        ("getLogger", re.compile(r"LoggerFactory\.getLogger\s*\(")),
        ("Slf4j_annotation", re.compile(r"@Slf4j")),
    ],
    ".kt": [],
}
_LOG_SETUP_PATTERNS[".js"] = _LOG_SETUP_PATTERNS[".ts"]
_LOG_SETUP_PATTERNS[".tsx"] = _LOG_SETUP_PATTERNS[".ts"]
_LOG_SETUP_PATTERNS[".jsx"] = _LOG_SETUP_PATTERNS[".js"]
_LOG_SETUP_PATTERNS[".kt"] = _LOG_SETUP_PATTERNS[".java"]


def extract_log_patterns(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Detect structured logging call sites and framework setup in source code.

    Returns symbol dicts:
    - Log emission sites  → symbol_type="utility"
    - Framework setup     → symbol_type="utility", keywords include framework name
    Also injects a summary symbol recording detected log_framework.
    """
    call_pats = _LOG_CALL_PATTERNS.get(ext, [])
    setup_pats = _LOG_SETUP_PATTERNS.get(ext, [])
    fw_pats = _LOG_FRAMEWORK_PATTERNS.get(ext, [])

    if not call_pats and not setup_pats:
        return []

    lines = content.splitlines()
    symbols: list[dict] = []

    # Detect framework
    log_framework = "unknown"
    for fw_name, fw_pat in fw_pats:
        if fw_pat.search(content):
            log_framework = fw_name
            break

    # Framework setup sites
    for setup_name, setup_pat in setup_pats:
        for lineno, line in enumerate(lines):
            if setup_pat.search(line):
                symbols.append({
                    "id": f"{file_path}::log_setup:{setup_name}:{lineno}",
                    "name": f"log_setup:{setup_name}",
                    "symbol_type": "utility",
                    "line_start": lineno,
                    "line_end": lineno,
                    "body_hash": _body_hash(lines, lineno, lineno),
                    "confidence": "high",
                    "exported": True,
                    "keywords": list(dict.fromkeys(
                        _name_keywords(setup_name) + [log_framework, "logging", "setup"]
                    ))[:10],
                })

    # Emission sites
    for level, pat in call_pats:
        for lineno, line in enumerate(lines):
            if pat.search(line):
                symbols.append({
                    "id": f"{file_path}::log:{level}:{lineno}",
                    "name": f"log:{level}",
                    "symbol_type": "utility",
                    "line_start": lineno,
                    "line_end": lineno,
                    "body_hash": _body_hash(lines, lineno, lineno),
                    "confidence": "high",
                    "exported": True,
                    "keywords": list(dict.fromkeys(
                        [level, "log", log_framework, "logging"]
                    ))[:10],
                })

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8E  SENTRY INSTRUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

_SENTRY_PATTERNS: dict[str, list[tuple[re.Pattern, str, str]]] = {
    # (pattern, kind, symbol_type)
    ".py": [
        (re.compile(r"sentry_sdk\.init\s*\("), "init", "utility"),
        (re.compile(r"sentry_sdk\.capture_exception\s*\("), "capture_exception", "hook"),
        (re.compile(r"sentry_sdk\.capture_message\s*\("), "capture_message", "hook"),
        (re.compile(r"@sentry_sdk\.trace"), "trace_decorator", "hook"),
        (re.compile(r"with\s+sentry_sdk\.start_transaction\s*\("), "start_transaction", "hook"),
    ],
    ".ts": [
        (re.compile(r"Sentry\.init\s*\("), "init", "utility"),
        (re.compile(r"Sentry\.captureException\s*\("), "captureException", "hook"),
        (re.compile(r"Sentry\.captureMessage\s*\("), "captureMessage", "hook"),
        (re.compile(r"Sentry\.startTransaction\s*\("), "startTransaction", "hook"),
        (re.compile(r"withSentryConfig\s*\("), "withSentryConfig", "utility"),
    ],
    ".js": [],   # filled below
    ".tsx": [],  # filled below
    ".jsx": [],  # filled below
    ".go": [
        (re.compile(r"sentry\.Init\s*\("), "init", "utility"),
        (re.compile(r"sentry\.CaptureException\s*\("), "CaptureException", "hook"),
        (re.compile(r"sentry\.StartSpan\s*\("), "StartSpan", "hook"),
    ],
    ".java": [
        (re.compile(r"Sentry\.init\s*\("), "init", "utility"),
        (re.compile(r"Sentry\.captureException\s*\("), "captureException", "hook"),
    ],
    ".kt": [],  # filled below
}
_SENTRY_PATTERNS[".js"] = _SENTRY_PATTERNS[".ts"]
_SENTRY_PATTERNS[".tsx"] = _SENTRY_PATTERNS[".ts"]
_SENTRY_PATTERNS[".jsx"] = _SENTRY_PATTERNS[".js"]
_SENTRY_PATTERNS[".kt"] = _SENTRY_PATTERNS[".java"]

# DSN extraction patterns
_SENTRY_DSN_PY = re.compile(r'dsn\s*=\s*["\']([^"\']+)["\']')
_SENTRY_DSN_JS = re.compile(r'dsn\s*:\s*["\']([^"\']+)["\']')
_SENTRY_TRANSACTION_JS = re.compile(r'name\s*:\s*["\']([^"\']+)["\']')
_SENTRY_SPAN_GO = re.compile(r'sentry\.StartSpan\s*\(\s*\w+\s*,\s*["\']([^"\']+)["\']')


def extract_sentry_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract Sentry SDK instrumentation call sites from source code.

    Detects Sentry SDK usage in Python (sentry-sdk), TypeScript/JavaScript (@sentry/*),
    Go (sentry-go), and Java/Kotlin.

    Returns symbol dicts with symbol_type='utility' (init calls) or 'hook' (capture/trace calls).
    """
    patterns = _SENTRY_PATTERNS.get(ext, [])
    if not patterns:
        return []

    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for lineno, line in enumerate(lines):
        for pat, kind, sym_type in patterns:
            if not pat.search(line):
                continue

            sym_id = f"{file_path}::sentry_{kind}_{lineno}"
            if sym_id in seen:
                continue
            seen.add(sym_id)

            # Extract extra keywords (dsn, transaction name, span name)
            extra_kw: list[str] = []
            if kind == "init":
                # Try to pull DSN value as a keyword hint
                dsn_pat = _SENTRY_DSN_PY if ext == ".py" else _SENTRY_DSN_JS
                m_dsn = dsn_pat.search(line)
                if m_dsn:
                    extra_kw.append("dsn")
            elif kind in ("startTransaction", "StartSpan"):
                m_name = _SENTRY_TRANSACTION_JS.search(line)
                if m_name:
                    extra_kw.extend(_name_keywords(m_name.group(1)))

            symbols.append({
                "id": sym_id,
                "name": f"sentry:{kind}",
                "symbol_type": sym_type,
                "line_start": lineno,
                "line_end": lineno,
                "body_hash": _body_hash(lines, lineno, lineno),
                "confidence": "high",
                "exported": False,
                "keywords": list(dict.fromkeys(
                    ["sentry", kind] + extra_kw
                ))[:10],
            })

    return symbols


_SENTRY_CONFIG_FILE_NAMES = re.compile(
    r"(?i)sentry\.(properties|ya?ml|json)$"
)
_SENTRY_INIT_PY = re.compile(r"sentry_sdk\.init\s*\(")
_SENTRY_INIT_JS = re.compile(r"Sentry\.init\s*\(")


def find_sentry_configs(project_root: str) -> list[str]:
    """
    Walk project_root and return paths to Sentry config files and source files
    that contain a Sentry.init / sentry_sdk.init call.
    """
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if _SENTRY_CONFIG_FILE_NAMES.search(fname):
                results.append(fpath)
                continue
            _, ext = os.path.splitext(fname.lower())
            if ext in OBSERVABILITY_SOURCE_EXTS:
                ok, content, _ = _safe_read(fpath)
                if ok and (_SENTRY_INIT_PY.search(content) or _SENTRY_INIT_JS.search(content)):
                    results.append(fpath)
    return sorted(results)


# ═══════════════════════════════════════════════════════════════════════════════
# 8F  DATADOG INSTRUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

_DATADOG_PATTERNS: dict[str, list[tuple[re.Pattern, str, str]]] = {
    ".py": [
        (re.compile(r"tracer\.trace\s*\("), "tracer_trace", "hook"),
        (re.compile(r"@tracer\.wrap\s*\("), "tracer_wrap", "hook"),
        (re.compile(r"DD_AGENT_HOST|DD_SERVICE|DD_ENV|DD_VERSION"), "datadog_config", "utility"),
        (re.compile(r"DogStatsD|statsd\.increment\s*\(|statsd\.gauge\s*\(|statsd\.histogram\s*\("), "statsd", "hook"),
        (re.compile(r"tracer\.configure\s*\("), "tracer_configure", "utility"),
    ],
    ".ts": [
        (re.compile(r"tracer\.init\s*\("), "tracer_init", "utility"),
        (re.compile(r"tracer\.startSpan\s*\("), "startSpan", "hook"),
        (re.compile(r"tracer\.trace\s*\("), "tracer_trace", "hook"),
    ],
    ".js": [],   # filled below
    ".tsx": [],  # filled below
    ".jsx": [],  # filled below
    ".go": [
        (re.compile(r"tracer\.Start\s*\("), "tracer_Start", "utility"),
        (re.compile(r"tracer\.StartSpan\s*\("), "StartSpan", "hook"),
        (re.compile(r"tracer\.StartSpanFromContext\s*\("), "StartSpanFromContext", "hook"),
    ],
    ".java": [
        (re.compile(r"GlobalTracer\.get\s*\(\s*\)\.buildSpan\s*\("), "buildSpan", "hook"),
        (re.compile(r"@Trace\b"), "trace_annotation", "hook"),
    ],
    ".kt": [],  # filled below
}
_DATADOG_PATTERNS[".js"] = _DATADOG_PATTERNS[".ts"]
_DATADOG_PATTERNS[".tsx"] = _DATADOG_PATTERNS[".ts"]
_DATADOG_PATTERNS[".jsx"] = _DATADOG_PATTERNS[".js"]
_DATADOG_PATTERNS[".kt"] = _DATADOG_PATTERNS[".java"]

_DD_OPERATION_PY = re.compile(r"tracer\.\w+\s*\(\s*['\"]([^'\"]+)['\"]")
_DD_SERVICE_JS = re.compile(r"service\s*:\s*['\"]([^'\"]+)['\"]")
_DD_SPAN_GO = re.compile(r"tracer\.Start\w*\s*\(\s*['\"]([^'\"]+)['\"]")


def extract_datadog_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract Datadog APM/tracing SDK instrumentation call sites from source code.

    Detects dd-trace (JS/TS), ddtrace (Python), and dd-trace-go (Go) usage.

    Returns symbol dicts with symbol_type='utility' (init/config calls) or 'hook' (span/trace calls).
    """
    patterns = _DATADOG_PATTERNS.get(ext, [])
    if not patterns:
        return []

    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    for lineno, line in enumerate(lines):
        for pat, kind, sym_type in patterns:
            if not pat.search(line):
                continue

            sym_id = f"{file_path}::dd_{kind}_{lineno}"
            if sym_id in seen:
                continue
            seen.add(sym_id)

            # Extract operation/service name as extra keyword
            extra_kw: list[str] = []
            if ext == ".py":
                m_op = _DD_OPERATION_PY.search(line)
                if m_op:
                    extra_kw.extend(_name_keywords(m_op.group(1)))
            elif ext in (".ts", ".js", ".tsx", ".jsx"):
                m_svc = _DD_SERVICE_JS.search(line)
                if m_svc:
                    extra_kw.extend(_name_keywords(m_svc.group(1)))
            elif ext == ".go":
                m_span = _DD_SPAN_GO.search(line)
                if m_span:
                    extra_kw.extend(_name_keywords(m_span.group(1)))

            symbols.append({
                "id": sym_id,
                "name": f"dd:{kind}",
                "symbol_type": sym_type,
                "line_start": lineno,
                "line_end": lineno,
                "body_hash": _body_hash(lines, lineno, lineno),
                "confidence": "high",
                "exported": False,
                "keywords": list(dict.fromkeys(
                    ["datadog", "dd", kind] + extra_kw
                ))[:10],
            })

    return symbols


_DD_CONFIG_FILE_NAMES = re.compile(
    r"(?i)(datadog|ddconfig)\.(ya?ml)$"
)
_DD_INIT_JS = re.compile(r"tracer\.init\s*\(")
_DD_START_GO = re.compile(r"tracer\.Start\s*\(")


def find_datadog_configs(project_root: str) -> list[str]:
    """
    Walk project_root and return paths to Datadog config files and source files
    that contain a tracer.init (dd-trace JS) or tracer.Start (Go) call.
    """
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if _DD_CONFIG_FILE_NAMES.search(fname):
                results.append(fpath)
                continue
            _, ext = os.path.splitext(fname.lower())
            if ext in OBSERVABILITY_SOURCE_EXTS:
                ok, content, _ = _safe_read(fpath)
                if ok and (_DD_INIT_JS.search(content) or _DD_START_GO.search(content)):
                    results.append(fpath)
    return sorted(results)


# ═══════════════════════════════════════════════════════════════════════════════
# 8G  NEW RELIC INSTRUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_newrelic_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract New Relic APM instrumentation from Python, Node.js, Go, and Java source files,
    as well as New Relic config files (newrelic.ini, newrelic.yml).

    Returns symbol dicts with symbol_type='utility' (imports/config) or 'hook' (trace calls).
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()
    fname = os.path.basename(file_path)

    def _add(lineno: int, name: str, sym_type: str, extra_kw: list[str] | None = None) -> None:
        sym_id = f"{file_path}::nr_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        kw = list(dict.fromkeys(["newrelic"] + _name_keywords(name) + (extra_kw or [])))[:10]
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": False,
            "keywords": kw,
        })

    # Config file detection
    if fname.lower() in ("newrelic.ini", "newrelic.yml", "newrelic.yaml"):
        _add(0, "newrelic:config", "utility")
        return symbols

    if ext == ".py":
        _NR_PY_IMPORT = re.compile(r"import newrelic\.agent")
        _NR_PY_FUNC_TRACE = re.compile(r"@newrelic\.agent\.function_trace\s*\(")
        _NR_PY_BG_TASK = re.compile(r"@newrelic\.agent\.background_task\s*\(")
        _NR_PY_CUSTOM_EVENT = re.compile(r"newrelic\.agent\.record_custom_event\s*\(")
        _NR_PY_NOTICE_ERROR = re.compile(r"newrelic\.agent\.notice_error\s*\(")

        for lineno, line in enumerate(lines):
            if _NR_PY_IMPORT.search(line):
                _add(lineno, "newrelic:agent", "utility")
            if _NR_PY_FUNC_TRACE.search(line):
                # Look ahead for the function name on the next def line
                func_name = ""
                for j in range(lineno + 1, min(lineno + 5, len(lines))):
                    m = re.match(r"\s*def\s+(\w+)", lines[j])
                    if m:
                        func_name = m.group(1)
                        break
                _add(lineno, f"newrelic:function_trace:{func_name}" if func_name else "newrelic:function_trace", "hook")
            if _NR_PY_BG_TASK.search(line):
                _add(lineno, "newrelic:background_task", "hook")
            if _NR_PY_CUSTOM_EVENT.search(line):
                _add(lineno, "newrelic:record_custom_event", "hook")
            if _NR_PY_NOTICE_ERROR.search(line):
                _add(lineno, "newrelic:notice_error", "hook")

    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        _NR_JS_REQUIRE = re.compile(r"require\s*\(\s*['\"]newrelic['\"]\s*\)")
        _NR_JS_WEB_TXN = re.compile(r"newrelic\.startWebTransaction\s*\(")
        _NR_JS_CUSTOM_EVENT = re.compile(r"newrelic\.recordCustomEvent\s*\(")
        _NR_JS_NOTICE_ERROR = re.compile(r"newrelic\.noticeError\s*\(")
        _NR_JS_CUSTOM_ATTR = re.compile(r"newrelic\.addCustomAttribute\s*\(")

        for lineno, line in enumerate(lines):
            if _NR_JS_REQUIRE.search(line):
                _add(lineno, "newrelic:agent", "utility")
            if _NR_JS_WEB_TXN.search(line):
                _add(lineno, "newrelic:startWebTransaction", "hook")
            if _NR_JS_CUSTOM_EVENT.search(line):
                _add(lineno, "newrelic:recordCustomEvent", "hook")
            if _NR_JS_NOTICE_ERROR.search(line):
                _add(lineno, "newrelic:noticeError", "hook")
            if _NR_JS_CUSTOM_ATTR.search(line):
                _add(lineno, "newrelic:addCustomAttribute", "hook")

    elif ext == ".go":
        _NR_GO_IMPORT = re.compile(r'"github\.com/newrelic/go-agent')
        _NR_GO_NEW_APP = re.compile(r"newrelic\.NewApplication\s*\(")
        _NR_GO_START_TXN = re.compile(r"app\.StartTransaction\s*\(")
        _NR_GO_NOTICE_ERR = re.compile(r"txn\.NoticeError\s*\(")

        for lineno, line in enumerate(lines):
            if _NR_GO_IMPORT.search(line):
                _add(lineno, "newrelic:agent", "utility")
            if _NR_GO_NEW_APP.search(line):
                _add(lineno, "newrelic:NewApplication", "utility")
            if _NR_GO_START_TXN.search(line):
                _add(lineno, "newrelic:StartTransaction", "hook")
            if _NR_GO_NOTICE_ERR.search(line):
                _add(lineno, "newrelic:NoticeError", "hook")

    elif ext in (".java", ".kt"):
        _NR_JAVA_IMPORT = re.compile(r"import com\.newrelic\.api\.agent\.")
        _NR_JAVA_TRACE_DISPATCHER = re.compile(r"@Trace\s*\(\s*dispatcher\s*=\s*true")
        _NR_JAVA_TRACE = re.compile(r"@Trace\b")
        _NR_JAVA_NOTICE_ERR = re.compile(r"NewRelic\.noticeError\s*\(")
        _NR_JAVA_CUSTOM_PARAM = re.compile(r"NewRelic\.addCustomParameter\s*\(")

        for lineno, line in enumerate(lines):
            if _NR_JAVA_IMPORT.search(line):
                _add(lineno, "newrelic:agent", "utility")
            if _NR_JAVA_TRACE_DISPATCHER.search(line):
                _add(lineno, "newrelic:Trace:dispatcher", "hook")
            elif _NR_JAVA_TRACE.search(line):
                _add(lineno, "newrelic:Trace", "hook")
            if _NR_JAVA_NOTICE_ERR.search(line):
                _add(lineno, "newrelic:noticeError", "hook")
            if _NR_JAVA_CUSTOM_PARAM.search(line):
                _add(lineno, "newrelic:addCustomParameter", "hook")

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8H  DYNATRACE INSTRUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_dynatrace_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract Dynatrace OneAgent SDK instrumentation from Python, Node.js, and Java source files,
    and detect Dynatrace config files (dtconfig.properties, ruxitagentproc.conf).

    Returns symbol dicts with symbol_type='utility' (imports/config) or 'hook' (trace calls).
    Note: Java @Trace annotation is attributed to Dynatrace only when com.dynatrace imports are present.
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()
    fname = os.path.basename(file_path)

    def _add(lineno: int, name: str, sym_type: str) -> None:
        sym_id = f"{file_path}::dt_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": False,
            "keywords": list(dict.fromkeys(["dynatrace"] + _name_keywords(name)))[:10],
        })

    # Config file detection
    if fname.lower() in ("dtconfig.properties", "ruxitagentproc.conf"):
        _add(0, "dynatrace:config", "utility")
        return symbols

    if ext == ".py":
        _DT_PY_IMPORT = re.compile(r"import oneagent|from oneagent import")
        _DT_PY_GET_SDK = re.compile(r"oneagent\.get_sdk\s*\(")
        _DT_PY_INCOMING = re.compile(r"with\s+sdk\.trace_incoming_remote_call\s*\(")
        _DT_PY_OUTGOING = re.compile(r"with\s+sdk\.trace_outgoing_remote_call\s*\(")
        _DT_PY_DB_INFO = re.compile(r"sdk\.create_database_info\s*\(")

        for lineno, line in enumerate(lines):
            if _DT_PY_IMPORT.search(line):
                _add(lineno, "dynatrace:oneagent", "utility")
            if _DT_PY_GET_SDK.search(line):
                _add(lineno, "dynatrace:get_sdk", "utility")
            if _DT_PY_INCOMING.search(line):
                _add(lineno, "dynatrace:trace_incoming_remote_call", "hook")
            if _DT_PY_OUTGOING.search(line):
                _add(lineno, "dynatrace:trace_outgoing_remote_call", "hook")
            if _DT_PY_DB_INFO.search(line):
                _add(lineno, "dynatrace:create_database_info", "hook")

    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        _DT_JS_REQUIRE = re.compile(r"require\s*\(\s*['\"]@dynatrace/oneagent-sdk['\"]\s*\)")
        _DT_JS_CREATE = re.compile(r"Sdk\.createInstance\s*\(")
        _DT_JS_INCOMING = re.compile(r"api\.createIncomingRemoteCallTracer\s*\(")

        for lineno, line in enumerate(lines):
            if _DT_JS_REQUIRE.search(line):
                _add(lineno, "dynatrace:oneagent-sdk", "utility")
            if _DT_JS_CREATE.search(line):
                _add(lineno, "dynatrace:createInstance", "utility")
            if _DT_JS_INCOMING.search(line):
                _add(lineno, "dynatrace:createIncomingRemoteCallTracer", "hook")

    elif ext in (".java", ".kt"):
        # Disambiguate from NR: only emit Dynatrace symbols when com.dynatrace is present
        has_dt_import = bool(re.search(r"import com\.dynatrace\.oneagent\.sdk\.", content))
        if not has_dt_import:
            return symbols

        _DT_JAVA_IMPORT = re.compile(r"import com\.dynatrace\.oneagent\.sdk\.")
        _DT_JAVA_TRACE_SAME = re.compile(r"@TraceSameTransaction\b")
        _DT_JAVA_ADD_ATTR = re.compile(r"@AddRequestAttribute\b")

        for lineno, line in enumerate(lines):
            if _DT_JAVA_IMPORT.search(line):
                _add(lineno, "dynatrace:oneagent-sdk", "utility")
            if _DT_JAVA_TRACE_SAME.search(line):
                _add(lineno, "dynatrace:TraceSameTransaction", "hook")
            if _DT_JAVA_ADD_ATTR.search(line):
                _add(lineno, "dynatrace:AddRequestAttribute", "hook")

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8I  HONEYCOMB INSTRUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_honeycomb_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract Honeycomb Beeline and OTel instrumentation from Python, Node.js, and Go source files.

    Returns symbol dicts with symbol_type='utility' (setup calls) or 'hook' (trace decorators).
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    def _add(lineno: int, name: str, sym_type: str) -> None:
        sym_id = f"{file_path}::hc_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": False,
            "keywords": list(dict.fromkeys(["honeycomb"] + _name_keywords(name)))[:10],
        })

    if ext == ".py":
        _HC_PY_IMPORT_BEELINE = re.compile(r"import beeline")
        _HC_PY_BEELINE_INIT = re.compile(r"beeline\.init\s*\(\s*writekey\s*=")
        _HC_PY_BEELINE_TRACED = re.compile(r"@beeline\.traced\b")
        _HC_PY_OTEL_IMPORT = re.compile(r"from honeycomb\.opentelemetry import")
        _HC_PY_CONFIGURE_OTEL = re.compile(r"configure_opentelemetry\s*\(")

        for lineno, line in enumerate(lines):
            if _HC_PY_IMPORT_BEELINE.search(line):
                _add(lineno, "honeycomb:beeline", "utility")
            if _HC_PY_BEELINE_INIT.search(line):
                _add(lineno, "honeycomb:beeline.init", "utility")
            if _HC_PY_BEELINE_TRACED.search(line):
                _add(lineno, "honeycomb:beeline.traced", "hook")
            if _HC_PY_OTEL_IMPORT.search(line):
                _add(lineno, "honeycomb:opentelemetry", "utility")
            if _HC_PY_CONFIGURE_OTEL.search(line):
                _add(lineno, "honeycomb:configure_opentelemetry", "utility")

    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        _HC_JS_BEELINE = re.compile(r"require\s*\(\s*['\"]honeycomb-beeline['\"]\s*\)")
        _HC_JS_LIBHONEY = re.compile(r"require\s*\(\s*['\"]libhoney['\"]\s*\)")
        _HC_JS_CONFIGURE = re.compile(r"beeline\.configure\s*\(\s*\{")
        _HC_JS_OTEL = re.compile(r"['\"]@honeycombio/opentelemetry-node['\"]")
        _HC_JS_WEB_SDK = re.compile(r"HoneycombWebSDK\b")

        for lineno, line in enumerate(lines):
            if _HC_JS_BEELINE.search(line):
                _add(lineno, "honeycomb:beeline", "utility")
            if _HC_JS_LIBHONEY.search(line):
                _add(lineno, "honeycomb:libhoney", "utility")
            if _HC_JS_CONFIGURE.search(line):
                _add(lineno, "honeycomb:beeline.configure", "utility")
            if _HC_JS_OTEL.search(line):
                _add(lineno, "honeycomb:opentelemetry-node", "utility")
            if _HC_JS_WEB_SDK.search(line):
                _add(lineno, "honeycomb:HoneycombWebSDK", "utility")

    elif ext == ".go":
        _HC_GO_IMPORT = re.compile(r'"github\.com/honeycombio/beeline-go"')
        _HC_GO_INIT = re.compile(r"beeline\.Init\s*\(")

        for lineno, line in enumerate(lines):
            if _HC_GO_IMPORT.search(line):
                _add(lineno, "honeycomb:beeline-go", "utility")
            if _HC_GO_INIT.search(line):
                _add(lineno, "honeycomb:beeline.Init", "utility")

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8J  ALERTMANAGER CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def extract_alertmanager_config(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract AlertManager configuration symbols from alertmanager.yml/yaml files.

    Detects receivers (as hooks), inhibit_rules (as utility), and route receiver references.
    Only runs on files named alertmanager.yml/.yaml or living under an alertmanager/ directory.
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    def _add(lineno: int, name: str, sym_type: str, kw: list[str]) -> None:
        sym_id = f"{file_path}::am_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": True,
            "keywords": list(dict.fromkeys(["alertmanager"] + kw))[:10],
        })

    # Top-level receivers: block → emit a config utility symbol
    if re.search(r"^receivers\s*:", content, re.MULTILINE):
        _add(0, "alertmanager:config", "utility", ["receivers", "config"])

    # inhibit_rules: block
    if re.search(r"^inhibit_rules\s*:", content, re.MULTILINE):
        inhibit_line = next(
            (i for i, ln in enumerate(lines) if re.match(r"^inhibit_rules\s*:", ln)), 0
        )
        _add(inhibit_line, "alertmanager:inhibit", "utility", ["inhibit", "rules"])

    # Per-receiver symbols
    in_receivers = False
    for lineno, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^receivers\s*:", line):
            in_receivers = True
            continue
        # End of receivers block (top-level key)
        if in_receivers and re.match(r"^[A-Za-z]", line) and not stripped.startswith("-"):
            in_receivers = False

        if in_receivers:
            m = re.match(r"^\s*-\s*name\s*:\s*[\"']?([^\"'\n]+)[\"']?", line)
            if m:
                rname = m.group(1).strip()
                kw = ["receiver", rname]
                # Peek ahead for notification type keywords
                for j in range(lineno + 1, min(lineno + 20, len(lines))):
                    nxt = lines[j].strip()
                    if nxt.startswith("- name:"):
                        break
                    if "slack_configs:" in nxt:
                        kw.append("slack")
                    if "pagerduty_configs:" in nxt:
                        kw.append("pagerduty")
                    if "webhook_configs:" in nxt:
                        kw.append("webhook")
                _add(lineno, f"receiver:{rname}", "hook", kw)

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8K  LOKI CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def extract_loki_config(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract Loki server config, Promtail config, and Loki datasource references from
    Grafana dashboard JSON files.

    Loki server: loki.yml/yaml, loki-config.yml, or path contains /loki/
    Promtail: promtail.yml/yaml
    Grafana JSON: "type": "loki" datasource references and LogQL expressions.
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()
    fname = os.path.basename(file_path).lower()

    def _add(lineno: int, name: str, sym_type: str, kw: list[str]) -> None:
        sym_id = f"{file_path}::loki_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": True,
            "keywords": list(dict.fromkeys(["loki"] + kw))[:10],
        })

    # Loki server config
    is_loki_server = (
        re.match(r"loki[-.]?(config)?\.ya?ml$", fname)
        or "/loki/" in file_path.replace("\\", "/")
    )
    # Promtail config
    is_promtail = bool(re.match(r"promtail\.ya?ml$", fname))

    if is_loki_server and ext in (".yaml", ".yml"):
        # http_listen_port
        for lineno, line in enumerate(lines):
            m = re.match(r"\s*http_listen_port\s*:\s*(\d+)", line)
            if m:
                _add(lineno, "loki:server", "utility", ["server", "port", m.group(1)])
                break
        # ingester:
        for lineno, line in enumerate(lines):
            if re.match(r"^ingester\s*:", line):
                _add(lineno, "loki:ingester", "utility", ["ingester"])
                break

    elif is_promtail and ext in (".yaml", ".yml"):
        # clients: url: ...loki
        in_clients = False
        for lineno, line in enumerate(lines):
            if re.match(r"^clients\s*:", line):
                in_clients = True
                continue
            if in_clients and re.match(r"^[A-Za-z]", line) and not line.strip().startswith("-"):
                in_clients = False
            if in_clients and re.search(r"url\s*:.*loki", line, re.IGNORECASE):
                _add(lineno, "promtail:client", "utility", ["promtail", "client"])
                in_clients = False

    elif ext == ".json":
        # Grafana dashboard: Loki datasource references
        for lineno, line in enumerate(lines):
            if re.search(r'"type"\s*:\s*"loki"', line):
                _add(lineno, "loki:datasource", "utility", ["datasource", "grafana"])
            # LogQL: look for label selector syntax in expression values
            if re.search(r'\{[^}]*\w+="[^"]*"[^}]*\}', line):
                _add(lineno, "loki:logql", "hook", ["logql", "query"])

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8L  JAEGER INSTRUMENTATION (direct, non-OTel)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_jaeger_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract direct (non-OTel) Jaeger client instrumentation from Python, Go, and Node.js files.

    Returns symbol dicts with symbol_type='utility' (setup) for tracer initialisation.
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    def _add(lineno: int, name: str, sym_type: str, extra_kw: list[str] | None = None) -> None:
        sym_id = f"{file_path}::jaeger_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": False,
            "keywords": list(dict.fromkeys(["jaeger"] + _name_keywords(name) + (extra_kw or [])))[:10],
        })

    if ext == ".py":
        _J_PY_IMPORT = re.compile(r"from jaeger_client import Config")
        _J_PY_CONFIG = re.compile(r"Config\s*\(\s*config\s*=\s*\{")
        _J_PY_SERVICE = re.compile(r"service_name\s*=\s*['\"]([^'\"]+)['\"]")
        _J_PY_INIT = re.compile(r"config\.initialize_tracer\s*\(")

        for lineno, line in enumerate(lines):
            if _J_PY_IMPORT.search(line):
                _add(lineno, "jaeger:Config", "utility")
            if _J_PY_CONFIG.search(line):
                # Try to extract service_name on the same or nearby lines
                svc = ""
                for j in range(lineno, min(lineno + 10, len(lines))):
                    m_svc = _J_PY_SERVICE.search(lines[j])
                    if m_svc:
                        svc = m_svc.group(1)
                        break
                _add(lineno, f"jaeger:{svc}" if svc else "jaeger:Config", "utility", [svc] if svc else [])
            if _J_PY_INIT.search(line):
                _add(lineno, "jaeger:initialize_tracer", "utility")

    elif ext == ".go":
        _J_GO_IMPORT = re.compile(r'"github\.com/uber/jaeger-client-go')
        _J_GO_CONFIG = re.compile(r"jaegercfg\.Configuration\s*\{")
        _J_GO_NEW_TRACER = re.compile(r"cfg\.NewTracer\s*\(")
        _J_GO_SERVICE = re.compile(r'ServiceName\s*:\s*["\']([^"\']+)["\']')

        for lineno, line in enumerate(lines):
            if _J_GO_IMPORT.search(line):
                _add(lineno, "jaeger:go-agent", "utility")
            if _J_GO_CONFIG.search(line):
                svc = ""
                for j in range(lineno, min(lineno + 10, len(lines))):
                    m_svc = _J_GO_SERVICE.search(lines[j])
                    if m_svc:
                        svc = m_svc.group(1)
                        break
                _add(lineno, f"jaeger:{svc}" if svc else "jaeger:Configuration", "utility", [svc] if svc else [])
            if _J_GO_NEW_TRACER.search(line):
                _add(lineno, "jaeger:NewTracer", "utility")

    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        _J_JS_REQUIRE = re.compile(r"require\s*\(\s*['\"]jaeger-client['\"]\s*\)")
        _J_JS_INIT = re.compile(r"initTracer\s*\(")

        for lineno, line in enumerate(lines):
            if _J_JS_REQUIRE.search(line):
                _add(lineno, "jaeger:client", "utility")
            if _J_JS_INIT.search(line):
                _add(lineno, "jaeger:initTracer", "utility")

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 8M  ZIPKIN INSTRUMENTATION (direct, non-OTel)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_zipkin_instrumentation(content: str, file_path: str, ext: str) -> list[dict]:
    """
    Extract direct (non-OTel) Zipkin client instrumentation from Python and Node.js files.

    Returns symbol dicts with symbol_type='utility' (setup) or 'hook' (span decorators).
    """
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    def _add(lineno: int, name: str, sym_type: str, extra_kw: list[str] | None = None) -> None:
        sym_id = f"{file_path}::zipkin_{name}_{lineno}"
        if sym_id in seen:
            return
        seen.add(sym_id)
        symbols.append({
            "id": sym_id,
            "name": name,
            "symbol_type": sym_type,
            "line_start": lineno,
            "line_end": lineno,
            "body_hash": _body_hash(lines, lineno, lineno),
            "confidence": "high",
            "exported": False,
            "keywords": list(dict.fromkeys(["zipkin"] + _name_keywords(name) + (extra_kw or [])))[:10],
        })

    if ext == ".py":
        _Z_PY_IMPORT = re.compile(r"from py_zipkin\.zipkin import zipkin_span")
        _Z_PY_DECORATOR = re.compile(r"@zipkin_span\s*\(\s*service_name\s*=\s*[\"']([^\"']+)[\"']")

        for lineno, line in enumerate(lines):
            if _Z_PY_IMPORT.search(line):
                _add(lineno, "zipkin:py_zipkin", "utility")
            m_dec = _Z_PY_DECORATOR.search(line)
            if m_dec:
                svc = m_dec.group(1)
                _add(lineno, f"zipkin:span:{svc}", "hook", [svc])

    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        _Z_JS_REQUIRE = re.compile(r"require\s*\(\s*['\"]zipkin['\"]\s*\)")
        _Z_JS_TRACER = re.compile(r"new\s+Tracer\s*\(\s*\{")
        _Z_JS_BATCH = re.compile(r"BatchRecorder\b")
        _Z_JS_TRANSPORT = re.compile(r"require\s*\(\s*['\"]zipkin-transport-http['\"]\s*\)")

        has_tracer = False
        has_batch = False
        for lineno, line in enumerate(lines):
            if _Z_JS_REQUIRE.search(line):
                _add(lineno, "zipkin:client", "utility")
            if _Z_JS_TRACER.search(line):
                has_tracer = True
            if _Z_JS_BATCH.search(line):
                has_batch = True
            if has_tracer and has_batch:
                _add(lineno, "zipkin:Tracer", "utility")
                has_tracer = False
                has_batch = False
            if _Z_JS_TRANSPORT.search(line):
                _add(lineno, "zipkin:transport-http", "utility")

    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
# MCP HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_newrelic_coverage(project_root: str) -> dict:
    """Return New Relic instrumentation summary for project."""
    hooks: list[dict] = []
    utilities: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            _, ext = os.path.splitext(fname.lower())
            ok, content, _ = _safe_read(fpath)
            if not ok:
                continue
            syms = extract_newrelic_instrumentation(content, fpath, ext)
            for s in syms:
                if s["symbol_type"] == "hook":
                    hooks.append(s)
                else:
                    utilities.append(s)

    return {
        "ok": True,
        "hook_count": len(hooks),
        "utility_count": len(utilities),
        "hooks": hooks,
        "utilities": utilities,
    }


def get_apm_coverage(project_root: str) -> dict:
    """
    Return combined APM coverage: which tools are instrumented.

    Covers: New Relic, Dynatrace, Honeycomb, Jaeger, Zipkin, Datadog, Sentry.
    """
    counts: dict[str, int] = {
        "newrelic": 0, "dynatrace": 0, "honeycomb": 0,
        "jaeger": 0, "zipkin": 0, "datadog": 0, "sentry": 0,
    }
    extractors = {
        "newrelic": extract_newrelic_instrumentation,
        "dynatrace": extract_dynatrace_instrumentation,
        "honeycomb": extract_honeycomb_instrumentation,
        "jaeger": extract_jaeger_instrumentation,
        "zipkin": extract_zipkin_instrumentation,
        "datadog": extract_datadog_instrumentation,
        "sentry": extract_sentry_instrumentation,
    }

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            _, ext = os.path.splitext(fname.lower())
            if ext not in OBSERVABILITY_SOURCE_EXTS:
                continue
            ok, content, _ = _safe_read(fpath)
            if not ok:
                continue
            for tool, fn in extractors.items():
                syms = fn(content, fpath, ext)
                counts[tool] += len(syms)

    active_tools = [t for t, c in counts.items() if c > 0]
    return {
        "ok": True,
        "tools_detected": active_tools,
        "symbol_counts": counts,
        "has_apm": len(active_tools) > 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPATCHERS
# ═══════════════════════════════════════════════════════════════════════════════

def extract_observability_symbols(content: str, file_path: str) -> list[dict]:
    """
    Main dispatcher — extract all observability symbols from any file.

    Routing:
    - .yaml / .yml  → otel config or prometheus config (by filename/content)
    - .json         → grafana dashboard (by filename/content)
    - source files  → otel instrumentation + log patterns
    """
    _, ext = os.path.splitext(file_path.lower())
    symbols: list[dict] = []

    if ext in (".yaml", ".yml"):
        if _OTEL_FILENAME_PAT.search(file_path) or _OTEL_CONTENT_HINTS.search(content[:512]):
            result = parse_otel_config(file_path)
            symbols.extend(result.get("symbols", []))
        elif (_PROMETHEUS_FILENAME_PAT.search(os.path.basename(file_path))
              or _PROMETHEUS_CONTENT_HINTS.search(content[:512])):
            result = parse_prometheus_alerts(file_path)
            symbols.extend(result.get("symbols", []))

    elif ext == ".json":
        if (_GRAFANA_FILENAME_PAT.search(os.path.basename(file_path))
                or _GRAFANA_CONTENT_HINTS.search(content[:512])):
            result = parse_grafana_dashboard(file_path)
            symbols.extend(result.get("symbols", []))

    elif ext in OBSERVABILITY_SOURCE_EXTS:
        symbols.extend(extract_otel_instrumentation(content, file_path, ext))
        symbols.extend(extract_log_patterns(content, file_path, ext))
        symbols.extend(extract_sentry_instrumentation(content, file_path, ext))
        symbols.extend(extract_datadog_instrumentation(content, file_path, ext))

        # New Relic
        if any(s in content for s in ['newrelic.agent', "require('newrelic')", 'newrelic.NewApplication', 'com.newrelic']):
            symbols.extend(extract_newrelic_instrumentation(content, file_path, ext))

        # Dynatrace
        if any(s in content for s in ['oneagent', 'dynatrace/oneagent-sdk', 'com.dynatrace']):
            symbols.extend(extract_dynatrace_instrumentation(content, file_path, ext))

        # Honeycomb
        if any(s in content for s in ['beeline', 'honeycomb', 'HONEYCOMB_API_KEY', 'libhoney']):
            symbols.extend(extract_honeycomb_instrumentation(content, file_path, ext))

        # Jaeger
        if any(s in content for s in ['jaeger_client', 'jaeger-client-go', "require('jaeger-client')"]):
            symbols.extend(extract_jaeger_instrumentation(content, file_path, ext))

        # Zipkin
        if any(s in content for s in ['py_zipkin', "require('zipkin')", 'zipkin-transport']):
            symbols.extend(extract_zipkin_instrumentation(content, file_path, ext))

    # AlertManager (config files)
    fname = os.path.basename(file_path)
    if 'alertmanager' in fname.lower() or 'alertmanager' in file_path.lower():
        symbols.extend(extract_alertmanager_config(content, file_path, ext))

    # Loki (config + promtail)
    if any(s in fname.lower() for s in ['loki', 'promtail']) or '/loki/' in file_path.replace('\\', '/'):
        symbols.extend(extract_loki_config(content, file_path, ext))
    if ext == '.json' and '"type": "loki"' in content:
        symbols.extend(extract_loki_config(content, file_path, ext))

    return symbols


def parse_observability_config(file_path: str) -> dict:
    """
    Parse any observability config file (otel, prometheus, grafana).

    Auto-detects file type from name and content.
    Returns the result dict from the appropriate specialised parser,
    always including at minimum {ok, file, symbols}.
    """
    ok, content, _ = _safe_read(file_path)
    if not ok:
        return {"ok": False, "file": file_path, "symbols": []}

    basename = os.path.basename(file_path)
    _, ext = os.path.splitext(file_path.lower())
    snippet = content[:512]

    if ext in (".yaml", ".yml"):
        if _OTEL_FILENAME_PAT.search(file_path) or _OTEL_CONTENT_HINTS.search(snippet):
            return parse_otel_config(file_path)
        if (_PROMETHEUS_FILENAME_PAT.search(basename)
                or _PROMETHEUS_CONTENT_HINTS.search(snippet)):
            result = parse_prometheus_alerts(file_path)
            # Also try scrape_configs (prometheus.yml)
            if re.search(r"scrape_configs\s*:", content):
                scrape = parse_prometheus_scrape_config(file_path)
                result["scrape_jobs"] = scrape.get("jobs", [])
            return result

    elif ext == ".json":
        if (_GRAFANA_FILENAME_PAT.search(basename)
                or _GRAFANA_CONTENT_HINTS.search(snippet)):
            return parse_grafana_dashboard(file_path)

    # Generic fallback
    symbols = extract_observability_symbols(content, file_path)
    return {"ok": True, "file": file_path, "symbols": symbols}


# ═══════════════════════════════════════════════════════════════════════════════
# COVERAGE SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

def get_observability_summary(project_root: str) -> dict:
    """
    Scan project_root for observability coverage and return a summary.

    Coverage score (0-100) heuristic:
        +20  any OTel config found
        +20  any Prometheus alerts found
        +10  Grafana dashboards found
        +10  span_count > 0
        +10  metric_count > 0
        +10  instrumented_files > 0
        +10  log patterns in source files
        +10  scrape jobs configured
    """
    otel_configs = find_otel_configs(project_root)
    prometheus_files = find_prometheus_configs(project_root)

    prometheus_alert_count = 0
    scrape_job_count = 0
    for pf in prometheus_files:
        _, content, _ = _safe_read(pf)
        if "scrape_configs" in content:
            sc = parse_prometheus_scrape_config(pf)
            scrape_job_count += len(sc.get("jobs", []))
        result = parse_prometheus_alerts(pf)
        prometheus_alert_count += len(result.get("alerts", []))

    # Grafana dashboards
    grafana_dashboards: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            if _GRAFANA_FILENAME_PAT.search(fname):
                grafana_dashboards.append(os.path.join(dirpath, fname))

    # Source file instrumentation scan
    span_count = 0
    metric_count = 0
    log_file_count = 0
    instrumented_files = 0
    sentry_init_files = 0
    sentry_total_files = 0
    datadog_init_files = 0
    datadog_total_files = 0
    newrelic_hooks = 0
    dynatrace_hooks = 0
    honeycomb_hooks = 0
    jaeger_hooks = 0
    zipkin_hooks = 0
    has_alertmanager = False
    has_loki = False

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            _, ext = os.path.splitext(fname.lower())
            fpath = os.path.join(dirpath, fname)
            ok, content, _ = _safe_read(fpath)
            if not ok:
                continue

            # AlertManager config files
            if 'alertmanager' in fname.lower() or 'alertmanager' in fpath.lower():
                am_syms = extract_alertmanager_config(content, fpath, ext)
                if am_syms:
                    has_alertmanager = True

            # Loki config files and Grafana dashboards with Loki datasource
            if (any(s in fname.lower() for s in ['loki', 'promtail'])
                    or '/loki/' in fpath.replace('\\', '/')):
                loki_syms = extract_loki_config(content, fpath, ext)
                if loki_syms:
                    has_loki = True
            if ext == '.json' and '"type": "loki"' in content:
                loki_syms = extract_loki_config(content, fpath, ext)
                if loki_syms:
                    has_loki = True

            if ext not in OBSERVABILITY_SOURCE_EXTS:
                continue

            otel_syms = extract_otel_instrumentation(content, fpath, ext)
            log_syms = extract_log_patterns(content, fpath, ext)
            sentry_syms = extract_sentry_instrumentation(content, fpath, ext)
            dd_syms = extract_datadog_instrumentation(content, fpath, ext)

            file_spans = sum(1 for s in otel_syms if s["symbol_type"] == "hook")
            file_metrics = sum(1 for s in otel_syms if s["symbol_type"] == "use_case")
            span_count += file_spans
            metric_count += file_metrics

            if otel_syms:
                instrumented_files += 1
            if log_syms:
                log_file_count += 1

            if sentry_syms:
                sentry_total_files += 1
                if any("init" in s["name"] for s in sentry_syms):
                    sentry_init_files += 1

            if dd_syms:
                datadog_total_files += 1
                if any(s["symbol_type"] == "utility" for s in dd_syms):
                    datadog_init_files += 1

            # New APM tools
            if any(s in content for s in ['newrelic.agent', "require('newrelic')", 'newrelic.NewApplication', 'com.newrelic']):
                nr_syms = extract_newrelic_instrumentation(content, fpath, ext)
                newrelic_hooks += sum(1 for s in nr_syms if s["symbol_type"] == "hook")

            if any(s in content for s in ['oneagent', 'dynatrace/oneagent-sdk', 'com.dynatrace']):
                dt_syms = extract_dynatrace_instrumentation(content, fpath, ext)
                dynatrace_hooks += sum(1 for s in dt_syms if s["symbol_type"] == "hook")

            if any(s in content for s in ['beeline', 'honeycomb', 'HONEYCOMB_API_KEY', 'libhoney']):
                hc_syms = extract_honeycomb_instrumentation(content, fpath, ext)
                honeycomb_hooks += len(hc_syms)

            if any(s in content for s in ['jaeger_client', 'jaeger-client-go', "require('jaeger-client')"]):
                j_syms = extract_jaeger_instrumentation(content, fpath, ext)
                jaeger_hooks += len(j_syms)

            if any(s in content for s in ['py_zipkin', "require('zipkin')", 'zipkin-transport']):
                z_syms = extract_zipkin_instrumentation(content, fpath, ext)
                zipkin_hooks += len(z_syms)

    # Coverage score
    score = 0
    if otel_configs:
        score += 20
    if prometheus_alert_count > 0:
        score += 20
    if grafana_dashboards:
        score += 10
    if span_count > 0:
        score += 10
    if metric_count > 0:
        score += 10
    if instrumented_files > 0:
        score += 10
    if log_file_count > 0:
        score += 10
    if scrape_job_count > 0:
        score += 10
    if sentry_init_files > 0:
        score += 5
    if datadog_init_files > 0:
        score += 5
    # New APM tools: +5 each
    if newrelic_hooks > 0:
        score += 5
    if dynatrace_hooks > 0:
        score += 5
    if honeycomb_hooks > 0:
        score += 5
    # Infrastructure tools: +3 each
    if has_alertmanager:
        score += 3
    if has_loki:
        score += 3

    # Recommendations
    recommendations: list[str] = []
    if not otel_configs:
        recommendations.append(
            "No OpenTelemetry collector config found. Add otel-collector-config.yaml "
            "to centralise trace/metric/log routing."
        )
    if span_count == 0:
        recommendations.append(
            "No OTel span instrumentation detected in source files. "
            "Add tracer.start_as_current_span / startActiveSpan calls to critical paths."
        )
    if metric_count == 0:
        recommendations.append(
            "No OTel metric instrumentation detected. "
            "Add meter.create_counter / createHistogram for key business metrics."
        )
    if prometheus_alert_count == 0:
        recommendations.append(
            "No Prometheus alert rules found. "
            "Define alert rules for error rate, latency p99, and saturation."
        )
    if not grafana_dashboards:
        recommendations.append(
            "No Grafana dashboard JSON found. "
            "Commit dashboard-as-code for reproducible observability."
        )
    if log_file_count == 0:
        recommendations.append(
            "No structured logging detected. "
            "Adopt structlog / winston / zap for consistent machine-readable logs."
        )
    if scrape_job_count == 0 and prometheus_files:
        recommendations.append(
            "Prometheus config found but no scrape_configs jobs detected. "
            "Verify your prometheus.yml exposes the correct targets."
        )
    if sentry_init_files == 0:
        recommendations.append(
            "No Sentry SDK initialisation detected. "
            "Add sentry_sdk.init / Sentry.init to capture unhandled errors in production."
        )
    if datadog_init_files == 0:
        recommendations.append(
            "No Datadog APM tracer initialisation detected. "
            "Add tracer.init / tracer.Start to enable distributed tracing."
        )

    return {
        "ok": True,
        "otel_configs": otel_configs,
        "prometheus_alerts": prometheus_alert_count,
        "grafana_dashboards": len(grafana_dashboards),
        "instrumented_files": instrumented_files,
        "span_count": span_count,
        "metric_count": metric_count,
        "sentry_coverage": {
            "init_files": sentry_init_files,
            "total_files_with_sentry": sentry_total_files,
        },
        "datadog_coverage": {
            "init_files": datadog_init_files,
            "total_files_with_datadog": datadog_total_files,
        },
        "newrelic_hooks": newrelic_hooks,
        "dynatrace_hooks": dynatrace_hooks,
        "honeycomb_hooks": honeycomb_hooks,
        "jaeger_hooks": jaeger_hooks,
        "zipkin_hooks": zipkin_hooks,
        "has_alertmanager": has_alertmanager,
        "has_loki": has_loki,
        "coverage_score": min(score, 100),
        "recommendations": recommendations,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TEST SUITE  (python3 observability.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _test_new_observability() -> None:
    # New Relic Python
    nr_py = 'import newrelic.agent\n@newrelic.agent.function_trace()\ndef my_func(): pass'
    syms = extract_newrelic_instrumentation(nr_py, "app.py", ".py")
    assert any('newrelic' in s['name'].lower() for s in syms), f"NR Python: {syms}"
    print("[PASS] New Relic Python")

    # New Relic Node
    nr_js = "require('newrelic')\nnewrelic.startWebTransaction('/api/users', handler)"
    syms2 = extract_newrelic_instrumentation(nr_js, "server.js", ".js")
    assert any('newrelic' in s['name'].lower() for s in syms2), f"NR Node: {syms2}"
    print("[PASS] New Relic Node")

    # New Relic Go
    nr_go = '"github.com/newrelic/go-agent/v3/newrelic"\nnewrelic.NewApplication()\napp.StartTransaction("my-txn")\ntxn.NoticeError(err)'
    syms_go = extract_newrelic_instrumentation(nr_go, "main.go", ".go")
    assert any('newrelic' in s['name'].lower() for s in syms_go), f"NR Go: {syms_go}"
    print("[PASS] New Relic Go")

    # New Relic Java
    nr_java = 'import com.newrelic.api.agent.NewRelic;\n@Trace(dispatcher=true)\npublic void handle() {}\nNewRelic.noticeError(e);'
    syms_java = extract_newrelic_instrumentation(nr_java, "Handler.java", ".java")
    assert any('newrelic' in s['name'].lower() for s in syms_java), f"NR Java: {syms_java}"
    print("[PASS] New Relic Java")

    # New Relic config file
    syms_cfg = extract_newrelic_instrumentation("[newrelic]\nlicense_key = abc", "newrelic.ini", ".ini")
    assert any('newrelic' in s['name'].lower() for s in syms_cfg), f"NR Config: {syms_cfg}"
    print("[PASS] New Relic config file")

    # Dynatrace
    dt = "import oneagent\nsdk = oneagent.get_sdk()\nwith sdk.trace_incoming_remote_call('my_service', 'users', 'list'): pass"
    syms3 = extract_dynatrace_instrumentation(dt, "service.py", ".py")
    assert any('dynatrace' in s['name'].lower() or 'oneagent' in s['name'].lower() for s in syms3), f"Dynatrace: {syms3}"
    print("[PASS] Dynatrace Python")

    # Dynatrace Node
    dt_js = "const Sdk = require('@dynatrace/oneagent-sdk');\nconst sdk = Sdk.createInstance();\napi.createIncomingRemoteCallTracer('svc');"
    syms_dt_js = extract_dynatrace_instrumentation(dt_js, "server.js", ".js")
    assert any('dynatrace' in s['name'].lower() for s in syms_dt_js), f"Dynatrace Node: {syms_dt_js}"
    print("[PASS] Dynatrace Node")

    # Dynatrace config file
    syms_dt_cfg = extract_dynatrace_instrumentation("", "dtconfig.properties", ".properties")
    assert any('dynatrace' in s['name'].lower() for s in syms_dt_cfg), f"Dynatrace cfg: {syms_dt_cfg}"
    print("[PASS] Dynatrace config file")

    # Honeycomb
    hc = "import beeline\nbeeline.init(writekey='abc123', dataset='my-app')\n@beeline.traced\ndef my_handler(): pass"
    syms4 = extract_honeycomb_instrumentation(hc, "app.py", ".py")
    assert any('beeline' in s['name'].lower() or 'honeycomb' in s['name'].lower() for s in syms4), f"Honeycomb: {syms4}"
    print("[PASS] Honeycomb Python")

    # Honeycomb Node
    hc_js = "const beeline = require('honeycomb-beeline');\nbeeline.configure({writeKey: 'abc'});\nconst hc = require('libhoney');"
    syms_hc_js = extract_honeycomb_instrumentation(hc_js, "server.js", ".js")
    assert any('honeycomb' in s['name'].lower() or 'beeline' in s['name'].lower() for s in syms_hc_js), f"Honeycomb Node: {syms_hc_js}"
    print("[PASS] Honeycomb Node")

    # AlertManager
    am = "global:\n  resolve_timeout: 5m\nroute:\n  receiver: slack\nreceivers:\n- name: slack\n  slack_configs:\n  - api_url: https://hooks.slack.com/xxx"
    syms5 = extract_alertmanager_config(am, "alertmanager.yml", ".yml")
    assert len(syms5) > 0, f"AlertManager: {syms5}"
    # Expect alertmanager:config utility and receiver:slack hook
    assert any('alertmanager:config' in s['name'] for s in syms5), f"AlertManager config symbol missing: {syms5}"
    assert any('receiver:slack' in s['name'] for s in syms5), f"AlertManager receiver:slack missing: {syms5}"
    print("[PASS] AlertManager")

    # AlertManager with inhibit_rules
    am_inhibit = "global:\n  resolve_timeout: 5m\nreceivers:\n- name: default\ninhibit_rules:\n- source_match:\n    severity: critical"
    syms_am_inh = extract_alertmanager_config(am_inhibit, "alertmanager.yml", ".yml")
    assert any('inhibit' in s['name'] for s in syms_am_inh), f"AlertManager inhibit: {syms_am_inh}"
    print("[PASS] AlertManager inhibit_rules")

    # Loki server config
    loki_cfg = "auth_enabled: false\nserver:\n  http_listen_port: 3100\ningester:\n  lifecycler:\n    ring:\n      kvstore:\n        store: inmemory"
    syms_loki = extract_loki_config(loki_cfg, "/etc/loki/loki.yml", ".yml")
    assert any('loki:server' in s['name'] or 'loki:ingester' in s['name'] for s in syms_loki), f"Loki server: {syms_loki}"
    print("[PASS] Loki server config")

    # Promtail config
    promtail_cfg = "clients:\n  - url: http://loki:3100/loki/api/v1/push\nscrape_configs:\n  - job_name: varlogs"
    syms_pt = extract_loki_config(promtail_cfg, "/etc/promtail/promtail.yml", ".yml")
    assert any('promtail' in s['name'] for s in syms_pt), f"Promtail: {syms_pt}"
    print("[PASS] Promtail config")

    # Loki datasource in Grafana JSON
    grafana_loki = '{"panels": [], "datasource": {"type": "loki", "uid": "loki-ds"}}'
    syms_loki_json = extract_loki_config(grafana_loki, "dashboard.json", ".json")
    assert any('loki' in s['name'].lower() for s in syms_loki_json), f"Loki JSON datasource: {syms_loki_json}"
    print("[PASS] Loki Grafana datasource")

    # Jaeger Python
    jaeger = "from jaeger_client import Config\nconfig = Config(config={}, service_name='my-service')\ntracer = config.initialize_tracer()"
    syms6 = extract_jaeger_instrumentation(jaeger, "tracing.py", ".py")
    assert len(syms6) > 0, f"Jaeger: {syms6}"
    assert any('jaeger' in s['name'].lower() for s in syms6), f"Jaeger name check: {syms6}"
    # Check that service_name is captured
    assert any('my-service' in s['name'] or 'my-service' in ' '.join(s['keywords']) for s in syms6), \
        f"Jaeger service_name not captured: {syms6}"
    print("[PASS] Jaeger Python")

    # Jaeger Go
    jaeger_go = '"github.com/uber/jaeger-client-go"\ncfg := jaegercfg.Configuration{ServiceName: "my-go-svc"}\ntracer, _, _ := cfg.NewTracer()'
    syms_j_go = extract_jaeger_instrumentation(jaeger_go, "tracer.go", ".go")
    assert any('jaeger' in s['name'].lower() for s in syms_j_go), f"Jaeger Go: {syms_j_go}"
    print("[PASS] Jaeger Go")

    # Jaeger Node
    jaeger_js = "const initTracer = require('jaeger-client').initTracer;\nconst tracer = initTracer(config, options);"
    syms_j_js = extract_jaeger_instrumentation(jaeger_js, "tracer.js", ".js")
    assert any('jaeger' in s['name'].lower() for s in syms_j_js), f"Jaeger Node: {syms_j_js}"
    print("[PASS] Jaeger Node")

    # Zipkin Python
    zipkin_py = "from py_zipkin.zipkin import zipkin_span\n@zipkin_span(service_name='myservice', span_name='do_work')\ndef do_work(): pass"
    syms_z = extract_zipkin_instrumentation(zipkin_py, "app.py", ".py")
    assert any('zipkin' in s['name'].lower() for s in syms_z), f"Zipkin Python: {syms_z}"
    # Should have both utility (import) and hook (decorator)
    assert any(s['symbol_type'] == 'utility' for s in syms_z), f"Zipkin utility missing: {syms_z}"
    assert any(s['symbol_type'] == 'hook' for s in syms_z), f"Zipkin hook missing: {syms_z}"
    print("[PASS] Zipkin Python")

    # Zipkin Node
    zipkin_js = "const {Tracer, BatchRecorder} = require('zipkin');\nconst {HttpLogger} = require('zipkin-transport-http');\nconst tracer = new Tracer({recorder: new BatchRecorder()});"
    syms_z_js = extract_zipkin_instrumentation(zipkin_js, "tracer.js", ".js")
    assert any('zipkin' in s['name'].lower() for s in syms_z_js), f"Zipkin Node: {syms_z_js}"
    print("[PASS] Zipkin Node")

    print("\n=== All new observability tests PASSED ===")


if __name__ == "__main__":
    _test_new_observability()
