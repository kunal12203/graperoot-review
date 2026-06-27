#!/usr/bin/env python3
"""Infrastructure-as-Code and CI/CD symbol/import extractor for GrapeRoot Pro.

Phase 4: IaC + CI/CD parsing.

Supported formats
-----------------
4A – IaC
  • Terraform / HCL          (.tf, .tfvars)
  • Docker Compose            (docker-compose*.yml, compose.yml)
  • Kubernetes                (.yml/.yaml with kind: field)
  • Helm Chart                (Chart.yaml)
  • Kustomize                 (kustomization.yaml)

4B – CI/CD
  • GitHub Actions            (.github/workflows/*.yml)
  • GitLab CI                 (.gitlab-ci.yml)
  • Jenkins                   (Jenkinsfile)
  • Azure Pipelines           (azure-pipelines.yml)
  • Ansible playbooks         (.yml with top-level hosts:)

All parsing is done with regex — no external YAML/HCL library required.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Top-level constants
# ---------------------------------------------------------------------------

INFRA_EXTS: frozenset[str] = frozenset({
    ".tf",
    ".tfvars",
    ".yml",
    ".yaml",
})

# File that must be matched by name (checked in is_infra_file)
_JENKINS_NAMES: frozenset[str] = frozenset({"Jenkinsfile", "Jenkinsfile.groovy"})

# Kubernetes kinds that map to specific symbol types
_K8S_KIND_MAP: dict[str, tuple[str, str]] = {
    # kind                → (symbol_type, confidence)
    "Deployment":         ("model",     "high"),
    "StatefulSet":        ("model",     "high"),
    "DaemonSet":          ("model",     "high"),
    "ReplicaSet":         ("model",     "medium"),
    "Pod":                ("model",     "medium"),
    "Job":                ("use_case",  "high"),
    "CronJob":            ("use_case",  "high"),
    "Service":            ("api_route", "high"),
    "Ingress":            ("api_route", "high"),
    "IngressClass":       ("api_route", "medium"),
    "ConfigMap":          ("utility",   "high"),
    "Secret":             ("utility",   "high"),
    "ServiceAccount":     ("utility",   "medium"),
    "Role":               ("utility",   "medium"),
    "ClusterRole":        ("utility",   "medium"),
    "RoleBinding":        ("utility",   "medium"),
    "ClusterRoleBinding": ("utility",   "medium"),
    "PersistentVolume":   ("utility",   "medium"),
    "PersistentVolumeClaim": ("utility", "medium"),
    "StorageClass":       ("utility",   "medium"),
    "HorizontalPodAutoscaler": ("use_case", "medium"),
    "NetworkPolicy":      ("utility",   "medium"),
    "Namespace":          ("utility",   "medium"),
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _body_hash(lines: list[str], start: int, end: int) -> str:
    """MD5[:8] of the lines in [start, end] (0-indexed, inclusive)."""
    body = "\n".join(lines[start: end + 1])
    return hashlib.md5(body.encode("utf-8", errors="ignore")).hexdigest()[:8]


def _name_keywords(name: str) -> list[str]:
    """Derive searchable keyword tokens from a symbol name."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        w = re.sub(r"[^a-z0-9]", "", word.lower())
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            tokens.append(w)

    add(name)
    # Split on non-alphanumeric separators (hyphens, underscores, dots, colons)
    for part in re.split(r"[^a-zA-Z0-9]+", name):
        add(part)
    # camelCase splitting
    for part in re.sub(r"([a-z0-9])([A-Z])", r"\1 \2",
                       re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)).split():
        add(part)

    return tokens[:12]


def _line_of(content: str, offset: int) -> int:
    """Return 0-indexed line number for a byte/char offset."""
    return content[:offset].count("\n")


def _make_symbol(
    file_path: str,
    name: str,
    symbol_type: str,
    line_start: int,
    line_end: int,
    lines: list[str],
    confidence: str = "high",
    extra_keywords: list[str] | None = None,
) -> dict:
    kw = _name_keywords(name)
    if extra_keywords:
        seen = set(kw)
        for w in extra_keywords:
            if w not in seen and len(w) >= 3:
                seen.add(w)
                kw.append(w)
    return {
        "id":          f"{file_path}::{name}",
        "name":        name,
        "symbol_type": symbol_type,
        "line_start":  line_start,
        "line_end":    line_end,
        "body_hash":   _body_hash(lines, line_start, line_end),
        "confidence":  confidence,
        "exported":    True,   # infra objects are always public
        "keywords":    kw,
    }


def _make_edge(from_id: str, to: str, rel: str = "imports") -> dict:
    return {"from": from_id, "to": to, "rel": rel}


# ---------------------------------------------------------------------------
# File-type detection helpers
# ---------------------------------------------------------------------------

def _is_docker_compose(file_path: str) -> bool:
    name = os.path.basename(file_path).lower()
    return bool(
        re.match(r"docker-compose.*\.(yml|yaml)$", name)
        or name in ("compose.yml", "compose.yaml")
    )


def _is_github_actions(file_path: str) -> bool:
    p = Path(file_path)
    parts = p.parts
    # Must be under .github/workflows/
    for i, part in enumerate(parts):
        if part == ".github" and i + 1 < len(parts) and parts[i + 1] == "workflows":
            return p.suffix.lower() in (".yml", ".yaml")
    return False


def _is_gitlab_ci(file_path: str) -> bool:
    return os.path.basename(file_path).lower() == ".gitlab-ci.yml"


def _is_azure_pipelines(file_path: str) -> bool:
    name = os.path.basename(file_path).lower()
    return name in ("azure-pipelines.yml", "azure-pipelines.yaml")


def _is_helm_chart(file_path: str) -> bool:
    return os.path.basename(file_path) in ("Chart.yaml", "Chart.yml")


def _is_kustomize(file_path: str) -> bool:
    return os.path.basename(file_path).lower() in (
        "kustomization.yaml", "kustomization.yml"
    )


def _is_jenkins(file_path: str) -> bool:
    return os.path.basename(file_path) in _JENKINS_NAMES


def _is_terraform(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in (".tf", ".tfvars")


def _is_kubernetes(content_hint: str) -> bool:
    """Detect Kubernetes YAML by presence of kind: and apiVersion: fields."""
    return bool(
        re.search(r"^\s*kind\s*:\s*\S", content_hint, re.MULTILINE)
        and re.search(r"^\s*apiVersion\s*:", content_hint, re.MULTILINE)
    )


def _is_ansible(content_hint: str) -> bool:
    """Detect Ansible playbook: top-level list with hosts: key."""
    # Playbooks start with '- hosts:' or '- name:' ... 'hosts:'
    return bool(re.search(r"^\s*-\s+hosts\s*:", content_hint, re.MULTILINE))


# ---------------------------------------------------------------------------
# 4A-1: Terraform / HCL
# ---------------------------------------------------------------------------

# HCL block opener: resource "TYPE" "NAME" {
_RE_TF_RESOURCE = re.compile(
    r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE
)
_RE_TF_MODULE = re.compile(r'^module\s+"([^"]+)"\s*\{', re.MULTILINE)
_RE_TF_DATA = re.compile(
    r'^data\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE
)
_RE_TF_VARIABLE = re.compile(r'^variable\s+"([^"]+)"\s*\{', re.MULTILINE)
_RE_TF_OUTPUT = re.compile(r'^output\s+"([^"]+)"\s*\{', re.MULTILINE)
_RE_TF_PROVIDER = re.compile(r'^provider\s+"([^"]+)"\s*\{', re.MULTILINE)
_RE_TF_MODULE_SOURCE = re.compile(r'source\s*=\s*"([^"]+)"')
_RE_TF_MODULE_CALL = re.compile(r'module\s*\.\s*([A-Za-z0-9_\-]+)', re.MULTILINE)


def _tf_block_end(lines: list[str], start: int) -> int:
    """Find the closing brace of a HCL block starting at line `start`."""
    depth = 0
    found_open = False
    limit = min(start + 500, len(lines))
    for i in range(start, limit):
        opens  = lines[i].count("{")
        closes = lines[i].count("}")
        depth += opens - closes
        if opens > 0:
            found_open = True
        if found_open and depth <= 0:
            return i
    return min(start + 100, len(lines) - 1)


def extract_symbols_terraform(content: str, file_path: str) -> list[dict]:
    """Extract Terraform/HCL symbols from a .tf or .tfvars file."""
    lines = content.splitlines()
    symbols: list[dict] = []
    seen: set[str] = set()

    def add(name: str, symbol_type: str, line_start: int,
            confidence: str = "high", extra_kw: list[str] | None = None) -> None:
        if name in seen:
            return
        seen.add(name)
        line_end = _tf_block_end(lines, line_start)
        sym = _make_symbol(file_path, name, symbol_type, line_start, line_end,
                           lines, confidence, extra_kw)
        symbols.append(sym)

    for m in _RE_TF_RESOURCE.finditer(content):
        rtype, rname = m.group(1), m.group(2)
        sym_name = f"resource:{rtype}.{rname}"
        line_no  = _line_of(content, m.start())
        add(sym_name, "model", line_no, "high", [rtype, rname])

    for m in _RE_TF_MODULE.finditer(content):
        sym_name = f"module:{m.group(1)}"
        line_no  = _line_of(content, m.start())
        add(sym_name, "use_case", line_no, "high", [m.group(1)])

    for m in _RE_TF_DATA.finditer(content):
        dtype, dname = m.group(1), m.group(2)
        sym_name = f"data:{dtype}.{dname}"
        line_no  = _line_of(content, m.start())
        add(sym_name, "model", line_no, "high", [dtype, dname])

    for m in _RE_TF_VARIABLE.finditer(content):
        sym_name = f"var:{m.group(1)}"
        line_no  = _line_of(content, m.start())
        add(sym_name, "utility", line_no, "high", [m.group(1)])

    for m in _RE_TF_OUTPUT.finditer(content):
        sym_name = f"output:{m.group(1)}"
        line_no  = _line_of(content, m.start())
        add(sym_name, "utility", line_no, "high", [m.group(1)])

    # Providers contribute keywords but are not surfaced as symbols (noisy)
    # We extract them only to enrich keywords on the file level — not added here.

    return symbols


def parse_imports_terraform(content: str, file_id: str) -> list[dict]:
    """Extract Terraform module source references as import edges."""
    edges: list[dict] = []
    for m in _RE_TF_MODULE_SOURCE.finditer(content):
        edges.append(_make_edge(file_id, m.group(1), "imports"))
    for m in _RE_TF_MODULE_CALL.finditer(content):
        edges.append(_make_edge(file_id, f"module:{m.group(1)}", "uses"))
    return edges


# ---------------------------------------------------------------------------
# 4A-2: Docker Compose
# ---------------------------------------------------------------------------

# Matches YAML mapping keys at various indent levels.
_RE_DC_SERVICE = re.compile(r"^( {2}|\t)([A-Za-z0-9_\-\.]+)\s*:", re.MULTILINE)
_RE_DC_IMAGE   = re.compile(r"^\s+image\s*:\s*(.+)$", re.MULTILINE)
_RE_DC_DEPENDS = re.compile(r"^\s+-\s+([A-Za-z0-9_\-\.]+)\s*$", re.MULTILINE)
_RE_DC_VOLUME_TOP  = re.compile(r"^volumes\s*:\s*$", re.MULTILINE)
_RE_DC_NETWORK_TOP = re.compile(r"^networks\s*:\s*$", re.MULTILINE)
_RE_DC_NAMED_KEY   = re.compile(r"^  ([A-Za-z0-9_\-\.]+)\s*:", re.MULTILINE)


def _dc_parse_top_level_block(content: str, block_header_re: re.Pattern,
                               start_offset: int) -> list[tuple[str, int]]:
    """
    After a top-level block header (e.g. 'volumes:'), collect 2-space-indented
    keys that are direct children.  Returns list of (name, line_no).
    """
    items: list[tuple[str, int]] = []
    lines = content.splitlines()
    header_line = content[:start_offset].count("\n")
    in_block = False
    for i in range(header_line, len(lines)):
        line = lines[i]
        if i == header_line:
            in_block = True
            continue
        if not line.strip():
            continue
        # Back to top-level (non-indented) means block ended
        if in_block and not line.startswith(" ") and not line.startswith("\t"):
            break
        if in_block:
            m = re.match(r"^  ([A-Za-z0-9_\-\.]+)\s*:", line)
            if m:
                items.append((m.group(1), i))
    return items


def extract_symbols_docker_compose(content: str, file_path: str) -> list[dict]:
    """Extract services, volumes, and networks from a Docker Compose file."""
    lines  = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    def add(name: str, symbol_type: str, line_no: int,
            extra_kw: list[str] | None = None) -> None:
        if name in seen:
            return
        seen.add(name)
        # Find the end of this service block: next peer key at same indent or EOF
        line_end = line_no
        indent   = len(lines[line_no]) - len(lines[line_no].lstrip())
        for j in range(line_no + 1, len(lines)):
            ll = lines[j]
            if not ll.strip():
                continue
            cur_indent = len(ll) - len(ll.lstrip())
            if cur_indent <= indent and re.match(r"\s*[A-Za-z0-9_\-\.]+\s*:", ll):
                break
            line_end = j
        sym = _make_symbol(file_path, name, symbol_type, line_no, line_end,
                           lines, "high", extra_kw)
        symbols.append(sym)

    # ── services ─────────────────────────────────────────────────────────────
    services_m = re.search(r"^services\s*:\s*$", content, re.MULTILINE)
    if services_m:
        service_names: list[str] = []
        service_section_start = _line_of(content, services_m.start())
        for i in range(service_section_start + 1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            # Top-level block ends when a non-indented key appears
            if not line.startswith(" ") and not line.startswith("\t"):
                break
            m = re.match(r"^  ([A-Za-z0-9_\-\.]+)\s*:", line)
            if m:
                svc = m.group(1)
                service_names.append(svc)
                add(f"service:{svc}", "model", i, [svc, "service", "container"])

    # ── volumes ───────────────────────────────────────────────────────────────
    vol_m = re.search(r"^volumes\s*:\s*$", content, re.MULTILINE)
    if vol_m:
        for vname, lineno in _dc_parse_top_level_block(content, _RE_DC_VOLUME_TOP,
                                                       vol_m.start()):
            add(f"volume:{vname}", "utility", lineno, [vname, "volume", "storage"])

    # ── networks ──────────────────────────────────────────────────────────────
    net_m = re.search(r"^networks\s*:\s*$", content, re.MULTILINE)
    if net_m:
        for nname, lineno in _dc_parse_top_level_block(content, _RE_DC_NETWORK_TOP,
                                                       net_m.start()):
            add(f"network:{nname}", "utility", lineno, [nname, "network"])

    return symbols


def parse_imports_docker_compose(content: str, file_id: str) -> list[dict]:
    """Extract image references and depends_on edges from a Docker Compose file."""
    edges: list[dict] = []
    lines = content.splitlines()

    # image: references
    for m in _RE_DC_IMAGE.finditer(content):
        image = m.group(1).strip().strip('"').strip("'")
        if image:
            edges.append(_make_edge(file_id, image, "uses_image"))

    # depends_on: — service A depends on service B
    # We need to associate the depends_on list with its parent service.
    # Strategy: scan lines; when we enter a 'depends_on:' block, emit edges
    # from the nearest enclosing service.
    current_service: str | None = None
    in_depends: bool = False
    for i, line in enumerate(lines):
        # Track current service (2-space indent key under 'services:')
        m_svc = re.match(r"^  ([A-Za-z0-9_\-\.]+)\s*:", line)
        if m_svc and not line.startswith("   "):
            current_service = m_svc.group(1)
            in_depends = False
            continue
        if re.match(r"^\s+depends_on\s*:", line):
            in_depends = True
            continue
        if in_depends:
            m_dep = re.match(r"^\s+-\s+([A-Za-z0-9_\-\.]+)", line)
            if m_dep:
                dep = m_dep.group(1)
                src = f"{file_id}::service:{current_service}" if current_service else file_id
                edges.append(_make_edge(src, f"service:{dep}", "depends_on"))
            elif line.strip() and not line.strip().startswith("-"):
                in_depends = False

    return edges


# ---------------------------------------------------------------------------
# 4A-3: Kubernetes YAML
# ---------------------------------------------------------------------------

_RE_K8S_KIND     = re.compile(r"^kind\s*:\s*(\S+)", re.MULTILINE)
_RE_K8S_API_VER  = re.compile(r"^apiVersion\s*:\s*(\S+)", re.MULTILINE)
_RE_K8S_META_NAME = re.compile(r"^  name\s*:\s*(\S+)", re.MULTILINE)
_RE_K8S_META_NS   = re.compile(r"^  namespace\s*:\s*(\S+)", re.MULTILINE)
_RE_K8S_CONFIGMAP_REF = re.compile(
    r"configMapKeyRef\s*:\s*\n\s+name\s*:\s*(\S+)"
    r"|configMapRef\s*:\s*\n\s+name\s*:\s*(\S+)"
    r"|configMap\s*:\s*\n\s+name\s*:\s*(\S+)",
    re.MULTILINE,
)
_RE_K8S_SECRET_REF = re.compile(
    r"secretKeyRef\s*:\s*\n\s+name\s*:\s*(\S+)"
    r"|secretRef\s*:\s*\n\s+name\s*:\s*(\S+)",
    re.MULTILINE,
)
_RE_K8S_SERVICE_REF = re.compile(
    r"serviceName\s*:\s*(\S+)", re.MULTILINE
)

# Multi-document YAML split
_RE_YAML_DOC_SEP = re.compile(r"^---\s*$", re.MULTILINE)


def _parse_single_k8s_doc(doc: str, file_path: str,
                           line_offset: int) -> list[dict]:
    """Parse a single Kubernetes YAML document (between --- separators)."""
    symbols: list[dict] = []
    lines_full = doc.splitlines()

    kind_m = _RE_K8S_KIND.search(doc)
    if not kind_m:
        return symbols

    kind = kind_m.group(1).strip()
    name_m = _RE_K8S_META_NAME.search(doc)
    res_name = name_m.group(1).strip() if name_m else "unknown"
    ns_m = _RE_K8S_META_NS.search(doc)
    namespace = ns_m.group(1).strip() if ns_m else ""

    sym_type, confidence = _K8S_KIND_MAP.get(kind, ("utility", "medium"))
    sym_name = f"{kind}:{res_name}"
    if namespace:
        sym_name = f"{kind}:{namespace}/{res_name}"

    extra_kw: list[str] = [kind.lower(), res_name]
    if namespace:
        extra_kw.append(namespace)
    api_m = _RE_K8S_API_VER.search(doc)
    if api_m:
        api = api_m.group(1).strip()
        # e.g. apps/v1 -> apps
        group = api.split("/")[0] if "/" in api else ""
        if group and group != "v1":
            extra_kw.append(group)

    line_start = line_offset
    line_end   = line_offset + len(lines_full) - 1

    sym = _make_symbol(file_path, sym_name, sym_type,
                       line_start, line_end,
                       [],  # lines not needed for hash — use doc lines below
                       confidence, extra_kw)
    # Recompute body hash with actual doc content
    sym["body_hash"] = hashlib.md5(doc.encode("utf-8", errors="ignore")).hexdigest()[:8]
    symbols.append(sym)
    return symbols


def extract_symbols_kubernetes(content: str, file_path: str) -> list[dict]:
    """Extract Kubernetes resource symbols from a YAML file (multi-doc aware)."""
    symbols: list[dict] = []
    # Split multi-document YAML
    doc_starts: list[int] = [0]
    for m in _RE_YAML_DOC_SEP.finditer(content):
        doc_starts.append(m.end())

    docs: list[tuple[str, int]] = []
    for i, start in enumerate(doc_starts):
        end = doc_starts[i + 1] if i + 1 < len(doc_starts) else len(content)
        doc = content[start:end]
        line_offset = content[:start].count("\n")
        docs.append((doc, line_offset))

    for doc, line_offset in docs:
        if doc.strip():
            symbols.extend(_parse_single_k8s_doc(doc, file_path, line_offset))

    return symbols


def parse_imports_kubernetes(content: str, file_id: str) -> list[dict]:
    """Extract ConfigMap, Secret, and Service references from Kubernetes YAML."""
    edges: list[dict] = []

    for m in _RE_K8S_CONFIGMAP_REF.finditer(content):
        ref = m.group(1) or m.group(2) or m.group(3)
        if ref:
            edges.append(_make_edge(file_id, f"ConfigMap:{ref}", "uses"))

    for m in _RE_K8S_SECRET_REF.finditer(content):
        ref = m.group(1) or m.group(2)
        if ref:
            edges.append(_make_edge(file_id, f"Secret:{ref}", "uses"))

    for m in _RE_K8S_SERVICE_REF.finditer(content):
        edges.append(_make_edge(file_id, f"Service:{m.group(1)}", "uses"))

    return edges


# ---------------------------------------------------------------------------
# 4A-4: Helm Chart.yaml
# ---------------------------------------------------------------------------

_RE_HELM_NAME    = re.compile(r"^name\s*:\s*(.+)$",    re.MULTILINE)
_RE_HELM_VERSION = re.compile(r"^version\s*:\s*(.+)$", re.MULTILINE)
_RE_HELM_APP_VER = re.compile(r"^appVersion\s*:\s*(.+)$", re.MULTILINE)
_RE_HELM_TYPE    = re.compile(r"^type\s*:\s*(.+)$",    re.MULTILINE)
_RE_HELM_DEP     = re.compile(
    r"^\s+-\s+name\s*:\s*(.+)$", re.MULTILINE
)
_RE_HELM_DEP_REPO = re.compile(
    r"^\s+repository\s*:\s*(.+)$", re.MULTILINE
)


def extract_symbols_helm(content: str, file_path: str) -> list[dict]:
    """Extract chart-level symbol and dependency symbols from Chart.yaml."""
    lines = content.splitlines()
    symbols: list[dict] = []

    name_m = _RE_HELM_NAME.search(content)
    chart_name = name_m.group(1).strip().strip('"').strip("'") if name_m else "unknown"
    version_m  = _RE_HELM_VERSION.search(content)
    version    = version_m.group(1).strip() if version_m else ""
    type_m     = _RE_HELM_TYPE.search(content)
    chart_type = type_m.group(1).strip() if type_m else "application"

    extra_kw = ["helm", "chart", chart_type, chart_name]
    if version:
        extra_kw.append(version)

    sym = _make_symbol(file_path, f"chart:{chart_name}", "use_case",
                       0, len(lines) - 1, lines, "high", extra_kw)
    symbols.append(sym)

    # Each dependency
    dep_names: list[str] = []
    in_deps = False
    for i, line in enumerate(lines):
        if re.match(r"^dependencies\s*:", line):
            in_deps = True
            continue
        if in_deps:
            m = re.match(r"^\s+-\s+name\s*:\s*(.+)$", line)
            if m:
                dep = m.group(1).strip().strip('"').strip("'")
                dep_names.append(dep)
                dep_sym = _make_symbol(
                    file_path, f"dep:{dep}", "use_case", i, i,
                    lines, "medium", ["helm", "dependency", dep]
                )
                symbols.append(dep_sym)
            elif line.strip() and not line.startswith(" "):
                in_deps = False

    return symbols


# ---------------------------------------------------------------------------
# 4A-5: Kustomize (kustomization.yaml)
# ---------------------------------------------------------------------------

_RE_KUST_BASE      = re.compile(r"^\s+-\s+(.+)$", re.MULTILINE)
_RE_KUST_SECTION   = re.compile(
    r"^(bases|resources|components|patches|patchesStrategicMerge"
    r"|patchesJson6902|configurations|generators|transformers)\s*:",
    re.MULTILINE
)
_RE_KUST_NAMESPACE = re.compile(r"^namespace\s*:\s*(.+)$", re.MULTILINE)
_RE_KUST_NAMEPREFIX = re.compile(r"^namePrefix\s*:\s*(.+)$", re.MULTILINE)


def extract_symbols_kustomize(content: str, file_path: str) -> list[dict]:
    """Extract a top-level kustomization symbol from kustomization.yaml."""
    lines = content.splitlines()
    symbols: list[dict] = []

    ns_m = _RE_KUST_NAMESPACE.search(content)
    namespace = ns_m.group(1).strip() if ns_m else ""
    prefix_m = _RE_KUST_NAMEPREFIX.search(content)
    name_prefix = prefix_m.group(1).strip() if prefix_m else ""

    extra_kw = ["kustomize", "kustomization"]
    if namespace:
        extra_kw.append(namespace)
    if name_prefix:
        extra_kw.append(name_prefix)

    sym_name = f"kustomization:{namespace}" if namespace else "kustomization:root"
    sym = _make_symbol(file_path, sym_name, "use_case",
                       0, len(lines) - 1, lines, "high", extra_kw)
    symbols.append(sym)
    return symbols


def _kustomize_refs(content: str) -> list[str]:
    """Return all path/url values listed under kustomize resource sections."""
    refs: list[str] = []
    lines = content.splitlines()
    in_section = False
    for line in lines:
        if _RE_KUST_SECTION.match(line):
            in_section = True
            continue
        if in_section:
            m = re.match(r"^\s+-\s+(.+)$", line)
            if m:
                refs.append(m.group(1).strip().strip('"').strip("'"))
            elif line.strip() and not line.startswith(" ") and not line.startswith("-"):
                in_section = False
    return refs


# ---------------------------------------------------------------------------
# 4B-1: GitHub Actions
# ---------------------------------------------------------------------------

_RE_GHA_JOB   = re.compile(r"^  ([A-Za-z0-9_\-]+)\s*:", re.MULTILINE)
_RE_GHA_NEEDS = re.compile(r"^\s+needs\s*:\s*\[?([^\]\n]+)\]?", re.MULTILINE)
_RE_GHA_USES  = re.compile(r"^\s+(?:-\s+)?uses\s*:\s*(.+)$", re.MULTILINE)
_RE_GHA_ON    = re.compile(r"^on\s*:\s*(.*)$", re.MULTILINE)
_RE_GHA_TRIGGER_LIST = re.compile(r"^\s+([a-z_]+)\s*:", re.MULTILINE)


def _gha_parse_jobs(content: str) -> list[tuple[str, int, int]]:
    """
    Return list of (job_name, line_start, line_end) from the jobs: block.
    """
    lines = content.splitlines()
    jobs_m = re.search(r"^jobs\s*:\s*$", content, re.MULTILINE)
    if not jobs_m:
        return []

    jobs_line = _line_of(content, jobs_m.start())
    job_list: list[tuple[str, int]] = []

    for i in range(jobs_line + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        # Job keys are exactly 2-space indented
        m = re.match(r"^  ([A-Za-z0-9_\-]+)\s*:", line)
        if m:
            job_list.append((m.group(1), i))
        elif not line.startswith(" ") and line.strip():
            break

    result: list[tuple[str, int, int]] = []
    for idx, (jname, jstart) in enumerate(job_list):
        jend = job_list[idx + 1][1] - 1 if idx + 1 < len(job_list) else len(lines) - 1
        result.append((jname, jstart, jend))
    return result


def _gha_trigger_keywords(content: str) -> list[str]:
    """Extract trigger event names from the 'on:' block."""
    kw: list[str] = []
    on_m = _RE_GHA_ON.search(content)
    if not on_m:
        return kw
    # Inline list: on: [push, pull_request]
    inline = on_m.group(1).strip()
    for ev in re.findall(r"[a-z_]+", inline):
        if len(ev) >= 3:
            kw.append(ev)
    # Or block form — scan subsequent indented keys
    lines = content.splitlines()
    on_line = _line_of(content, on_m.start())
    for i in range(on_line + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if not line.startswith(" "):
            break
        m = re.match(r"^  ([a-z_]+)\s*:", line)
        if m:
            kw.append(m.group(1))
    return kw


def extract_symbols_github_actions(content: str, file_path: str) -> list[dict]:
    """Extract job symbols from a GitHub Actions workflow file."""
    lines   = content.splitlines()
    symbols: list[dict] = []

    triggers = _gha_trigger_keywords(content)

    for jname, jstart, jend in _gha_parse_jobs(content):
        extra_kw = ["github", "actions", "ci", "job"] + triggers
        sym = _make_symbol(file_path, f"job:{jname}", "use_case",
                           jstart, jend, lines, "high", extra_kw)
        symbols.append(sym)

    return symbols


def parse_imports_github_actions(content: str, file_id: str) -> list[dict]:
    """Extract job 'needs' dependencies and 'uses' action references."""
    edges: list[dict] = []
    lines = content.splitlines()

    # Map line number → owning job name
    job_at_line: dict[int, str] = {}
    for jname, jstart, jend in _gha_parse_jobs(content):
        for ln in range(jstart, jend + 1):
            job_at_line[ln] = jname

    def owning_job(line_no: int) -> str | None:
        return job_at_line.get(line_no)

    for m in _RE_GHA_NEEDS.finditer(content):
        line_no = _line_of(content, m.start())
        job = owning_job(line_no)
        src = f"{file_id}::job:{job}" if job else file_id
        raw = m.group(1)
        for dep in re.findall(r"[A-Za-z0-9_\-]+", raw):
            edges.append(_make_edge(src, f"job:{dep}", "needs"))

    for m in _RE_GHA_USES.finditer(content):
        line_no = _line_of(content, m.start())
        job = owning_job(line_no)
        src  = f"{file_id}::job:{job}" if job else file_id
        uses = m.group(1).strip()
        edges.append(_make_edge(src, uses, "uses_action"))

    return edges


# ---------------------------------------------------------------------------
# 4B-2: GitLab CI
# ---------------------------------------------------------------------------

# Top-level keys that are NOT jobs
_GITLAB_SPECIAL_KEYS: frozenset[str] = frozenset({
    "stages", "variables", "cache", "before_script", "after_script",
    "default", "include", "workflow", "image", "services",
    "pages", ".gitlab", "coverage", "artifacts",
})

_RE_GITLAB_JOB     = re.compile(r"^([A-Za-z0-9_\-\.][A-Za-z0-9_\-\. ]*)\s*:", re.MULTILINE)
_RE_GITLAB_NEEDS   = re.compile(r"^\s+(?:needs|dependencies)\s*:", re.MULTILINE)
_RE_GITLAB_EXTENDS = re.compile(r"^\s+extends\s*:\s*(.+)$", re.MULTILINE)
_RE_GITLAB_STAGE   = re.compile(r"^\s+stage\s*:\s*(.+)$", re.MULTILINE)
_RE_GITLAB_INCLUDE = re.compile(r"^\s+-\s+(?:local|project|remote|template)\s*:\s*(.+)$",
                                re.MULTILINE)


def _gitlab_job_line_range(content: str, job_name: str,
                            job_line: int) -> tuple[int, int]:
    """Find end line of a GitLab job block."""
    lines = content.splitlines()
    for i in range(job_line + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if re.match(r"^[A-Za-z0-9_\-\.]", line) and not line.startswith(" "):
            return job_line, i - 1
    return job_line, len(lines) - 1


def extract_symbols_gitlab_ci(content: str, file_path: str) -> list[dict]:
    """Extract job symbols from a GitLab CI configuration file."""
    lines   = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    # Collect stages for keyword enrichment
    stages_kw: list[str] = []
    stages_m = re.search(r"^stages\s*:\s*$", content, re.MULTILINE)
    if stages_m:
        stage_line = _line_of(content, stages_m.start())
        for i in range(stage_line + 1, len(lines)):
            ll = lines[i]
            if not ll.strip():
                continue
            if not ll.startswith(" "):
                break
            sm = re.match(r"^\s+-\s+(.+)$", ll)
            if sm:
                stages_kw.append(sm.group(1).strip())

    for m in _RE_GITLAB_JOB.finditer(content):
        job_name = m.group(1).strip()
        # Skip special/reserved keys and hidden jobs that start with a dot
        # (hidden jobs are valid but keep them — they can be templates)
        if job_name.lower() in _GITLAB_SPECIAL_KEYS:
            continue
        if job_name in seen:
            continue
        line_no = _line_of(content, m.start())
        _, line_end = _gitlab_job_line_range(content, job_name, line_no)

        # Extract stage for this job
        job_block = "\n".join(lines[line_no: line_end + 1])
        stage_m = _RE_GITLAB_STAGE.search(job_block)
        stage   = stage_m.group(1).strip() if stage_m else ""
        extra_kw = ["gitlab", "ci", "job"] + stages_kw
        if stage:
            extra_kw.append(stage)

        seen.add(job_name)
        sym = _make_symbol(file_path, f"job:{job_name}", "use_case",
                           line_no, line_end, lines, "high", extra_kw)
        symbols.append(sym)

    return symbols


def parse_imports_gitlab_ci(content: str, file_id: str) -> list[dict]:
    """Extract extends, needs/dependencies, and include edges from GitLab CI."""
    edges: list[dict] = []
    lines = content.splitlines()

    # Job name by line
    job_ranges: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for m in _RE_GITLAB_JOB.finditer(content):
        job_name = m.group(1).strip()
        if job_name.lower() in _GITLAB_SPECIAL_KEYS or job_name in seen:
            continue
        seen.add(job_name)
        line_no = _line_of(content, m.start())
        _, line_end = _gitlab_job_line_range(content, job_name, line_no)
        job_ranges.append((job_name, line_no, line_end))

    def find_job(line_no: int) -> str | None:
        for jname, jstart, jend in job_ranges:
            if jstart <= line_no <= jend:
                return jname
        return None

    for m in _RE_GITLAB_EXTENDS.finditer(content):
        line_no = _line_of(content, m.start())
        job = find_job(line_no)
        src = f"{file_id}::job:{job}" if job else file_id
        raw = m.group(1).strip()
        for ext in re.findall(r"[A-Za-z0-9_\-\.]+", raw):
            edges.append(_make_edge(src, f"job:{ext}", "extends"))

    # needs/dependencies block: list items below the key
    in_needs = False
    needs_job: str | None = None
    for i, line in enumerate(lines):
        if re.match(r"^\s+(?:needs|dependencies)\s*:", line):
            needs_job = find_job(i)
            in_needs  = True
            continue
        if in_needs:
            m_dep = re.match(r"^\s+-\s+(?:job\s*:\s*)?([A-Za-z0-9_\-\.]+)", line)
            if m_dep:
                src = f"{file_id}::job:{needs_job}" if needs_job else file_id
                edges.append(_make_edge(src, f"job:{m_dep.group(1)}", "needs"))
            elif line.strip() and not re.match(r"^\s+(?:-|\w+\s*:)", line):
                in_needs = False

    # include: references
    for m in _RE_GITLAB_INCLUDE.finditer(content):
        ref = m.group(1).strip().strip('"').strip("'")
        if ref:
            edges.append(_make_edge(file_id, ref, "includes"))

    return edges


# ---------------------------------------------------------------------------
# 4B-3: Jenkins
# ---------------------------------------------------------------------------

_RE_JENKINS_STAGE   = re.compile(
    r"""stage\s*\(\s*['"]([^'"]+)['"]\s*\)""", re.MULTILINE
)
_RE_JENKINS_LIBRARY = re.compile(
    r"""library\s+identifier\s*:\s*['"]([^'"@]+)""", re.MULTILINE
)
_RE_JENKINS_NODE    = re.compile(r"node\s*\(", re.MULTILINE)


def extract_symbols_jenkins(content: str, file_path: str) -> list[dict]:
    """Extract stage symbols from a Jenkinsfile."""
    lines   = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    for m in _RE_JENKINS_STAGE.finditer(content):
        stage_name = m.group(1).strip()
        if stage_name in seen:
            continue
        seen.add(stage_name)
        line_no = _line_of(content, m.start())
        # Find the matching closing brace
        line_end = _tf_block_end(lines, line_no)
        sym = _make_symbol(file_path, f"stage:{stage_name}", "use_case",
                           line_no, line_end, lines, "high",
                           ["jenkins", "stage", "ci", stage_name.lower()])
        symbols.append(sym)

    return symbols


def _parse_imports_jenkins(content: str, file_id: str) -> list[dict]:
    """Extract library identifier references from a Jenkinsfile."""
    edges: list[dict] = []
    for m in _RE_JENKINS_LIBRARY.finditer(content):
        edges.append(_make_edge(file_id, m.group(1).strip(), "uses_library"))
    return edges


# ---------------------------------------------------------------------------
# 4B-4: Azure Pipelines
# ---------------------------------------------------------------------------

_RE_AZ_STAGE = re.compile(r"^\s*-\s+stage\s*:\s*(.+)$", re.MULTILINE)
_RE_AZ_JOB   = re.compile(r"^\s*-\s+job\s*:\s*(.+)$",   re.MULTILINE)
_RE_AZ_DEPLOYMENT = re.compile(r"^\s*-\s+deployment\s*:\s*(.+)$", re.MULTILINE)
_RE_AZ_TEMPLATE   = re.compile(r"^\s+template\s*:\s*(.+)$", re.MULTILINE)
_RE_AZ_DEPENDS    = re.compile(r"^\s+dependsOn\s*:\s*\[?([^\]\n]+)\]?", re.MULTILINE)


def extract_symbols_azure_pipelines(content: str, file_path: str) -> list[dict]:
    """Extract stage and job symbols from an Azure Pipelines definition."""
    lines   = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    for m in _RE_AZ_STAGE.finditer(content):
        name = m.group(1).strip().strip('"').strip("'")
        if name in seen:
            continue
        seen.add(name)
        line_no = _line_of(content, m.start())
        sym = _make_symbol(file_path, f"stage:{name}", "model",
                           line_no, line_no, lines, "high",
                           ["azure", "pipelines", "stage", name.lower()])
        symbols.append(sym)

    for m in _RE_AZ_JOB.finditer(content):
        name = m.group(1).strip().strip('"').strip("'")
        if name in seen:
            continue
        seen.add(name)
        line_no = _line_of(content, m.start())
        sym = _make_symbol(file_path, f"job:{name}", "use_case",
                           line_no, line_no, lines, "high",
                           ["azure", "pipelines", "job", name.lower()])
        symbols.append(sym)

    for m in _RE_AZ_DEPLOYMENT.finditer(content):
        name = m.group(1).strip().strip('"').strip("'")
        if name in seen:
            continue
        seen.add(name)
        line_no = _line_of(content, m.start())
        sym = _make_symbol(file_path, f"deployment:{name}", "use_case",
                           line_no, line_no, lines, "high",
                           ["azure", "pipelines", "deployment", name.lower()])
        symbols.append(sym)

    return symbols


def _parse_imports_azure_pipelines(content: str, file_id: str) -> list[dict]:
    """Extract template includes and dependsOn edges from Azure Pipelines."""
    edges: list[dict] = []

    for m in _RE_AZ_TEMPLATE.finditer(content):
        tmpl = m.group(1).strip().strip('"').strip("'")
        if tmpl:
            edges.append(_make_edge(file_id, tmpl, "uses_template"))

    for m in _RE_AZ_DEPENDS.finditer(content):
        raw = m.group(1)
        for dep in re.findall(r"[A-Za-z0-9_\-]+", raw):
            edges.append(_make_edge(file_id, dep, "depends_on"))

    return edges


# ---------------------------------------------------------------------------
# 4B-5: Ansible
# ---------------------------------------------------------------------------

_RE_ANSIBLE_TASK_NAME = re.compile(
    r"^\s+-\s+name\s*:\s*(.+)$", re.MULTILINE
)
_RE_ANSIBLE_HOSTS = re.compile(
    r"^\s*-\s+hosts\s*:\s*(.+)$", re.MULTILINE
)
_RE_ANSIBLE_INCLUDE_ROLE = re.compile(
    r"(?:include_role|import_role)\s*:\s*\n\s+name\s*:\s*(\S+)", re.MULTILINE
)
_RE_ANSIBLE_INCLUDE_TASKS = re.compile(
    r"(?:include_tasks|import_tasks)\s*:\s*(.+)$", re.MULTILINE
)
_RE_ANSIBLE_INCLUDE_PLAYBOOK = re.compile(
    r"import_playbook\s*:\s*(.+)$", re.MULTILINE
)


def extract_symbols_ansible(content: str, file_path: str) -> list[dict]:
    """Extract task symbols from an Ansible playbook."""
    lines   = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    # Collect hosts for keyword enrichment
    host_kw: list[str] = []
    for hm in _RE_ANSIBLE_HOSTS.finditer(content):
        hval = hm.group(1).strip()
        for h in re.split(r"[,\s]+", hval):
            h = h.strip()
            if h:
                host_kw.append(h)

    for m in _RE_ANSIBLE_TASK_NAME.finditer(content):
        task_name = m.group(1).strip().strip('"').strip("'")
        if task_name in seen or not task_name:
            continue
        seen.add(task_name)
        line_no = _line_of(content, m.start())
        extra_kw = ["ansible", "task"] + host_kw + [
            w.lower() for w in re.split(r"\W+", task_name) if len(w) >= 3
        ]
        sym = _make_symbol(file_path, f"task:{task_name}", "use_case",
                           line_no, line_no, lines, "high", extra_kw)
        symbols.append(sym)

    return symbols


def parse_imports_ansible(content: str, file_id: str) -> list[dict]:
    """Extract include_role, import_tasks, and import_playbook edges."""
    edges: list[dict] = []

    for m in _RE_ANSIBLE_INCLUDE_ROLE.finditer(content):
        edges.append(_make_edge(file_id, f"role:{m.group(1)}", "uses_role"))

    for m in _RE_ANSIBLE_INCLUDE_TASKS.finditer(content):
        ref = m.group(1).strip().strip('"').strip("'")
        if ref:
            edges.append(_make_edge(file_id, ref, "includes"))

    for m in _RE_ANSIBLE_INCLUDE_PLAYBOOK.finditer(content):
        ref = m.group(1).strip().strip('"').strip("'")
        if ref:
            edges.append(_make_edge(file_id, ref, "imports"))

    return edges


# ---------------------------------------------------------------------------
# 4B-6: CircleCI
# ---------------------------------------------------------------------------

_RE_CCI_WORKFLOW_NAME = re.compile(r"^  ([A-Za-z0-9_\-]+)\s*:", re.MULTILINE)
_RE_CCI_JOB_IN_WF    = re.compile(r"^\s+-\s+([A-Za-z0-9_\-]+)\s*(?::|$)", re.MULTILINE)
_RE_CCI_REQUIRES     = re.compile(r"^\s+requires\s*:", re.MULTILINE)
_RE_CCI_CONTEXT      = re.compile(r"^\s+-\s+([A-Za-z0-9_\-]+)\s*$", re.MULTILINE)
_RE_CCI_JOB_DEF      = re.compile(r"^  ([A-Za-z0-9_\-]+)\s*:", re.MULTILINE)


def _cci_find_block_end(lines: list[str], start: int) -> int:
    """Find the end of a 2-space-indented YAML block starting at ``start``."""
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if not line.startswith("   ") and not line.startswith("\t  "):
            return i - 1
    return len(lines) - 1


def extract_symbols_circleci(content: str, file_path: str) -> list[dict]:
    """Extract workflow and job symbols from a CircleCI config.yml."""
    lines   = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    # ── workflows ────────────────────────────────────────────────────────────
    wf_m = re.search(r"^workflows\s*:", content, re.MULTILINE)
    if wf_m:
        wf_line = _line_of(content, wf_m.start())
        for i in range(wf_line + 1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            if not line.startswith(" ") and not line.startswith("\t"):
                break
            m = re.match(r"^  ([A-Za-z0-9_\-]+)\s*:", line)
            if m:
                wf_name = m.group(1)
                if wf_name in ("version",) or wf_name in seen:
                    continue
                seen.add(wf_name)
                end_ln = _cci_find_block_end(lines, i)
                sym = _make_symbol(
                    file_path, f"workflow:{wf_name}", "use_case",
                    i, end_ln, lines, "high",
                    ["circleci", "workflow", "ci", wf_name],
                )
                symbols.append(sym)

    # ── jobs ─────────────────────────────────────────────────────────────────
    jobs_m = re.search(r"^jobs\s*:", content, re.MULTILINE)
    if jobs_m:
        jobs_line = _line_of(content, jobs_m.start())
        for i in range(jobs_line + 1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            if not line.startswith(" ") and not line.startswith("\t"):
                break
            m = re.match(r"^  ([A-Za-z0-9_\-]+)\s*:", line)
            if m:
                job_name = m.group(1)
                if job_name in seen:
                    continue
                seen.add(job_name)
                end_ln = _cci_find_block_end(lines, i)
                sym = _make_symbol(
                    file_path, f"job:{job_name}", "utility",
                    i, end_ln, lines, "high",
                    ["circleci", "job", "ci", job_name],
                )
                symbols.append(sym)

    return symbols


def parse_imports_circleci(content: str, file_id: str) -> list[dict]:
    """Extract 'requires' dependency edges and 'context' usage edges from CircleCI."""
    edges: list[dict] = []
    lines = content.splitlines()

    # Parse workflow jobs block to extract requires/context edges
    wf_m = re.search(r"^workflows\s*:", content, re.MULTILINE)
    if not wf_m:
        return edges

    wf_line = _line_of(content, wf_m.start())
    current_wf: str | None = None
    current_job: str | None = None
    in_requires = False
    in_context  = False

    for i in range(wf_line + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        # Top-level block end
        if not line.startswith(" ") and not line.startswith("\t"):
            break

        # Workflow name (2-space indent)
        m_wf = re.match(r"^  ([A-Za-z0-9_\-]+)\s*:", line)
        if m_wf and not line.startswith("    "):
            current_wf  = m_wf.group(1)
            current_job = None
            in_requires = False
            in_context  = False
            continue

        # requires: block  (check before job-name detection to avoid collision)
        if re.match(r"^\s+requires\s*:", line):
            in_requires = True
            in_context  = False
            continue

        # context: block
        if re.match(r"^\s+context\s*:", line):
            in_context  = True
            in_requires = False
            continue

        # List items under requires/context take priority over job detection
        m_item = re.match(r"^\s+-\s+([A-Za-z0-9_\-]+)\s*$", line)
        if m_item and (in_requires or in_context):
            item = m_item.group(1)
            src = f"{file_id}::job:{current_job}" if current_job else file_id
            if in_requires:
                edges.append(_make_edge(src, f"job:{item}", "needs"))
            elif in_context:
                edges.append(_make_edge(src, f"context:{item}", "uses_context"))
            continue

        # Job name inside workflow jobs list: "- jobname" or "- jobname:"
        m_job = re.match(r"^\s+-\s+([A-Za-z0-9_\-]+)\s*(?::|$)", line)
        if m_job:
            current_job = m_job.group(1)
            in_requires = False
            in_context  = False
            continue

        # Anything non-list resets the flags
        if line.strip() and not line.strip().startswith("-"):
            in_requires = False
            in_context  = False

    return edges


def _is_circleci(file_path: str) -> bool:
    """Detect .circleci/config.yml files."""
    p = Path(file_path)
    parts = p.parts
    for i, part in enumerate(parts):
        if part == ".circleci" and i + 1 < len(parts):
            return p.suffix.lower() in (".yml", ".yaml")
    return False


# ---------------------------------------------------------------------------
# 4B-7: Buildkite
# ---------------------------------------------------------------------------

_RE_BK_LABEL    = re.compile(r"^\s+-\s+label\s*:\s*(.+)$",      re.MULTILINE)
_RE_BK_TRIGGER  = re.compile(r"^\s+-\s+trigger\s*:\s*(.+)$",    re.MULTILINE)
_RE_BK_DEPENDS  = re.compile(r"^\s+depends_on\s*:\s*(.+)$",     re.MULTILINE)
_RE_BK_COMMAND  = re.compile(r"^\s+command\s*:\s*(.+)$",        re.MULTILINE)


def extract_symbols_buildkite(content: str, file_path: str) -> list[dict]:
    """Extract step symbols from a Buildkite pipeline file."""
    lines   = content.splitlines()
    symbols: list[dict] = []
    seen:   set[str] = set()

    for m in _RE_BK_LABEL.finditer(content):
        label = m.group(1).strip().strip('"').strip("'")
        # Skip emoji-only or blank labels
        if not label or label in seen:
            continue
        seen.add(label)
        ln     = _line_of(content, m.start())
        end_ln = ln
        # Find end of this step block (next "- " at same indent level)
        indent = len(lines[ln]) - len(lines[ln].lstrip()) if ln < len(lines) else 0
        for j in range(ln + 1, min(ln + 50, len(lines))):
            ll = lines[j]
            if not ll.strip():
                continue
            cur_indent = len(ll) - len(ll.lstrip())
            if cur_indent <= indent and re.match(r"\s*-\s+", ll):
                break
            end_ln = j
        sym = _make_symbol(
            file_path, f"step:{label}", "utility",
            ln, end_ln, lines, "high",
            ["buildkite", "step", "ci", label.lower()],
        )
        symbols.append(sym)

    # Trigger steps — no label but a trigger: key
    for m in _RE_BK_TRIGGER.finditer(content):
        pipeline = m.group(1).strip().strip('"').strip("'")
        name = f"trigger:{pipeline}"
        if name in seen:
            continue
        seen.add(name)
        ln = _line_of(content, m.start())
        sym = _make_symbol(
            file_path, name, "utility",
            ln, ln, lines, "high",
            ["buildkite", "trigger", "ci", pipeline],
        )
        symbols.append(sym)

    return symbols


def parse_imports_buildkite(content: str, file_id: str) -> list[dict]:
    """Extract depends_on and trigger edges from a Buildkite pipeline file."""
    edges: list[dict] = []
    lines = content.splitlines()

    # Map label → step id (label string used as identifier)
    step_at_line: dict[int, str] = {}
    for m in _RE_BK_LABEL.finditer(content):
        label = m.group(1).strip().strip('"').strip("'")
        ln    = _line_of(content, m.start())
        step_at_line[ln] = label

    def nearest_step(line_no: int) -> str | None:
        """Return the label of the step containing line_no."""
        best: str | None = None
        best_ln = -1
        for ln, lbl in step_at_line.items():
            if ln <= line_no and ln > best_ln:
                best    = lbl
                best_ln = ln
        return best

    for m in _RE_BK_DEPENDS.finditer(content):
        raw    = m.group(1).strip().strip('"').strip("'")
        ln     = _line_of(content, m.start())
        src_lbl = nearest_step(ln)
        src    = f"{file_id}::step:{src_lbl}" if src_lbl else file_id
        for dep in re.findall(r"[A-Za-z0-9_\-]+", raw):
            edges.append(_make_edge(src, f"step:{dep}", "needs"))

    for m in _RE_BK_TRIGGER.finditer(content):
        pipeline = m.group(1).strip().strip('"').strip("'")
        ln       = _line_of(content, m.start())
        src_lbl  = nearest_step(ln)
        src      = f"{file_id}::step:{src_lbl}" if src_lbl else file_id
        edges.append(_make_edge(src, f"pipeline:{pipeline}", "dispatches_to"))

    return edges


def _is_buildkite(file_path: str) -> bool:
    """Detect .buildkite/pipeline.yml or buildkite.yml files."""
    name = os.path.basename(file_path).lower()
    p    = Path(file_path)
    parts = p.parts
    # .buildkite/pipeline.yml
    for i, part in enumerate(parts):
        if part == ".buildkite" and i + 1 < len(parts):
            return p.suffix.lower() in (".yml", ".yaml")
    # standalone buildkite.yml at any level
    return name == "buildkite.yml" or name == "buildkite.yaml"


# ---------------------------------------------------------------------------
# File type classification
# ---------------------------------------------------------------------------

def is_infra_file(file_path: str, content_hint: str = "") -> bool:
    """Return True if this file should be processed by this module."""
    name = os.path.basename(file_path)
    ext  = Path(file_path).suffix.lower()

    # Jenkins has no extension
    if _is_jenkins(file_path):
        return True

    # Terraform
    if _is_terraform(file_path):
        return True

    # Named YAML files that are always infra
    if _is_github_actions(file_path):
        return True
    if _is_gitlab_ci(file_path):
        return True
    if _is_azure_pipelines(file_path):
        return True
    if _is_helm_chart(file_path):
        return True
    if _is_kustomize(file_path):
        return True
    if _is_docker_compose(file_path):
        return True
    if _is_circleci(file_path):
        return True
    if _is_buildkite(file_path):
        return True

    # Generic YAML files: detect by content
    if ext in (".yml", ".yaml") and content_hint:
        if _is_kubernetes(content_hint):
            return True
        if _is_ansible(content_hint):
            return True

    return False


def _detect_yaml_type(file_path: str, content: str) -> str:
    """
    Classify a .yml/.yaml file into one of:
      'github_actions' | 'gitlab_ci' | 'azure_pipelines' | 'helm' |
      'kustomize' | 'docker_compose' | 'kubernetes' | 'ansible' | 'unknown'
    """
    if _is_github_actions(file_path):
        return "github_actions"
    if _is_gitlab_ci(file_path):
        return "gitlab_ci"
    if _is_azure_pipelines(file_path):
        return "azure_pipelines"
    if _is_helm_chart(file_path):
        return "helm"
    if _is_kustomize(file_path):
        return "kustomize"
    if _is_docker_compose(file_path):
        return "docker_compose"
    if _is_circleci(file_path):
        return "circleci"
    if _is_buildkite(file_path):
        return "buildkite"
    # Content-based detection — order matters
    if _is_kubernetes(content):
        return "kubernetes"
    if _is_ansible(content):
        return "ansible"
    # GitLab CI can have any filename — last resort heuristic
    if re.search(r"^stages\s*:", content, re.MULTILINE) and re.search(
        r"^\S[A-Za-z0-9_\-]+\s*:\s*$", content, re.MULTILINE
    ):
        return "gitlab_ci"
    return "unknown"


# ---------------------------------------------------------------------------
# Main dispatchers
# ---------------------------------------------------------------------------

def extract_infra_symbols(content: str, file_path: str) -> list[dict]:
    """
    Dispatch to the correct parser and return the list of symbol dicts.

    Each dict follows the standard GrapeRoot symbol format:
      id, name, symbol_type, line_start, line_end, body_hash,
      confidence, exported, keywords
    """
    ext  = Path(file_path).suffix.lower()
    name = os.path.basename(file_path)

    if _is_terraform(file_path):
        return extract_symbols_terraform(content, file_path)

    if _is_jenkins(file_path):
        return extract_symbols_jenkins(content, file_path)

    if ext in (".yml", ".yaml"):
        yaml_type = _detect_yaml_type(file_path, content)
        if yaml_type == "github_actions":
            return extract_symbols_github_actions(content, file_path)
        if yaml_type == "gitlab_ci":
            return extract_symbols_gitlab_ci(content, file_path)
        if yaml_type == "azure_pipelines":
            return extract_symbols_azure_pipelines(content, file_path)
        if yaml_type == "helm":
            return extract_symbols_helm(content, file_path)
        if yaml_type == "kustomize":
            return extract_symbols_kustomize(content, file_path)
        if yaml_type == "docker_compose":
            return extract_symbols_docker_compose(content, file_path)
        if yaml_type == "circleci":
            return extract_symbols_circleci(content, file_path)
        if yaml_type == "buildkite":
            return extract_symbols_buildkite(content, file_path)
        if yaml_type == "kubernetes":
            return extract_symbols_kubernetes(content, file_path)
        if yaml_type == "ansible":
            return extract_symbols_ansible(content, file_path)

    return []


def parse_infra_imports(content: str, file_id: str, file_path: str) -> list[dict]:
    """
    Dispatch to the correct import parser and return a list of edge dicts.

    Each edge dict: { "from": str, "to": str, "rel": str }
    """
    ext = Path(file_path).suffix.lower()

    if _is_terraform(file_path):
        return parse_imports_terraform(content, file_id)

    if _is_jenkins(file_path):
        return _parse_imports_jenkins(content, file_id)

    if ext in (".yml", ".yaml"):
        yaml_type = _detect_yaml_type(file_path, content)
        if yaml_type == "github_actions":
            return parse_imports_github_actions(content, file_id)
        if yaml_type == "gitlab_ci":
            return parse_imports_gitlab_ci(content, file_id)
        if yaml_type == "azure_pipelines":
            return _parse_imports_azure_pipelines(content, file_id)
        if yaml_type == "helm":
            # Helm dependencies are surfaced as symbols; no separate import edges needed
            return []
        if yaml_type == "kustomize":
            refs = _kustomize_refs(content)
            return [_make_edge(file_id, r, "includes") for r in refs]
        if yaml_type == "docker_compose":
            return parse_imports_docker_compose(content, file_id)
        if yaml_type == "circleci":
            return parse_imports_circleci(content, file_id)
        if yaml_type == "buildkite":
            return parse_imports_buildkite(content, file_id)
        if yaml_type == "kubernetes":
            return parse_imports_kubernetes(content, file_id)
        if yaml_type == "ansible":
            return parse_imports_ansible(content, file_id)

    return []
