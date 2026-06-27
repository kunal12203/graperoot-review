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

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                         "dist", "build", "target", ".tox"}
        ]
        for fname in filenames:
            _, ext = os.path.splitext(fname.lower())
            if ext not in OBSERVABILITY_SOURCE_EXTS:
                continue
            fpath = os.path.join(dirpath, fname)
            ok, content, _ = _safe_read(fpath)
            if not ok:
                continue

            otel_syms = extract_otel_instrumentation(content, fpath, ext)
            log_syms = extract_log_patterns(content, fpath, ext)

            file_spans = sum(1 for s in otel_syms if s["symbol_type"] == "hook")
            file_metrics = sum(1 for s in otel_syms if s["symbol_type"] == "use_case")
            span_count += file_spans
            metric_count += file_metrics

            if otel_syms:
                instrumented_files += 1
            if log_syms:
                log_file_count += 1

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

    return {
        "ok": True,
        "otel_configs": otel_configs,
        "prometheus_alerts": prometheus_alert_count,
        "grafana_dashboards": len(grafana_dashboards),
        "instrumented_files": instrumented_files,
        "span_count": span_count,
        "metric_count": metric_count,
        "coverage_score": min(score, 100),
        "recommendations": recommendations,
    }
