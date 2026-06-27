#!/usr/bin/env python3
"""Extended CI/CD symbol/import extractor for GrapeRoot Pro.

Covers seven CI/CD systems not handled by graph_builder_infra.py:
  1. Travis CI          (.travis.yml)
  2. Drone CI           (.drone.yml / .drone.yaml / .drone/*.yml)
  3. Bitbucket Pipelines (bitbucket-pipelines.yml)
  4. ArgoCD             (apiVersion: argoproj.io/v1alpha1)
  5. Tekton             (apiVersion: tekton.dev/*)
  6. Flux (FluxCD)      (apiVersion: *.toolkit.fluxcd.io/*)
  7. TeamCity Kotlin DSL (.teamcity/**/*.kts)

All parsing is regex-only — no external YAML/HCL/Kotlin library required.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CI_EXT_EXTS: set[str] = {".yml", ".yaml", ".kts"}

# ---------------------------------------------------------------------------
# Internal helpers (mirror graph_builder_infra.py conventions)
# ---------------------------------------------------------------------------


def _body_hash(lines: list[str], start: int, end: int) -> str:
    """MD5[:8] of lines[start:end+1] (0-indexed, inclusive)."""
    body = "\n".join(lines[start : end + 1])
    return hashlib.md5(body.encode("utf-8", errors="ignore")).hexdigest()[:8]


def _name_keywords(name: str, extra: list[str] | None = None) -> list[str]:
    """Derive searchable keyword tokens from a symbol name."""
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(word: str) -> None:
        w = re.sub(r"[^a-z0-9]", "", word.lower())
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            tokens.append(w)

    _add(name)
    for part in re.split(r"[^a-zA-Z0-9]+", name):
        _add(part)
    for part in re.sub(
        r"([a-z0-9])([A-Z])", r"\1 \2",
        re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name),
    ).split():
        _add(part)
    if extra:
        for w in extra:
            _add(w)
    return tokens[:12]


def _line_of(content: str, offset: int) -> int:
    """Return 0-indexed line number for a char offset."""
    return content[:offset].count("\n")


def _make_sym(
    file_path: str,
    name: str,
    symbol_type: str,
    line_start: int,
    line_end: int,
    lines: list[str],
    confidence: float = 0.9,
    extra_keywords: list[str] | None = None,
    ci_system: str = "",
) -> dict:
    kw = _name_keywords(name, extra_keywords)
    return {
        "id":          f"{file_path}::{name}",
        "name":        name,
        "symbol_type": symbol_type,
        "line_start":  line_start,
        "line_end":    line_end,
        "body_hash":   _body_hash(lines, line_start, line_end),
        "confidence":  confidence,
        "exported":    True,
        "keywords":    kw,
        "ci_system":   ci_system,
    }


def _make_edge(
    from_id: str, to_id: str, relation: str = "needs", confidence: float = 0.9
) -> dict:
    return {
        "from_id":    from_id,
        "to_id":      to_id,
        "relation":   relation,
        "confidence": confidence,
    }


def _split_yaml_docs(content: str) -> list[str]:
    """Split a YAML file on document-separator lines (^---).

    Returns a non-empty list even for single-document files.
    """
    docs = re.split(r"(?m)^---[ \t]*$", content)
    return [d for d in docs if d.strip()] or [""]


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _basename(file_path: str) -> str:
    return os.path.basename(file_path).lower()


def _is_travis(file_path: str) -> bool:
    return _basename(file_path) == ".travis.yml"


def _is_drone(file_path: str) -> bool:
    base = _basename(file_path)
    if base in (".drone.yml", ".drone.yaml"):
        return True
    # files inside a .drone/ directory
    norm = file_path.replace("\\", "/")
    return "/.drone/" in norm or norm.startswith(".drone/")


def _is_bitbucket(file_path: str) -> bool:
    return _basename(file_path) == "bitbucket-pipelines.yml"


def _is_argocd(content: str) -> bool:
    return bool(re.search(r"apiVersion:\s*argoproj\.io/v1alpha1", content))


def _is_tekton(content: str) -> bool:
    return bool(re.search(r"apiVersion:\s*tekton\.dev/", content))


def _is_flux(content: str) -> bool:
    return bool(re.search(r"apiVersion:\s*\S+\.toolkit\.fluxcd\.io/", content))


def _is_teamcity(file_path: str, ext: str) -> bool:
    if ext != ".kts":
        return False
    norm = file_path.replace("\\", "/")
    return "/.teamcity/" in norm or norm.startswith(".teamcity/")


# ---------------------------------------------------------------------------
# Public API — detection
# ---------------------------------------------------------------------------


def is_ci_extended_file(content: str, file_path: str, ext: str) -> bool:
    """Return True if this file belongs to one of the seven extended CI/CD systems."""
    if ext not in CI_EXT_EXTS:
        return False
    if _is_travis(file_path):
        return True
    if _is_drone(file_path):
        return True
    if _is_bitbucket(file_path):
        return True
    if ext in (".yml", ".yaml"):
        if _is_argocd(content) or _is_tekton(content) or _is_flux(content):
            return True
    if _is_teamcity(file_path, ext):
        return True
    return False


def detect_ci_system(content: str, file_path: str, ext: str) -> str | None:
    """Return the CI/CD system name or None."""
    if ext not in CI_EXT_EXTS:
        return None
    if _is_travis(file_path):
        return "travis"
    if _is_drone(file_path):
        return "drone"
    if _is_bitbucket(file_path):
        return "bitbucket"
    if ext in (".yml", ".yaml"):
        if _is_argocd(content):
            return "argocd"
        if _is_tekton(content):
            return "tekton"
        if _is_flux(content):
            return "flux"
    if _is_teamcity(file_path, ext):
        return "teamcity"
    return None


# ---------------------------------------------------------------------------
# System-specific extractors
# ---------------------------------------------------------------------------

# ── 1. Travis CI ──────────────────────────────────────────────────────────


def _extract_travis(content: str, file_path: str) -> list[dict]:
    """Extract symbols from a .travis.yml file."""
    if not content.strip():
        return []
    lines = content.splitlines()
    symbols: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, stype: str, ln: int, extra: list[str] | None = None) -> None:
        name = name.strip()
        if not name or name in seen_names:
            return
        seen_names.add(name)
        symbols.append(
            _make_sym(file_path, name, stype, ln, ln, lines, 0.85,
                      extra_keywords=extra, ci_system="travis")
        )

    # Language keyword (used as extra kw for everything)
    lang_kw: list[str] = []
    m = re.search(r"(?m)^language:\s*(\S+)", content)
    if m:
        lang_kw = [m.group(1).strip()]

    # Stages block: "stages:\n  - name: X" or "stages:\n  - X"
    in_stages = False
    for i, line in enumerate(lines):
        if re.match(r"^stages:\s*$", line):
            in_stages = True
            continue
        if in_stages:
            if re.match(r"^\S", line) and not re.match(r"^\s*-", line):
                in_stages = False
                continue
            # "  - name: StageName" or "  - StageName"
            m = re.match(r"^\s+-\s+name:\s*(.+)", line)
            if m:
                _add(m.group(1).strip(), "use_case", i, lang_kw)
                continue
            m = re.match(r"^\s+-\s+(\w[\w\s\-\/]+)", line)
            if m:
                val = m.group(1).strip()
                if val and not val.startswith("#"):
                    _add(val, "use_case", i, lang_kw)

    # Jobs with "- stage: X" and optional "name: Y"
    for i, line in enumerate(lines):
        m = re.match(r"^\s+-\s+stage:\s*(.+)", line)
        if m:
            stage_val = m.group(1).strip()
            # Look ahead for a name: line within the same job block
            job_name: str | None = None
            for j in range(i + 1, min(i + 10, len(lines))):
                nm = re.match(r"^\s+name:\s*(.+)", lines[j])
                if nm:
                    job_name = nm.group(1).strip()
                    break
                # stop at next list item
                if re.match(r"^\s+-\s+\S", lines[j]):
                    break
            name = job_name if job_name else f"{stage_val}_job_{i}"
            _add(name, "utility", i, [stage_val] + lang_kw)

    # Environment matrix: "env:\n  - VAR=val"
    in_env = False
    for i, line in enumerate(lines):
        if re.match(r"^env:\s*$", line) or re.match(r"^\s+matrix:\s*$", line):
            in_env = True
            continue
        if in_env:
            if re.match(r"^\S", line) and not re.match(r"^\s*-", line):
                in_env = False
                continue
            m = re.match(r"^\s+-\s+(.+)", line)
            if m:
                entry = m.group(1).strip()
                if entry and not entry.startswith("#"):
                    _add(f"env_{i}", "utility", i, [entry] + lang_kw)

    return symbols


# ── 2. Drone CI ──────────────────────────────────────────────────────────


def _extract_drone(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """Extract symbols and edges from a Drone CI file."""
    if not content.strip():
        return [], []

    lines = content.splitlines()
    symbols: list[dict] = []
    edges: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, stype: str, ln: int, extra: list[str] | None = None) -> dict | None:
        name = name.strip()
        if not name or name in seen_names:
            return None
        seen_names.add(name)
        sym = _make_sym(file_path, name, stype, ln, ln, lines, 0.9,
                        extra_keywords=extra, ci_system="drone")
        symbols.append(sym)
        return sym

    # Detect format version
    is_v1 = bool(re.search(r"(?m)^kind:\s*pipeline", content))
    is_v0 = not is_v1 and bool(re.search(r"(?m)^pipeline:\s*$", content))

    if is_v1:
        # Pipeline name
        m = re.search(r"(?m)^name:\s*(.+)", content)
        pipeline_name = m.group(1).strip() if m else "pipeline"
        pipeline_sym = _add(pipeline_name, "use_case",
                            _line_of(content, m.start()) if m else 0)

        # Steps
        in_steps = False
        for i, line in enumerate(lines):
            if re.match(r"^\s*steps:\s*$", line):
                in_steps = True
                continue
            if in_steps:
                if re.match(r"^\S", line) and not re.match(r"^\s*-", line):
                    in_steps = False
                    continue
                sm = re.match(r"^\s*-\s+name:\s*(.+)", line)
                if sm:
                    step_name = sm.group(1).strip()
                    step_sym = _add(step_name, "utility", i)
                    # depends_on
                    for j in range(i + 1, min(i + 20, len(lines))):
                        if re.match(r"^\s*-\s+name:", lines[j]):
                            break
                        dm = re.match(r"^\s*depends_on:\s*$", lines[j])
                        if dm:
                            for k in range(j + 1, min(j + 20, len(lines))):
                                dep = re.match(r"^\s*-\s+(.+)", lines[k])
                                if dep:
                                    dep_name = dep.group(1).strip()
                                    if step_sym:
                                        edges.append(
                                            _make_edge(
                                                f"{file_path}::{step_name}",
                                                f"{file_path}::{dep_name}",
                                                "needs",
                                            )
                                        )
                                elif re.match(r"^\s*\S", lines[k]):
                                    break

        # Services
        in_services = False
        for i, line in enumerate(lines):
            if re.match(r"^\s*services:\s*$", line):
                in_services = True
                continue
            if in_services:
                if re.match(r"^\S", line) and not re.match(r"^\s*-", line):
                    in_services = False
                    continue
                sm = re.match(r"^\s*-\s+name:\s*(.+)", line)
                if sm:
                    _add(sm.group(1).strip(), "utility", i, ["service"])

    elif is_v0:
        # Drone 0.x: map keys under "pipeline:"
        in_pipeline = False
        for i, line in enumerate(lines):
            if re.match(r"^pipeline:\s*$", line):
                in_pipeline = True
                continue
            if in_pipeline:
                if re.match(r"^\S", line):
                    in_pipeline = False
                    continue
                # Step key: two-space indent, word, colon, then image: on next lines
                m = re.match(r"^  (\w[\w\-]*):\s*$", line)
                if m:
                    # Check next few lines for image:
                    for j in range(i + 1, min(i + 5, len(lines))):
                        if re.match(r"^\s+image:", lines[j]):
                            _add(m.group(1), "utility", i)
                            break

    return symbols, edges


def _extract_drone_imports(content: str, file_path: str) -> list[dict]:
    _, edges = _extract_drone(content, file_path)
    return edges


# ── 3. Bitbucket Pipelines ───────────────────────────────────────────────


def _extract_bitbucket(content: str, file_path: str) -> list[dict]:
    """Extract symbols from bitbucket-pipelines.yml."""
    if not content.strip():
        return []
    lines = content.splitlines()
    symbols: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, stype: str, ln: int, extra: list[str] | None = None) -> None:
        name = name.strip()
        if not name or name in seen_names:
            return
        seen_names.add(name)
        symbols.append(
            _make_sym(file_path, name, stype, ln, ln, lines, 0.9,
                      extra_keywords=extra, ci_system="bitbucket")
        )

    # Pipeline sections — they live at any indent under "pipelines:" in real files
    # so match with optional leading whitespace.
    in_custom = False
    custom_indent: str = ""
    for i, line in enumerate(lines):
        # Match section headers like "  default:", "  custom:", "branches:", etc.
        m = re.match(r"^(\s*)(default|branches|pull-requests|tags|custom):\s*$", line)
        if m:
            section = m.group(2)
            section_indent = m.group(1)
            _add(section, "use_case", i)
            in_custom = (section == "custom")
            custom_indent = section_indent + "  "  # child keys are one more indent level
            continue
        # Custom pipeline names: one extra indent below "custom:"
        if in_custom:
            # If we hit a line at the same or lower indent as the section, stop
            if re.match(r"^\S", line):
                in_custom = False
            elif custom_indent and re.match(
                rf"^{re.escape(custom_indent)}([^\s:][^:]*):\s*$", line
            ):
                custom_name = re.match(
                    rf"^{re.escape(custom_indent)}([^\s:][^:]*):\s*$", line
                ).group(1)
                _add(custom_name.strip(), "use_case", i, ["custom"])

    # Step names: "- step:\n    name: X"
    for i, line in enumerate(lines):
        if re.match(r"^\s*-\s+step:\s*$", line) or re.match(r"^\s*-\s+step:\s*&", line):
            for j in range(i + 1, min(i + 10, len(lines))):
                nm = re.match(r"^\s+name:\s*(.+)", lines[j])
                if nm:
                    _add(nm.group(1).strip(), "utility", j)
                    break
                if re.match(r"^\s*-", lines[j]):
                    break

    # Service definitions: "definitions:\n  services:\n    <name>:"
    in_defs = False
    in_services = False
    for i, line in enumerate(lines):
        if re.match(r"^definitions:\s*$", line):
            in_defs = True
            in_services = False
            continue
        if in_defs:
            if re.match(r"^\S", line):
                in_defs = False
                in_services = False
                continue
            if re.match(r"^  services:\s*$", line):
                in_services = True
                continue
            if in_services:
                if re.match(r"^  \S", line):
                    in_services = False
                    continue
                m = re.match(r"^    (\w[\w\-]*):\s*$", line)
                if m:
                    _add(m.group(1), "utility", i, ["service"])

    return symbols


# ── 4. ArgoCD ─────────────────────────────────────────────────────────────


def _extract_argocd_doc(content: str, file_path: str, lines_offset: int,
                        all_lines: list[str]) -> tuple[list[dict], list[dict]]:
    """Extract from a single ArgoCD YAML document."""
    symbols: list[dict] = []
    edges: list[dict] = []

    kind_m = re.search(r"(?m)^kind:\s*(\w+)", content)
    if not kind_m:
        return symbols, edges
    kind = kind_m.group(1).strip()

    if kind not in ("Application", "AppProject", "ApplicationSet"):
        return symbols, edges

    name_m = re.search(r"(?m)^  name:\s*(.+)", content)
    if not name_m:
        return symbols, edges
    resource_name = name_m.group(1).strip()

    # Absolute line number
    kind_line = lines_offset + _line_of(content, kind_m.start())
    end_line = lines_offset + content.count("\n")

    extra_kw: list[str] = [kind.lower()]

    # Source repoURL
    repo_m = re.search(r"repoURL:\s*(.+)", content)
    if repo_m:
        extra_kw.append(repo_m.group(1).strip())

    # Destination namespace
    ns_m = re.search(r"namespace:\s*(.+)", content)
    if ns_m:
        extra_kw.append(ns_m.group(1).strip())

    # Sync policy prune / selfHeal
    for flag_m in re.finditer(r"(?:prune|selfHeal):\s*(true|false)", content):
        extra_kw.append(f"{flag_m.group(0).replace(' ', '').lower()}")

    sym = _make_sym(file_path, resource_name, "use_case",
                    kind_line, end_line, all_lines, 0.95,
                    extra_keywords=extra_kw, ci_system="argocd")
    symbols.append(sym)

    # ApplicationSet generators → uses edges
    if kind == "ApplicationSet":
        for gen_m in re.finditer(r"- (list|git|matrix|merge|scmProvider|clusterDecisionResource|pullRequest):", content):
            gen_name = gen_m.group(1)
            gen_id = f"{file_path}::{resource_name}_gen_{gen_name}"
            edges.append(_make_edge(sym["id"], gen_id, "uses", 0.7))

    return symbols, edges


def _extract_argocd(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    if not content.strip():
        return [], []
    all_lines = content.splitlines()
    symbols: list[dict] = []
    edges: list[dict] = []
    offset = 0
    for doc in _split_yaml_docs(content):
        syms, edgs = _extract_argocd_doc(doc, file_path, offset, all_lines)
        symbols.extend(syms)
        edges.extend(edgs)
        offset += doc.count("\n") + 1  # +1 for the "---" separator line
    return symbols, edges


# ── 5. Tekton ─────────────────────────────────────────────────────────────


def _extract_tekton_doc(content: str, file_path: str, lines_offset: int,
                        all_lines: list[str]) -> tuple[list[dict], list[dict]]:
    """Extract from a single Tekton YAML document."""
    symbols: list[dict] = []
    edges: list[dict] = []

    kind_m = re.search(r"(?m)^kind:\s*(\w+)", content)
    if not kind_m:
        return symbols, edges
    kind = kind_m.group(1).strip()

    if kind not in ("Pipeline", "Task", "ClusterTask", "PipelineRun", "TaskRun"):
        return symbols, edges

    name_m = re.search(r"(?m)^  name:\s*(.+)", content)
    if not name_m:
        return symbols, edges
    resource_name = name_m.group(1).strip()

    kind_line = lines_offset + _line_of(content, kind_m.start())
    end_line = lines_offset + content.count("\n")

    stype = "use_case" if kind == "Pipeline" else "utility"
    sym = _make_sym(file_path, resource_name, stype,
                    kind_line, end_line, all_lines, 0.95,
                    extra_keywords=[kind.lower()], ci_system="tekton")
    symbols.append(sym)

    if kind in ("Task", "ClusterTask"):
        # Steps within task
        for step_m in re.finditer(r"(?m)^\s+- name:\s*(.+)", content):
            # Confirm we're inside a steps: block by scanning backwards
            snippet = content[: step_m.start()]
            if not re.search(r"steps:\s*$", snippet.rsplit("\n", 20)[-1]
                             if "\n" in snippet else snippet):
                # broader check: last 'steps:' before this match
                pass
            step_name = step_m.group(1).strip()
            step_line = lines_offset + _line_of(content, step_m.start())
            step_sym = _make_sym(file_path, f"{resource_name}.{step_name}", "utility",
                                 step_line, step_line, all_lines, 0.85,
                                 extra_keywords=["step", kind.lower()], ci_system="tekton")
            symbols.append(step_sym)

    if kind == "Pipeline":
        # Tasks in pipeline spec + runAfter deps
        for task_m in re.finditer(r"(?m)^\s+- name:\s*(.+)", content):
            task_name = task_m.group(1).strip()
            task_line = lines_offset + _line_of(content, task_m.start())
            task_sym = _make_sym(file_path, f"{resource_name}.{task_name}", "utility",
                                 task_line, task_line, all_lines, 0.85,
                                 extra_keywords=["task"], ci_system="tekton")
            symbols.append(task_sym)

            # runAfter: [...] inline or block form
            # Look within the next 20 lines of this task entry
            task_snippet = content[task_m.start():]
            # block form: runAfter:\n  - dep1
            block_m = re.search(
                r"runAfter:\s*\n((?:\s+- .+\n)+)", task_snippet[:500]
            )
            if block_m:
                for dep in re.findall(r"^\s+- (.+)", block_m.group(1), re.MULTILINE):
                    dep = dep.strip()
                    edges.append(
                        _make_edge(
                            task_sym["id"],
                            f"{file_path}::{resource_name}.{dep}",
                            "needs",
                        )
                    )
            # inline form: runAfter: [dep1, dep2]
            inline_m = re.search(r"runAfter:\s*\[([^\]]+)\]", task_snippet[:500])
            if inline_m:
                for dep in inline_m.group(1).split(","):
                    dep = dep.strip().strip("\"'")
                    if dep:
                        edges.append(
                            _make_edge(
                                task_sym["id"],
                                f"{file_path}::{resource_name}.{dep}",
                                "needs",
                            )
                        )

    return symbols, edges


def _extract_tekton(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    if not content.strip():
        return [], []
    all_lines = content.splitlines()
    symbols: list[dict] = []
    edges: list[dict] = []
    offset = 0
    for doc in _split_yaml_docs(content):
        syms, edgs = _extract_tekton_doc(doc, file_path, offset, all_lines)
        symbols.extend(syms)
        edges.extend(edgs)
        offset += doc.count("\n") + 1
    return symbols, edges


# ── 6. Flux (FluxCD) ──────────────────────────────────────────────────────

_FLUX_USE_CASE_KINDS = {"Kustomization", "GitRepository"}
_FLUX_UTILITY_KINDS = {"HelmRelease", "HelmRepository", "HelmChart", "ImageUpdateAutomation"}
_FLUX_ALL_KINDS = _FLUX_USE_CASE_KINDS | _FLUX_UTILITY_KINDS


def _extract_flux_doc(content: str, file_path: str, lines_offset: int,
                      all_lines: list[str]) -> tuple[list[dict], list[dict]]:
    symbols: list[dict] = []
    edges: list[dict] = []

    kind_m = re.search(r"(?m)^kind:\s*(\w+)", content)
    if not kind_m:
        return symbols, edges
    kind = kind_m.group(1).strip()
    if kind not in _FLUX_ALL_KINDS:
        return symbols, edges

    name_m = re.search(r"(?m)^  name:\s*(.+)", content)
    if not name_m:
        return symbols, edges
    resource_name = name_m.group(1).strip()

    kind_line = lines_offset + _line_of(content, kind_m.start())
    end_line = lines_offset + content.count("\n")

    stype = "use_case" if kind in _FLUX_USE_CASE_KINDS else "utility"
    extra_kw: list[str] = [kind.lower()]

    url_m = re.search(r"url:\s*(.+)", content)
    if url_m:
        extra_kw.append(url_m.group(1).strip())
    path_m = re.search(r"\bpath:\s*(.+)", content)
    if path_m:
        extra_kw.append(path_m.group(1).strip())
    chart_m = re.search(r"\bchart:\s*(.+)", content)
    if chart_m:
        extra_kw.append(chart_m.group(1).strip())

    sym = _make_sym(file_path, resource_name, stype,
                    kind_line, end_line, all_lines, 0.95,
                    extra_keywords=extra_kw, ci_system="flux")
    symbols.append(sym)

    # dependsOn: block (last item may or may not have a trailing newline)
    dep_block_m = re.search(r"dependsOn:\s*\n((?:\s+- name:\s*.+\n?)+)", content)
    if dep_block_m:
        for dep in re.findall(r"- name:\s*(.+)", dep_block_m.group(1)):
            dep = dep.strip()
            edges.append(
                _make_edge(sym["id"], f"{file_path}::{dep}", "needs")
            )

    return symbols, edges


def _extract_flux(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    if not content.strip():
        return [], []
    all_lines = content.splitlines()
    symbols: list[dict] = []
    edges: list[dict] = []
    offset = 0
    for doc in _split_yaml_docs(content):
        syms, edgs = _extract_flux_doc(doc, file_path, offset, all_lines)
        symbols.extend(syms)
        edges.extend(edgs)
        offset += doc.count("\n") + 1
    return symbols, edges


# ── 7. TeamCity Kotlin DSL ────────────────────────────────────────────────


def _extract_teamcity(content: str, file_path: str) -> tuple[list[dict], list[dict]]:
    """Extract symbols and edges from a TeamCity .kts file."""
    if not content.strip():
        return [], []
    lines = content.splitlines()
    symbols: list[dict] = []
    edges: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, stype: str, ln: int, extra: list[str] | None = None) -> dict | None:
        name = name.strip()
        if not name or name in seen_names:
            return None
        seen_names.add(name)
        sym = _make_sym(file_path, name, stype, ln, ln, lines, 0.9,
                        extra_keywords=extra, ci_system="teamcity")
        symbols.append(sym)
        return sym

    # Top-level project { ... }
    proj_m = re.search(r"(?m)^project\s*\{", content)
    if proj_m:
        _add("project", "use_case", _line_of(content, proj_m.start()), ["project"])

    # BuildType objects: object Foo : BuildType({
    bt_syms: dict[str, dict] = {}  # object_name → symbol
    for bt_m in re.finditer(r"(?m)^object\s+(\w+)\s*:\s*BuildType\s*\(", content):
        obj_name = bt_m.group(1)
        bt_line = _line_of(content, bt_m.start())

        # Look for name = "..." inside the block (next 40 lines)
        snippet_start = bt_m.start()
        snippet = content[snippet_start: snippet_start + 2000]
        name_m = re.search(r'name\s*=\s*"([^"]+)"', snippet)
        display_name = name_m.group(1) if name_m else obj_name

        sym = _add(display_name, "use_case", bt_line, ["buildtype"])
        if sym is None and obj_name != display_name:
            sym = _add(obj_name, "use_case", bt_line, ["buildtype"])
        if sym:
            bt_syms[obj_name] = sym

        # Steps within this BuildType
        steps_m = re.search(r"steps\s*\{", snippet)
        if steps_m:
            steps_snippet = snippet[steps_m.start():]
            for step_m in re.finditer(r'name\s*=\s*"([^"]+)"', steps_snippet[:1000]):
                step_display = step_m.group(1)
                step_line = bt_line + _line_of(snippet[: steps_m.start() + step_m.start()], 0)
                step_line = bt_line + snippet[: steps_m.start() + step_m.start()].count("\n")
                _add(f"{display_name}.{step_display}", "utility", step_line, ["step"])

    # VCS root objects
    for vcs_m in re.finditer(r"(?m)^object\s+(\w+)\s*:\s*VcsRoot\b", content):
        vcs_line = _line_of(content, vcs_m.start())
        _add(vcs_m.group(1), "utility", vcs_line, ["vcsroot"])

    # Snapshot and artifact dependencies
    for dep_block_m in re.finditer(r"dependencies\s*\{([^}]{0,2000})\}", content, re.DOTALL):
        block = dep_block_m.group(1)
        # find the enclosing BuildType
        pre = content[: dep_block_m.start()]
        enclosing_bt: str | None = None
        for bt_m in re.finditer(r"(?m)^object\s+(\w+)\s*:\s*BuildType\s*\(", pre):
            enclosing_bt = bt_m.group(1)

        if enclosing_bt and enclosing_bt in bt_syms:
            from_sym = bt_syms[enclosing_bt]
            for snap_m in re.finditer(r"snapshot\((\w+)\)", block):
                dep_name = snap_m.group(1)
                if dep_name in bt_syms:
                    edges.append(_make_edge(from_sym["id"], bt_syms[dep_name]["id"], "needs"))
                else:
                    edges.append(_make_edge(from_sym["id"],
                                            f"{file_path}::{dep_name}", "needs"))
            for art_m in re.finditer(r"artifacts\((\w+)\)", block):
                dep_name = art_m.group(1)
                if dep_name in bt_syms:
                    edges.append(_make_edge(from_sym["id"], bt_syms[dep_name]["id"], "needs"))
                else:
                    edges.append(_make_edge(from_sym["id"],
                                            f"{file_path}::{dep_name}", "needs"))

    return symbols, edges


# ---------------------------------------------------------------------------
# Public API — extraction
# ---------------------------------------------------------------------------


def extract_ci_extended_symbols(content: str, file_path: str, ext: str) -> list[dict]:
    """Return a list of symbol dicts for the given file, or [] if not recognised."""
    if not content.strip():
        return []
    try:
        system = detect_ci_system(content, file_path, ext)
        if system is None:
            return []
        if system == "travis":
            return _extract_travis(content, file_path)
        if system == "drone":
            syms, _ = _extract_drone(content, file_path)
            return syms
        if system == "bitbucket":
            return _extract_bitbucket(content, file_path)
        if system == "argocd":
            syms, _ = _extract_argocd(content, file_path)
            return syms
        if system == "tekton":
            syms, _ = _extract_tekton(content, file_path)
            return syms
        if system == "flux":
            syms, _ = _extract_flux(content, file_path)
            return syms
        if system == "teamcity":
            syms, _ = _extract_teamcity(content, file_path)
            return syms
    except Exception:
        pass
    return []


def parse_ci_extended_imports(content: str, file_id: str, ext: str) -> list[dict]:
    """Return edge dicts for job/task/step dependencies."""
    if not content.strip():
        return []
    try:
        system = detect_ci_system(content, file_id, ext)
        if system is None:
            return []
        # Travis and Bitbucket have no explicit DAG
        if system in ("travis", "bitbucket"):
            return []
        if system == "drone":
            _, edges = _extract_drone(content, file_id)
            return edges
        if system == "argocd":
            _, edges = _extract_argocd(content, file_id)
            return edges
        if system == "tekton":
            _, edges = _extract_tekton(content, file_id)
            return edges
        if system == "flux":
            _, edges = _extract_flux(content, file_id)
            return edges
        if system == "teamcity":
            _, edges = _extract_teamcity(content, file_id)
            return edges
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# MCP helper
# ---------------------------------------------------------------------------


def get_ci_extended_summary(project_root: str) -> dict:
    """Walk *project_root*, detect extended CI/CD files, extract symbols.

    Returns::

        {
          "ok": True,
          "total_systems_found": int,
          "systems": {"travis": N, "drone": N, ...},
          "pipelines": [{"file": str, "system": str, "symbols": int}, ...]
        }
    """
    system_counts: dict[str, int] = {
        "travis": 0, "drone": 0, "bitbucket": 0,
        "argocd": 0, "tekton": 0, "flux": 0, "teamcity": 0,
    }
    pipelines: list[dict] = []

    for dirpath, _dirs, filenames in os.walk(project_root):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            ext = Path(fname).suffix.lower()
            if ext not in CI_EXT_EXTS:
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            system = detect_ci_system(content, fpath, ext)
            if system is None:
                continue
            syms = extract_ci_extended_symbols(content, fpath, ext)
            system_counts[system] = system_counts.get(system, 0) + 1
            pipelines.append({"file": fpath, "system": system, "symbols": len(syms)})

    active = sum(1 for v in system_counts.values() if v > 0)
    return {
        "ok": True,
        "total_systems_found": active,
        "systems": system_counts,
        "pipelines": pipelines,
    }


# ---------------------------------------------------------------------------
# Self-contained test suite
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    PASS = "[PASS]"
    FAIL = "[FAIL]"

    def check(label: str, condition: bool) -> bool:
        status = PASS if condition else FAIL
        print(f"{status} {label}")
        return condition

    all_ok = True

    # ── Travis CI ──────────────────────────────────────────────────────
    travis_yaml = """
language: python
stages:
  - name: Test
  - Deploy
jobs:
  include:
    - stage: Test
      name: unit-tests
      script: pytest
    - stage: Deploy
      name: deploy-prod
      script: ./deploy.sh
env:
  - DB=mysql
  - DB=postgres
""".strip()

    travis_path = "repo/.travis.yml"
    ok_detect = check(
        "Travis: detect_ci_system",
        detect_ci_system(travis_yaml, travis_path, ".yml") == "travis",
    )
    travis_syms = extract_ci_extended_symbols(travis_yaml, travis_path, ".yml")
    ok_syms = check(
        "Travis: symbols extracted (>=2)",
        len(travis_syms) >= 2,
    )
    ok_uc = check(
        "Travis: at least one use_case (stage)",
        any(s["symbol_type"] == "use_case" for s in travis_syms),
    )
    ok_util = check(
        "Travis: at least one utility (job)",
        any(s["symbol_type"] == "utility" for s in travis_syms),
    )
    ok_sys = check(
        "Travis: ci_system field == 'travis'",
        all(s["ci_system"] == "travis" for s in travis_syms),
    )
    ok_no_edges = check(
        "Travis: no dependency edges",
        parse_ci_extended_imports(travis_yaml, travis_path, ".yml") == [],
    )
    all_ok = all_ok and all([ok_detect, ok_syms, ok_uc, ok_util, ok_sys, ok_no_edges])

    print()

    # ── Drone 1.x ─────────────────────────────────────────────────────
    drone_v1_yaml = """
kind: pipeline
type: docker
name: default

steps:
  - name: build
    image: golang
    commands:
      - go build ./...

  - name: test
    image: golang
    depends_on:
      - build
    commands:
      - go test ./...
""".strip()

    drone_path = "repo/.drone.yml"
    ok_detect = check(
        "Drone 1.x: detect_ci_system",
        detect_ci_system(drone_v1_yaml, drone_path, ".yml") == "drone",
    )
    drone_syms = extract_ci_extended_symbols(drone_v1_yaml, drone_path, ".yml")
    ok_uc = check(
        "Drone 1.x: pipeline use_case present",
        any(s["symbol_type"] == "use_case" and s["name"] == "default"
            for s in drone_syms),
    )
    ok_steps = check(
        "Drone 1.x: steps extracted (>=2)",
        sum(1 for s in drone_syms if s["symbol_type"] == "utility") >= 2,
    )
    drone_edges = parse_ci_extended_imports(drone_v1_yaml, drone_path, ".yml")
    ok_edge = check(
        "Drone 1.x: depends_on edge present",
        len(drone_edges) >= 1,
    )
    all_ok = all_ok and all([ok_detect, ok_uc, ok_steps, ok_edge])

    print()

    # ── Drone 0.x ─────────────────────────────────────────────────────
    drone_v0_yaml = """
pipeline:
  frontend:
    image: node
    commands:
      - npm install

  backend:
    image: golang
    commands:
      - go build
""".strip()

    drone0_path = "repo/.drone.yml"
    ok_detect = check(
        "Drone 0.x: detect_ci_system",
        detect_ci_system(drone_v0_yaml, drone0_path, ".yml") == "drone",
    )
    drone0_syms = extract_ci_extended_symbols(drone_v0_yaml, drone0_path, ".yml")
    ok_steps = check(
        "Drone 0.x: steps extracted (>=2)",
        sum(1 for s in drone0_syms if s["symbol_type"] == "utility") >= 2,
    )
    all_ok = all_ok and all([ok_detect, ok_steps])

    print()

    # ── Bitbucket Pipelines ───────────────────────────────────────────
    bb_yaml = """
pipelines:
  default:
    - step:
        name: Build and test
        script:
          - npm install

  custom:
    deploy-to-production:
      - step:
          name: Deploy
          script:
            - ./deploy.sh

definitions:
  services:
    docker:
      image: docker:dind
""".strip()

    bb_path = "repo/bitbucket-pipelines.yml"
    ok_detect = check(
        "Bitbucket: detect_ci_system",
        detect_ci_system(bb_yaml, bb_path, ".yml") == "bitbucket",
    )
    bb_syms = extract_ci_extended_symbols(bb_yaml, bb_path, ".yml")
    ok_default = check(
        "Bitbucket: 'default' section use_case",
        any(s["name"] == "default" and s["symbol_type"] == "use_case" for s in bb_syms),
    )
    ok_custom_sec = check(
        "Bitbucket: 'custom' section use_case",
        any(s["name"] == "custom" and s["symbol_type"] == "use_case" for s in bb_syms),
    )
    ok_step = check(
        "Bitbucket: step utility present",
        any(s["symbol_type"] == "utility" for s in bb_syms),
    )
    ok_no_edges = check(
        "Bitbucket: no dependency edges",
        parse_ci_extended_imports(bb_yaml, bb_path, ".yml") == [],
    )
    all_ok = all_ok and all([ok_detect, ok_default, ok_custom_sec, ok_step, ok_no_edges])

    print()

    # ── ArgoCD ────────────────────────────────────────────────────────
    argocd_yaml = """
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: guestbook
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    targetRevision: HEAD
    path: guestbook
  destination:
    server: https://kubernetes.default.svc
    namespace: guestbook
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
""".strip()

    argocd_path = "k8s/apps/guestbook.yaml"
    ok_detect = check(
        "ArgoCD: detect_ci_system",
        detect_ci_system(argocd_yaml, argocd_path, ".yaml") == "argocd",
    )
    argocd_syms = extract_ci_extended_symbols(argocd_yaml, argocd_path, ".yaml")
    ok_sym = check(
        "ArgoCD: Application use_case extracted",
        any(s["name"] == "guestbook" and s["symbol_type"] == "use_case"
            for s in argocd_syms),
    )
    ok_ci = check(
        "ArgoCD: ci_system == 'argocd'",
        all(s["ci_system"] == "argocd" for s in argocd_syms),
    )
    all_ok = all_ok and all([ok_detect, ok_sym, ok_ci])

    print()

    # ── Tekton ────────────────────────────────────────────────────────
    tekton_yaml = """
apiVersion: tekton.dev/v1beta1
kind: Pipeline
metadata:
  name: build-and-push
spec:
  tasks:
    - name: fetch-source
      taskRef:
        name: git-clone
    - name: build-image
      runAfter: [fetch-source]
      taskRef:
        name: kaniko
    - name: run-tests
      runAfter:
        - build-image
      taskRef:
        name: pytest
""".strip()

    tekton_path = "pipelines/build.yaml"
    ok_detect = check(
        "Tekton: detect_ci_system",
        detect_ci_system(tekton_yaml, tekton_path, ".yaml") == "tekton",
    )
    tekton_syms = extract_ci_extended_symbols(tekton_yaml, tekton_path, ".yaml")
    ok_pipe = check(
        "Tekton: Pipeline use_case extracted",
        any(s["name"] == "build-and-push" and s["symbol_type"] == "use_case"
            for s in tekton_syms),
    )
    ok_task = check(
        "Tekton: task utility symbols extracted",
        sum(1 for s in tekton_syms if s["symbol_type"] == "utility") >= 1,
    )
    tekton_edges = parse_ci_extended_imports(tekton_yaml, tekton_path, ".yaml")
    ok_edge = check(
        "Tekton: runAfter edges present (>=2)",
        len(tekton_edges) >= 1,
    )
    all_ok = all_ok and all([ok_detect, ok_pipe, ok_task, ok_edge])

    print()

    # ── Flux ──────────────────────────────────────────────────────────
    flux_yaml = """
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  interval: 10m0s
  path: ./apps/staging
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  dependsOn:
    - name: infra-controllers
    - name: infra-configs
""".strip()

    flux_path = "clusters/staging/apps.yaml"
    ok_detect = check(
        "Flux: detect_ci_system",
        detect_ci_system(flux_yaml, flux_path, ".yaml") == "flux",
    )
    flux_syms = extract_ci_extended_symbols(flux_yaml, flux_path, ".yaml")
    ok_sym = check(
        "Flux: Kustomization use_case extracted",
        any(s["name"] == "apps" and s["symbol_type"] == "use_case" for s in flux_syms),
    )
    flux_edges = parse_ci_extended_imports(flux_yaml, flux_path, ".yaml")
    ok_edges = check(
        "Flux: dependsOn edges (>=2)",
        len(flux_edges) >= 2,
    )
    all_ok = all_ok and all([ok_detect, ok_sym, ok_edges])

    print()

    # ── TeamCity Kotlin DSL ───────────────────────────────────────────
    tc_kts = """
import jetbrains.buildServer.configs.kotlin.*

object MyProject : Project({
    buildType(Build)
    buildType(Deploy)
})

object Build : BuildType({
    name = "Build and Test"

    steps {
        script {
            name = "compile"
            scriptContent = "mvn compile"
        }
        script {
            name = "test"
            scriptContent = "mvn test"
        }
    }
})

object Deploy : BuildType({
    name = "Deploy to Prod"

    dependencies {
        snapshot(Build)
    }
})
""".strip()

    tc_path = ".teamcity/settings.kts"
    ok_detect = check(
        "TeamCity: detect_ci_system",
        detect_ci_system(tc_kts, tc_path, ".kts") == "teamcity",
    )
    tc_syms = extract_ci_extended_symbols(tc_kts, tc_path, ".kts")
    ok_bt = check(
        "TeamCity: BuildType use_case symbols (>=2)",
        sum(1 for s in tc_syms if s["symbol_type"] == "use_case") >= 2,
    )
    ok_step = check(
        "TeamCity: step utility symbols present",
        sum(1 for s in tc_syms if s["symbol_type"] == "utility") >= 1,
    )
    tc_edges = parse_ci_extended_imports(tc_kts, tc_path, ".kts")
    ok_edge = check(
        "TeamCity: snapshot dependency edge present",
        len(tc_edges) >= 1,
    )
    all_ok = all_ok and all([ok_detect, ok_bt, ok_step, ok_edge])

    print()

    # ── Edge cases ────────────────────────────────────────────────────
    ok_empty = check(
        "Edge case: empty file → no symbols",
        extract_ci_extended_symbols("", ".travis.yml", ".yml") == [],
    )
    ok_unknown = check(
        "Edge case: unknown file → None system",
        detect_ci_system("foo: bar\n", "random.yml", ".yml") is None,
    )
    ok_multi_doc = check(
        "Edge case: multi-doc Flux YAML",
        len(
            extract_ci_extended_symbols(
                flux_yaml + "\n---\n" + flux_yaml.replace("name: apps", "name: infra"),
                "clusters/staging/multi.yaml",
                ".yaml",
            )
        )
        >= 2,
    )
    all_ok = all_ok and all([ok_empty, ok_unknown, ok_multi_doc])

    print()
    if all_ok:
        print("All tests passed.")
    else:
        print("Some tests FAILED.")
        sys.exit(1)
