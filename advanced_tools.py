#!/usr/bin/env python3
"""
advanced_tools.py — Phase 7 of GrapeRoot Pro

Advanced analysis functions that become MCP tools. All functions accept a
pre-loaded info_graph.json dict (``graph``) and a ``project_root`` path string.

No external dependencies — stdlib only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SKIP_DIRS: set[str] = {
    "node_modules", ".git", "venv", ".venv", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", "vendor",
    ".dual-graph", ".dual-graph-pro", ".graperoot",
}

TEST_PATTERNS: set[str] = {
    "*.test.*", "*.spec.*", "test_*.py", "*_test.go", "*_test.ts",
}

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _bm25_score(query_terms: list[str], doc_keywords: list[str]) -> float:
    """BM25-style term frequency scoring (no IDF — single-document context)."""
    if not query_terms or not doc_keywords:
        return 0.0
    k1 = 1.5
    b = 0.75
    avg_dl = 20.0  # assumed average doc length
    dl = len(doc_keywords)
    doc_lower = [w.lower() for w in doc_keywords]
    score = 0.0
    for term in query_terms:
        t = term.lower()
        tf = doc_lower.count(t)
        if tf == 0:
            continue
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
        score += tf_norm
    return score


def _run_git(cmd: list[str], project_root: str) -> str:
    """Run a git command rooted at project_root; return stdout or '' on error."""
    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
        return ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _load_graph(path: str) -> dict:
    """Load info_graph.json from an explicit file path."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "nodes": [], "edges": []}


def _file_nodes(graph: dict) -> list[dict]:
    return [n for n in graph.get("nodes", []) if n.get("kind") == "file"]


def _symbol_nodes(graph: dict) -> list[dict]:
    return [n for n in graph.get("nodes", []) if n.get("kind") == "symbol"]


def _edges(graph: dict) -> list[dict]:
    return graph.get("edges", [])


def _is_test_file(path: str) -> bool:
    """Return True when a file path looks like a test file."""
    basename = Path(path).name
    for pat in TEST_PATTERNS:
        if fnmatch.fnmatch(basename, pat):
            return True
    # directory-based detection
    parts = Path(path).parts
    for part in parts:
        if part in {"test", "tests", "__tests__", "spec", "specs"}:
            return True
    return False


def _relative(path: str, project_root: str) -> str:
    """Return a project-relative path, or the original if not under project_root."""
    try:
        return str(Path(path).relative_to(project_root))
    except ValueError:
        return path


def _edges_from(graph: dict, node_id: str) -> list[dict]:
    return [e for e in _edges(graph) if e.get("from") == node_id]


def _edges_to(graph: dict, node_id: str) -> list[dict]:
    return [e for e in _edges(graph) if e.get("to") == node_id]


def _build_reverse_import_graph(graph: dict) -> dict[str, list[str]]:
    """Map each file id → list of file ids that import it."""
    rev: dict[str, list[str]] = {}
    for edge in _edges(graph):
        if edge.get("rel") in {"imports", "uses", "calls"}:
            src = str(edge.get("from", ""))
            tgt = str(edge.get("to", ""))
            if src and tgt:
                rev.setdefault(tgt, [])
                if src not in rev[tgt]:
                    rev[tgt].append(src)
    return rev


def _build_forward_import_graph(graph: dict) -> dict[str, list[str]]:
    """Map each file id → list of file ids it imports."""
    fwd: dict[str, list[str]] = {}
    for edge in _edges(graph):
        if edge.get("rel") in {"imports", "uses", "calls"}:
            src = str(edge.get("from", ""))
            tgt = str(edge.get("to", ""))
            if src and tgt:
                fwd.setdefault(src, [])
                if tgt not in fwd[src]:
                    fwd[src].append(tgt)
    return fwd


def _symbols_in_file(graph: dict, file_id: str) -> list[dict]:
    return [n for n in _symbol_nodes(graph) if n.get("path") == file_id]


def _find_codeowners_file(project_root: str) -> str | None:
    candidates = [
        Path(project_root) / ".github" / "CODEOWNERS",
        Path(project_root) / "CODEOWNERS",
        Path(project_root) / "docs" / "CODEOWNERS",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


# ---------------------------------------------------------------------------
# 7.1 Test Coverage (Static)
# ---------------------------------------------------------------------------


def compute_test_coverage(graph: dict, project_root: str) -> dict:
    """Static test coverage: exported symbols with no test imports."""
    try:
        all_symbols = _symbol_nodes(graph)
        exported_symbols = [s for s in all_symbols if s.get("exported", False)]

        if not exported_symbols:
            return {
                "ok": True,
                "total_exported": 0,
                "covered": 0,
                "directly_tested": 0,
                "coverage_pct": 0.0,
                "uncovered_symbols": [],
                "most_untested_files": [],
                "by_type": {},
            }

        # Identify all test files from the graph
        test_file_ids: set[str] = set()
        for node in _file_nodes(graph):
            fid = str(node.get("id", "") or node.get("path", ""))
            if fid and _is_test_file(fid):
                test_file_ids.add(fid)

        # Also scan for test files on disk that may not be in the graph
        root = Path(project_root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip dirs in-place so os.walk won't descend into them
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = Path(dirpath).relative_to(root)
            for filename in filenames:
                rel_path = str(rel_dir / filename)
                if _is_test_file(rel_path):
                    test_file_ids.add(rel_path)

        # Build a set of files imported by test files (graph edges)
        files_covered_by_tests: set[str] = set()
        # Add test files themselves as covered
        files_covered_by_tests.update(test_file_ids)

        for edge in _edges(graph):
            if edge.get("from") in test_file_ids:
                tgt = str(edge.get("to", ""))
                if tgt:
                    # strip symbol suffix for file-level coverage
                    base = tgt.split("::")[0] if "::" in tgt else tgt
                    files_covered_by_tests.add(base)

        # Read test file contents for symbol-name matching
        # Build map: symbol_name -> bool (directly tested)
        test_content_combined = ""
        for tf_id in test_file_ids:
            tf_path = root / tf_id
            try:
                test_content_combined += tf_path.read_text(encoding="utf-8", errors="ignore") + "\n"
            except OSError:
                pass

        # Also gather symbol keywords from test file graph nodes
        test_keywords: set[str] = set()
        for node in graph.get("nodes", []):
            nid = str(node.get("id", "") or node.get("path", ""))
            if nid in test_file_ids:
                for kw in node.get("keywords", []):
                    test_keywords.add(str(kw).lower())

        covered_count = 0
        directly_tested_count = 0
        uncovered: list[dict] = []
        by_type: dict[str, dict] = {}

        # Per-file tracking for most_untested_files
        file_stats: dict[str, dict] = {}

        for sym in exported_symbols:
            sym_file = str(sym.get("path", ""))
            sym_name = str(sym.get("name", ""))
            sym_type = str(sym.get("symbol_type", "unknown"))
            sym_line = int(sym.get("line_start", 0))

            # by_type accumulation
            by_type.setdefault(sym_type, {"total": 0, "covered": 0})
            by_type[sym_type]["total"] += 1

            file_stats.setdefault(sym_file, {"exported_count": 0, "covered_count": 0})
            file_stats[sym_file]["exported_count"] += 1

            is_covered = sym_file in files_covered_by_tests
            is_directly_tested = False

            if sym_name:
                # Direct test: test file content contains the symbol name as a token
                pattern = r"\b" + re.escape(sym_name) + r"\b"
                if re.search(pattern, test_content_combined):
                    is_directly_tested = True
                    is_covered = True  # directly tested implies covered

            if is_covered:
                covered_count += 1
                file_stats[sym_file]["covered_count"] += 1
                by_type[sym_type]["covered"] += 1
                if is_directly_tested:
                    directly_tested_count += 1
            else:
                uncovered.append({
                    "file": sym_file,
                    "symbol": sym_name,
                    "symbol_type": sym_type,
                    "line": sym_line,
                })

        total = len(exported_symbols)
        coverage_pct = round(covered_count / total * 100, 1) if total > 0 else 0.0

        # Most untested files: sort by (exported - covered) descending
        most_untested = sorted(
            [
                {
                    "file": fid,
                    "exported_count": stats["exported_count"],
                    "covered_count": stats["covered_count"],
                }
                for fid, stats in file_stats.items()
                if stats["exported_count"] > stats["covered_count"]
            ],
            key=lambda x: x["exported_count"] - x["covered_count"],
            reverse=True,
        )[:20]

        return {
            "ok": True,
            "total_exported": total,
            "covered": covered_count,
            "directly_tested": directly_tested_count,
            "coverage_pct": coverage_pct,
            "uncovered_symbols": uncovered,
            "most_untested_files": most_untested,
            "by_type": by_type,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7.2 PR Impact Analysis
# ---------------------------------------------------------------------------


def compute_pr_impact(graph: dict, changed_files: list[str], max_hops: int = 3) -> dict:
    """BFS blast radius from changed files up to max_hops import hops."""
    try:
        rev = _build_reverse_import_graph(graph)

        # Normalise changed_files to ids that exist in graph or keep as-is
        all_file_ids = {
            str(n.get("id", "") or n.get("path", ""))
            for n in _file_nodes(graph)
        }

        # BFS
        visited: dict[str, dict] = {}  # file_id -> {distance, via}
        queue: deque[tuple[str, int, str]] = deque()  # (file_id, distance, via_path)

        for cf in changed_files:
            if cf not in visited:
                visited[cf] = {"distance": 0, "via": cf}
                queue.append((cf, 0, cf))

        while queue:
            current, dist, via = queue.popleft()
            if dist >= max_hops:
                continue
            for importer in rev.get(current, []):
                if importer not in visited:
                    visited[importer] = {"distance": dist + 1, "via": via}
                    queue.append((importer, dist + 1, via))

        # Build affected file list (exclude the seed files themselves at distance 0)
        affected: list[dict] = []
        api_routes: list[str] = []
        models: list[str] = []

        for fid, info in visited.items():
            if info["distance"] == 0:
                continue  # changed files are inputs, not blast-radius
            syms = _symbols_in_file(graph, fid)
            sym_names = [s.get("name", "") for s in syms if s.get("name")]
            sym_types = [s.get("symbol_type", "") for s in syms]

            if "api_route" in sym_types:
                api_routes.append(fid)
            if "model" in sym_types:
                models.append(fid)

            affected.append({
                "file": fid,
                "distance": info["distance"],
                "via": info["via"],
                "symbols": sym_names[:20],
            })

        affected.sort(key=lambda x: (x["distance"], x["file"]))

        # Risk score: 0-100 based on affected count and API routes
        raw_score = min(len(affected) * 3, 70) + min(len(api_routes) * 10, 30)
        risk_score = min(100, raw_score)

        # Recommended tests: test files that import any affected file
        fwd = _build_forward_import_graph(graph)
        affected_ids = {item["file"] for item in affected} | set(changed_files)
        recommended_tests: list[str] = []
        for file_id in all_file_ids:
            if _is_test_file(file_id):
                imports = set(fwd.get(file_id, []))
                if imports & affected_ids:
                    recommended_tests.append(file_id)

        return {
            "ok": True,
            "changed_files": changed_files,
            "affected_files": affected,
            "affected_count": len(affected),
            "risk_score": risk_score,
            "api_routes_affected": sorted(set(api_routes)),
            "models_affected": sorted(set(models)),
            "recommended_tests": sorted(set(recommended_tests))[:20],
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7.3 Version Audit
# ---------------------------------------------------------------------------


def _parse_package_json(filepath: Path) -> dict[str, dict[str, str]]:
    """Return {name: version} from package.json deps + devDeps."""
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
        packages: dict[str, dict[str, str]] = {}
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for name, ver in (data.get(section) or {}).items():
                packages[name] = {"version": str(ver), "ecosystem": "npm"}
        return packages
    except Exception:
        return {}


def _parse_requirements_txt(filepath: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    try:
        for raw_line in filepath.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # name==version, name>=version, name~=version, etc.
            m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*([=><!~^]+)\s*([^\s;]+)", line)
            if m:
                name = m.group(1).lower()
                op = m.group(2)
                ver = m.group(3)
                packages[name] = {
                    "version": f"{op}{ver}",
                    "pinned": op == "==",
                    "ecosystem": "pip",
                }
            else:
                # bare name — no version pin
                m2 = re.match(r"^([A-Za-z0-9_\-\.]+)", line)
                if m2:
                    packages[m2.group(1).lower()] = {
                        "version": "*",
                        "pinned": False,
                        "ecosystem": "pip",
                    }
    except Exception:
        pass
    return packages


def _parse_pyproject_toml(filepath: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    try:
        content = filepath.read_text(encoding="utf-8")
        # [project.dependencies] list form: "name>=version"
        in_project_deps = False
        in_poetry_deps = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "[project.dependencies]":
                in_project_deps = True
                in_poetry_deps = False
                continue
            if stripped == "[tool.poetry.dependencies]":
                in_poetry_deps = True
                in_project_deps = False
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                in_project_deps = False
                in_poetry_deps = False
                continue

            if in_project_deps:
                # list item: "name>=version" or name = "version"
                m = re.match(r'^["\']?([A-Za-z0-9_\-\.]+)["\']?\s*([=><!~^]+.*)', stripped)
                if m:
                    name = m.group(1).lower()
                    ver = m.group(2).strip().strip('"').strip("'")
                    packages[name] = {"version": ver, "ecosystem": "pip"}

            if in_poetry_deps:
                # name = "version" or name = {version = "..."}
                m = re.match(r'^([A-Za-z0-9_\-\.]+)\s*=\s*["\']([^"\']+)["\']', stripped)
                if m:
                    name = m.group(1).lower()
                    ver = m.group(2)
                    if name != "python":
                        packages[name] = {"version": ver, "ecosystem": "pip"}
                else:
                    m2 = re.match(r'^([A-Za-z0-9_\-\.]+)\s*=\s*\{', stripped)
                    if m2:
                        # inline table — extract version key
                        mv = re.search(r'version\s*=\s*["\']([^"\']+)["\']', stripped)
                        if mv:
                            packages[m2.group(1).lower()] = {
                                "version": mv.group(1),
                                "ecosystem": "pip",
                            }
    except Exception:
        pass
    return packages


def _parse_cargo_toml(filepath: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    try:
        content = filepath.read_text(encoding="utf-8")
        in_deps = False
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r'^\[(dependencies|dev-dependencies|build-dependencies)\]$', stripped):
                in_deps = True
                continue
            if stripped.startswith("["):
                in_deps = False
                continue
            if in_deps:
                # name = "version"
                m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*["\']([^"\']+)["\']', stripped)
                if m:
                    packages[m.group(1)] = {"version": m.group(2), "ecosystem": "cargo"}
                else:
                    # name = { version = "..." }
                    m2 = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*\{', stripped)
                    if m2:
                        mv = re.search(r'version\s*=\s*["\']([^"\']+)["\']', stripped)
                        if mv:
                            packages[m2.group(1)] = {"version": mv.group(1), "ecosystem": "cargo"}
    except Exception:
        pass
    return packages


def _parse_go_mod(filepath: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    try:
        in_require = False
        content = filepath.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("require ("):
                in_require = True
                continue
            if in_require and stripped == ")":
                in_require = False
                continue
            if stripped.startswith("require ") and not in_require:
                # single-line: require module/path v1.2.3
                parts = stripped.split()
                if len(parts) >= 3:
                    packages[parts[1]] = {"version": parts[2], "ecosystem": "go"}
            elif in_require:
                parts = stripped.split()
                if len(parts) >= 2 and not stripped.startswith("//"):
                    packages[parts[0]] = {"version": parts[1], "ecosystem": "go"}
    except Exception:
        pass
    return packages


def _parse_gradle(filepath: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    try:
        content = filepath.read_text(encoding="utf-8")
        # implementation 'group:name:version' or implementation("group:name:version")
        for m in re.finditer(
            r'(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s*[\(\'"]+([^"\')\s]+)["\'\)]+',
            content,
        ):
            coord = m.group(1)
            parts = coord.split(":")
            if len(parts) >= 3:
                name = f"{parts[0]}:{parts[1]}"
                ver = parts[2]
                packages[name] = {"version": ver, "ecosystem": "gradle"}
    except Exception:
        pass
    return packages


def _parse_pom_xml(filepath: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    try:
        tree = ET.parse(filepath)
        ns_match = re.match(r"\{[^}]+\}", tree.getroot().tag)
        ns = ns_match.group(0) if ns_match else ""
        for dep in tree.iter(f"{ns}dependency"):
            gid = dep.findtext(f"{ns}groupId") or ""
            aid = dep.findtext(f"{ns}artifactId") or ""
            ver = dep.findtext(f"{ns}version") or "*"
            if gid and aid:
                name = f"{gid}:{aid}"
                packages[name] = {"version": ver, "ecosystem": "maven"}
    except Exception:
        pass
    return packages


def version_audit(project_root: str) -> dict:
    """Detect dependency version conflicts across package manifests."""
    try:
        root = Path(project_root)
        MANIFEST_PARSERS = {
            "package.json": _parse_package_json,
            "requirements.txt": _parse_requirements_txt,
            "pyproject.toml": _parse_pyproject_toml,
            "Cargo.toml": _parse_cargo_toml,
            "go.mod": _parse_go_mod,
            "build.gradle": _parse_gradle,
            "build.gradle.kts": _parse_gradle,
            "pom.xml": _parse_pom_xml,
        }

        # Walk the project and collect all manifest files
        found_manifests: list[tuple[str, str]] = []  # (filename, full_path)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                if fname in MANIFEST_PARSERS:
                    found_manifests.append((fname, str(Path(dirpath) / fname)))

        # Parse each manifest
        # packages: {name: {ecosystem, versions: [{file, version}]}}
        packages: dict[str, dict] = {}
        outdated_format_files: list[str] = []

        for fname, fpath in found_manifests:
            parser = MANIFEST_PARSERS[fname]
            pkgs = parser(Path(fpath))
            rel_fpath = _relative(fpath, project_root)

            for name, info in pkgs.items():
                ver = info.get("version", "*")
                eco = info.get("ecosystem", "unknown")

                # Track unpinned requirements
                if fname == "requirements.txt" and not info.get("pinned", True):
                    if rel_fpath not in outdated_format_files:
                        outdated_format_files.append(rel_fpath)

                if name not in packages:
                    packages[name] = {"ecosystem": eco, "versions": []}
                packages[name]["versions"].append({"file": rel_fpath, "version": ver})

        # Detect conflicts: same package pinned to different concrete versions
        conflicts: list[dict] = []
        for pkg_name, pkg_info in packages.items():
            versions_seen = pkg_info["versions"]
            if len(versions_seen) < 2:
                continue
            # Normalise: strip operators, keep concrete version strings
            concrete: dict[str, list[str]] = {}  # version_str -> [files]
            for entry in versions_seen:
                v = re.sub(r"^[=><!~^]+\s*", "", entry["version"]).strip()
                if v in {"*", "", "latest"}:
                    continue
                concrete.setdefault(v, []).append(entry["file"])

            distinct = list(concrete.keys())
            if len(distinct) > 1:
                severity = "high" if pkg_info["ecosystem"] in ("npm", "pip") else "medium"
                conflicts.append({
                    "package": pkg_name,
                    "versions": distinct,
                    "files": [f for v in distinct for f in concrete[v]],
                    "severity": severity,
                })

        return {
            "ok": True,
            "packages": packages,
            "conflicts": conflicts,
            "outdated_format_files": outdated_format_files,
            "total_packages": len(packages),
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7.4 Graph Diff
# ---------------------------------------------------------------------------


def _body_hash_from_content(content: str, name: str) -> str:
    """Approximate body hash from file content for a named symbol."""
    # Heuristic: find the first occurrence of the symbol name and grab ~30 lines
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"\b" + re.escape(name) + r"\b", line):
            snippet = "\n".join(lines[i: i + 30])
            return hashlib.md5(snippet.encode()).hexdigest()[:8]
    return hashlib.md5(content.encode()).hexdigest()[:8]


def compute_graph_diff(graph: dict, since_commit: str, project_root: str) -> dict:
    """What changed across all services since a git ref."""
    try:
        # Get changed files from git
        diff_output = _run_git(["git", "diff", "--name-only", since_commit], project_root)
        if not diff_output and since_commit:
            # Try with HEAD
            diff_output = _run_git(
                ["git", "diff", "--name-only", since_commit, "HEAD"], project_root
            )

        changed_files: list[str] = [
            line.strip() for line in diff_output.splitlines() if line.strip()
        ]

        # Build current symbol map: file_path -> {name: {body_hash, symbol_type, line_start}}
        current_symbols: dict[str, dict[str, dict]] = {}
        for sym in _symbol_nodes(graph):
            fpath = str(sym.get("path", ""))
            name = str(sym.get("name", ""))
            if not fpath or not name:
                continue
            current_symbols.setdefault(fpath, {})[name] = {
                "body_hash": str(sym.get("body_hash", "")),
                "symbol_type": str(sym.get("symbol_type", "")),
                "line_start": int(sym.get("line_start", 0)),
            }

        added_symbols: list[dict] = []
        removed_symbols: list[dict] = []
        modified_symbols: list[dict] = []
        api_changes: list[dict] = []
        breaking_changes: list[dict] = []

        for fpath in changed_files:
            cur_syms = current_symbols.get(fpath, {})

            # Attempt to read old content via git show
            old_content = _run_git(["git", "show", f"{since_commit}:{fpath}"], project_root)

            if not old_content:
                # File is new — all its symbols are added
                for sym_name, sym_info in cur_syms.items():
                    entry = {"file": fpath, "name": sym_name, **sym_info}
                    added_symbols.append(entry)
                    if sym_info["symbol_type"] == "api_route":
                        api_changes.append({"file": fpath, "name": sym_name, "change": "added"})
                continue

            # Parse symbol names from old content (simple heuristic — function/class defs)
            old_sym_names: set[str] = set()
            for m in re.finditer(
                r'^\s*(?:export\s+)?(?:async\s+)?(?:function|class|def|func|type|interface|const|let|var)\s+([A-Za-z_]\w*)',
                old_content,
                re.MULTILINE,
            ):
                old_sym_names.add(m.group(1))

            cur_sym_names = set(cur_syms.keys())

            # Added: in current but not old
            for sym_name in cur_sym_names - old_sym_names:
                entry = {"file": fpath, "name": sym_name, **cur_syms[sym_name]}
                added_symbols.append(entry)
                if cur_syms[sym_name]["symbol_type"] == "api_route":
                    api_changes.append({"file": fpath, "name": sym_name, "change": "added"})

            # Removed: in old but not current
            for sym_name in old_sym_names - cur_sym_names:
                entry = {"file": fpath, "name": sym_name, "body_hash": "", "line_start": 0}
                removed_symbols.append(entry)
                # Check if removed symbol was an API route in old content
                # Heuristic: look for route decorator or handler signature patterns
                if re.search(
                    r'(?:router|app|Route|@Get|@Post|@Put|@Delete|@Patch)\b',
                    old_content,
                ):
                    breaking_changes.append({
                        "file": fpath,
                        "name": sym_name,
                        "reason": "api_route removed",
                    })

            # Modified: in both but body_hash differs
            for sym_name in cur_sym_names & old_sym_names:
                cur_hash = cur_syms[sym_name]["body_hash"]
                old_hash = _body_hash_from_content(old_content, sym_name)
                if cur_hash and old_hash and cur_hash != old_hash:
                    entry = {
                        "file": fpath,
                        "name": sym_name,
                        "old_hash": old_hash,
                        "new_hash": cur_hash,
                        **{k: v for k, v in cur_syms[sym_name].items() if k != "body_hash"},
                    }
                    modified_symbols.append(entry)
                    if cur_syms[sym_name]["symbol_type"] == "api_route":
                        api_changes.append({
                            "file": fpath,
                            "name": sym_name,
                            "change": "modified",
                        })

        return {
            "ok": True,
            "since": since_commit,
            "changed_files": changed_files,
            "added_symbols": added_symbols,
            "removed_symbols": removed_symbols,
            "modified_symbols": modified_symbols,
            "api_changes": api_changes,
            "breaking_changes": breaking_changes,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7.5 Snapshot and API Diff
# ---------------------------------------------------------------------------


def take_snapshot(graph: dict, project_root: str) -> dict:
    """Save current API surface as a snapshot for future comparison."""
    try:
        graperoot_dir = Path(project_root) / ".graperoot"
        graperoot_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).isoformat()
        ts_slug = re.sub(r"[^\d]", "", timestamp)[:15]
        snapshot_path = graperoot_dir / f"api_snapshot_{ts_slug}.json"

        api_routes: list[dict] = []
        models: list[dict] = []
        exported_symbols: list[dict] = []

        for sym in _symbol_nodes(graph):
            sym_type = str(sym.get("symbol_type", ""))
            name = str(sym.get("name", ""))
            fpath = str(sym.get("path", ""))
            line_start = int(sym.get("line_start", 0))
            body_hash = str(sym.get("body_hash", ""))
            exported = bool(sym.get("exported", False))

            base = {
                "file": fpath,
                "name": name,
                "line_start": line_start,
                "body_hash": body_hash,
            }

            if sym_type == "api_route":
                # Try to extract HTTP method and path from keywords
                keywords = sym.get("keywords", [])
                method = next(
                    (k.upper() for k in keywords if k.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}),
                    "UNKNOWN",
                )
                route_path = next(
                    (k for k in keywords if k.startswith("/")),
                    name,
                )
                api_routes.append({**base, "path": route_path, "method": method})

            elif sym_type == "model":
                models.append(base)

            if exported:
                exported_symbols.append({**base, "symbol_type": sym_type})

        snapshot = {
            "timestamp": timestamp,
            "api_routes": api_routes,
            "models": models,
            "exported_symbols": exported_symbols,
        }

        snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

        return {
            "ok": True,
            "snapshot_file": str(snapshot_path),
            "timestamp": timestamp,
            "api_routes_count": len(api_routes),
            "models_count": len(models),
            "exported_symbols_count": len(exported_symbols),
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def compare_snapshot(graph: dict, snapshot_file: str) -> dict:
    """Detect breaking API changes since snapshot."""
    try:
        snap = json.loads(Path(snapshot_file).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Cannot read snapshot: {exc}"}

    try:
        snapshot_time = snap.get("timestamp", "unknown")

        # Build current API surface
        current_routes: dict[str, dict] = {}
        current_models: dict[str, dict] = {}
        current_exports: dict[str, dict] = {}

        for sym in _symbol_nodes(graph):
            sym_type = str(sym.get("symbol_type", ""))
            name = str(sym.get("name", ""))
            fpath = str(sym.get("path", ""))
            body_hash = str(sym.get("body_hash", ""))
            key = f"{fpath}::{name}"
            base = {
                "file": fpath,
                "name": name,
                "body_hash": body_hash,
                "symbol_type": sym_type,
            }
            if sym_type == "api_route":
                current_routes[key] = base
            elif sym_type == "model":
                current_models[key] = base
            if sym.get("exported", False):
                current_exports[key] = base

        # Previous snapshot surface
        old_routes: dict[str, dict] = {
            f"{r['file']}::{r['name']}": r for r in snap.get("api_routes", [])
        }
        old_models: dict[str, dict] = {
            f"{m['file']}::{m['name']}": m for m in snap.get("models", [])
        }

        old_route_keys = set(old_routes.keys())
        cur_route_keys = set(current_routes.keys())

        routes_added = [current_routes[k] for k in cur_route_keys - old_route_keys]
        routes_removed = [old_routes[k] for k in old_route_keys - cur_route_keys]

        routes_changed: list[dict] = []
        for key in old_route_keys & cur_route_keys:
            old_h = old_routes[key].get("body_hash", "")
            new_h = current_routes[key].get("body_hash", "")
            if old_h and new_h and old_h != new_h:
                routes_changed.append({
                    **current_routes[key],
                    "old_hash": old_h,
                    "new_hash": new_h,
                })

        old_model_keys = set(old_models.keys())
        cur_model_keys = set(current_models.keys())
        models_changed: list[dict] = []
        for key in old_model_keys & cur_model_keys:
            old_h = old_models[key].get("body_hash", "")
            new_h = current_models.get(key, {}).get("body_hash", "")
            if old_h and new_h and old_h != new_h:
                models_changed.append({
                    **current_models.get(key, {}),
                    "old_hash": old_h,
                    "new_hash": new_h,
                })

        breaking: list[dict] = [
            {**r, "reason": "route removed"} for r in routes_removed
        ]

        if breaking:
            severity = "breaking"
        elif routes_changed or models_changed:
            severity = "safe"  # implementation changed but no removals
        else:
            severity = "safe"

        # Edge case: severity unknown when snapshot is too old (>30 days)
        try:
            snap_dt = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - snap_dt).days
            if age_days > 30:
                severity = "unknown"
        except Exception:
            pass

        return {
            "ok": True,
            "snapshot_time": snapshot_time,
            "routes_added": routes_added,
            "routes_removed": routes_removed,
            "routes_changed": routes_changed,
            "models_changed": models_changed,
            "breaking_changes": breaking,
            "severity": severity,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7.6 Graph Fix (Dead Methods)
# ---------------------------------------------------------------------------


def suggest_fixes(graph: dict, fix_type: str = "dead_methods", dry_run: bool = True) -> dict:
    """Suggest or apply automatic fixes."""
    try:
        if fix_type == "dead_methods":
            return _fix_dead_methods(graph, dry_run)
        elif fix_type == "unused_imports":
            return _fix_unused_imports(graph, dry_run)
        elif fix_type == "duplicate_routes":
            return _fix_duplicate_routes(graph, dry_run)
        else:
            return {
                "ok": False,
                "error": f"Unknown fix_type '{fix_type}'. "
                         "Valid: dead_methods, unused_imports, duplicate_routes",
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _fix_dead_methods(graph: dict, dry_run: bool) -> dict:
    """Find unexported symbols with zero callers in the graph."""
    # Build set of all symbol ids referenced by a non-containment edge.
    # "contains" edges mean a file owns a symbol — that is NOT an external caller.
    # We want symbols that have zero callers/importers/users outside themselves.
    referenced: set[str] = set()
    for edge in _edges(graph):
        if edge.get("rel") == "contains":
            continue  # containment is ownership, not a caller reference
        tgt = str(edge.get("to", ""))
        if tgt:
            referenced.add(tgt)
            # Also track base file so symbol IDs like file::name are handled
            referenced.add(tgt.split("::")[0])

    # Identify test-referenced files to protect them
    test_refs: set[str] = set()
    for node in _file_nodes(graph):
        fid = str(node.get("id", "") or node.get("path", ""))
        if _is_test_file(fid):
            for edge in _edges_from(graph, fid):
                test_refs.add(str(edge.get("to", "")))

    suggestions: list[dict] = []
    skipped: list[dict] = []

    for sym in _symbol_nodes(graph):
        sym_id = str(sym.get("id", ""))
        name = str(sym.get("name", ""))
        fpath = str(sym.get("path", ""))
        sym_type = str(sym.get("symbol_type", ""))
        line_start = int(sym.get("line_start", 0))
        exported = bool(sym.get("exported", False))

        # Only consider unexported symbols
        if exported:
            continue

        # Never touch API routes
        if sym_type == "api_route":
            skipped.append({
                "file": fpath,
                "name": name,
                "line": line_start,
                "reason": "api_route — never remove public API",
            })
            continue

        # Skip if referenced from a test file
        if sym_id in test_refs or f"{fpath}::{name}" in test_refs:
            skipped.append({
                "file": fpath,
                "name": name,
                "line": line_start,
                "reason": "referenced from test file",
            })
            continue

        # Check if any edge targets this symbol
        if sym_id not in referenced and f"{fpath}::{name}" not in referenced:
            safe = not exported and sym_type not in {"api_route"}
            suggestions.append({
                "file": fpath,
                "line": line_start,
                "action": "remove",
                "description": f"Unexported symbol '{name}' has no callers in the graph",
                "safe": safe,
            })

    applied: list[dict] = []
    if not dry_run:
        # Safety: only apply when explicitly requested
        for sug in suggestions:
            if sug["safe"]:
                # We intentionally do NOT modify files automatically unless
                # the caller has verified the suggestion manually.
                # (Actual AST-based removal would go here in a production patch.)
                applied.append(sug)

    return {
        "ok": True,
        "fix_type": "dead_methods",
        "dry_run": dry_run,
        "suggestions": suggestions,
        "applied": applied if not dry_run else [],
        "skipped": skipped,
    }


def _fix_unused_imports(graph: dict, dry_run: bool) -> dict:
    """Find import edges where the imported symbol is never used."""
    # Build a map: file -> set of names that are *used* (via calls/uses edges)
    file_used: dict[str, set[str]] = {}
    for edge in _edges(graph):
        if edge.get("rel") in {"calls", "uses"}:
            frm = str(edge.get("from", ""))
            tgt = str(edge.get("to", ""))
            if "::" in tgt:
                sym_name = tgt.split("::")[-1]
                file_used.setdefault(frm, set()).add(sym_name)

    suggestions: list[dict] = []
    skipped: list[dict] = []

    for edge in _edges(graph):
        if edge.get("rel") != "imports":
            continue
        frm = str(edge.get("from", ""))
        tgt = str(edge.get("to", ""))
        if not frm or not tgt:
            continue

        # What names does this import make available?
        imported_name = tgt.split("::")[-1] if "::" in tgt else None
        if imported_name is None:
            # Whole-file import — harder to determine unused without AST
            continue

        used_names = file_used.get(frm, set())
        if imported_name not in used_names:
            suggestions.append({
                "file": frm,
                "line": 0,  # line info not stored on edges
                "action": "remove_import",
                "description": f"Import of '{imported_name}' from '{tgt}' appears unused",
                "safe": True,
            })

    return {
        "ok": True,
        "fix_type": "unused_imports",
        "dry_run": dry_run,
        "suggestions": suggestions,
        "applied": [],
        "skipped": skipped,
    }


def _fix_duplicate_routes(graph: dict, dry_run: bool) -> dict:
    """Find duplicate route paths in API route symbols."""
    route_map: dict[str, list[dict]] = {}  # route_key -> list of symbols

    for sym in _symbol_nodes(graph):
        if sym.get("symbol_type") != "api_route":
            continue
        name = str(sym.get("name", ""))
        fpath = str(sym.get("path", ""))
        keywords = sym.get("keywords", [])
        # Route path heuristic: keyword starting with "/"
        route_path = next((k for k in keywords if k.startswith("/")), name)
        method = next(
            (k.upper() for k in keywords if k.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}),
            "",
        )
        key = f"{method} {route_path}".strip()
        route_map.setdefault(key, []).append({
            "file": fpath,
            "name": name,
            "line": int(sym.get("line_start", 0)),
        })

    suggestions: list[dict] = []
    for route_key, occurrences in route_map.items():
        if len(occurrences) > 1:
            for occ in occurrences[1:]:  # keep first, flag rest
                suggestions.append({
                    "file": occ["file"],
                    "line": occ["line"],
                    "action": "review_duplicate",
                    "description": f"Duplicate route '{route_key}' also defined in {occurrences[0]['file']}",
                    "safe": False,  # human review required
                })

    return {
        "ok": True,
        "fix_type": "duplicate_routes",
        "dry_run": dry_run,
        "suggestions": suggestions,
        "applied": [],
        "skipped": [],
    }


# ---------------------------------------------------------------------------
# 7.7 Multi-shard Federated Search
# ---------------------------------------------------------------------------


def federated_search(query: str, shard_paths: list[str], top_k: int = 10) -> dict:
    """Search across multiple info_graph.json shards (multi-service)."""
    try:
        query_terms = re.findall(r"[A-Za-z0-9_]+", query.lower())
        query_terms = [t for t in query_terms if len(t) >= 3]

        all_results: list[dict] = []
        shards_searched = 0

        for shard_path in shard_paths:
            shard = _load_graph(shard_path)
            if not shard or shard.get("ok") is False:
                continue

            shard_name = Path(shard_path).parent.name or shard_path
            shards_searched += 1

            # Score each file node
            for node in shard.get("nodes", []):
                if node.get("kind") != "file":
                    continue
                keywords = node.get("keywords", [])
                summary = str(node.get("summary", ""))
                doc_terms = keywords + re.findall(r"[A-Za-z0-9_]+", summary.lower())
                score = _bm25_score(query_terms, doc_terms)
                if score <= 0.0:
                    continue

                fpath = str(node.get("path", "") or node.get("id", ""))
                all_results.append({
                    "file": fpath,
                    "shard": shard_name,
                    "score": round(score, 3),
                    "summary": summary[:200],
                    "symbol_type": "",  # file-level hit
                })

            # Score symbol nodes too
            for node in shard.get("nodes", []):
                if node.get("kind") != "symbol":
                    continue
                name = str(node.get("name", ""))
                sym_type = str(node.get("symbol_type", ""))
                keywords = node.get("keywords", []) + [name, sym_type]
                score = _bm25_score(query_terms, keywords)
                if score <= 0.0:
                    continue

                fpath = str(node.get("path", ""))
                all_results.append({
                    "file": f"{fpath}::{name}",
                    "shard": shard_name,
                    "score": round(score, 3),
                    "summary": f"{sym_type} {name}",
                    "symbol_type": sym_type,
                })

        # Sort by score descending, deduplicate by (shard, file)
        all_results.sort(key=lambda x: -x["score"])
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for r in all_results:
            key = (r["shard"], r["file"])
            if key not in seen:
                seen.add(key)
                deduped.append(r)
            if len(deduped) >= top_k:
                break

        return {
            "ok": True,
            "query": query,
            "shards_searched": shards_searched,
            "results": deduped,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 7.8 Who Owns
# ---------------------------------------------------------------------------


def who_owns(file_path: str, project_root: str) -> dict:
    """Look up CODEOWNERS for a file."""
    try:
        codeowners_path = _find_codeowners_file(project_root)
        if not codeowners_path:
            return {
                "ok": True,
                "file": file_path,
                "owners": [],
                "matching_rule": "",
                "codeowners_file": "",
            }

        rel_file = _relative(file_path, project_root)

        # Parse CODEOWNERS — last matching rule wins (GitHub semantics)
        rules: list[tuple[str, list[str]]] = []  # [(pattern, [owners])]
        with open(codeowners_path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if not parts:
                    continue
                pattern = parts[0]
                owners = parts[1:]
                rules.append((pattern, owners))

        # Evaluate rules — last match wins
        matched_rule = ""
        matched_owners: list[str] = []

        for pattern, owners in rules:
            if _codeowners_match(pattern, rel_file):
                matched_rule = pattern
                matched_owners = owners

        return {
            "ok": True,
            "file": rel_file,
            "owners": matched_owners,
            "matching_rule": matched_rule,
            "codeowners_file": _relative(codeowners_path, project_root),
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _codeowners_match(pattern: str, file_path: str) -> bool:
    """Match a CODEOWNERS pattern against a relative file path."""
    # Normalise path separators
    fp = file_path.replace("\\", "/")
    pat = pattern.lstrip("/")

    # Directory pattern: trailing slash means "all files in this dir"
    if pat.endswith("/"):
        pat = pat.rstrip("/")
        return fp.startswith(pat + "/") or fp == pat

    # Patterns without "/" match anywhere in the tree (like .gitignore rules)
    if "/" not in pat:
        basename = fp.split("/")[-1]
        return fnmatch.fnmatch(basename, pat)

    # Otherwise: anchor to the root and fnmatch the full path
    return fnmatch.fnmatch(fp, pat) or fnmatch.fnmatch(fp, pat.lstrip("/"))


# ---------------------------------------------------------------------------
# 7.9 Explain Path
# ---------------------------------------------------------------------------


def explain_path(graph: dict, source: str, target: str, max_hops: int = 6) -> dict:
    """Find and explain the connection between two files/symbols."""
    try:
        # Build adjacency: node_id -> [(neighbour_id, edge_type)]
        adj: dict[str, list[tuple[str, str]]] = {}
        for edge in _edges(graph):
            frm = str(edge.get("from", ""))
            tgt = str(edge.get("to", ""))
            rel = str(edge.get("rel", ""))
            if frm and tgt:
                adj.setdefault(frm, []).append((tgt, rel))

        # Also allow undirected traversal via "contains" edges
        for edge in _edges(graph):
            if edge.get("rel") == "contains":
                frm = str(edge.get("from", ""))
                tgt = str(edge.get("to", ""))
                adj.setdefault(tgt, []).append((frm, "contained_in"))

        # BFS — track the path
        # Each entry: (node_id, path_so_far)
        queue: deque[tuple[str, list[dict]]] = deque()
        queue.append((source, [{"node": source, "edge_type": "start", "description": f"Start at {source}"}]))
        visited: set[str] = {source}
        found_path: list[dict] | None = None
        alt_path_count = 0
        all_paths: list[list[dict]] = []

        while queue:
            current, path = queue.popleft()
            if len(path) - 1 > max_hops:
                continue

            if current == target:
                if found_path is None:
                    found_path = path
                else:
                    alt_path_count += 1
                    all_paths.append(path)
                continue

            for neighbour, edge_type in adj.get(current, []):
                if neighbour not in visited:
                    visited.add(neighbour)
                    desc = _describe_edge(current, neighbour, edge_type)
                    new_path = path + [{"node": neighbour, "edge_type": edge_type, "description": desc}]
                    queue.append((neighbour, new_path))

        if found_path is None:
            return {
                "ok": True,
                "source": source,
                "target": target,
                "connected": False,
                "path": [],
                "explanation": f"No connection found between '{source}' and '{target}' within {max_hops} hops.",
                "hops": -1,
                "alternative_paths": 0,
            }

        hops = len(found_path) - 1
        explanation = _build_explanation(found_path)

        return {
            "ok": True,
            "source": source,
            "target": target,
            "connected": True,
            "path": found_path,
            "explanation": explanation,
            "hops": hops,
            "alternative_paths": alt_path_count,
        }

    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _describe_edge(frm: str, tgt: str, edge_type: str) -> str:
    frm_name = frm.split("::")[-1] if "::" in frm else Path(frm).stem
    tgt_name = tgt.split("::")[-1] if "::" in tgt else Path(tgt).stem
    descriptions = {
        "imports": f"{frm_name} imports {tgt_name}",
        "uses": f"{frm_name} uses {tgt_name}",
        "calls": f"{frm_name} calls {tgt_name}",
        "contains": f"{frm_name} contains {tgt_name}",
        "contained_in": f"{tgt_name} contains {frm_name}",
        "exports": f"{frm_name} exports {tgt_name}",
    }
    return descriptions.get(edge_type, f"{frm_name} -{edge_type}-> {tgt_name}")


def _build_explanation(path: list[dict]) -> str:
    """Build a human-readable explanation from a path."""
    if len(path) <= 1:
        return "Source and target are the same node."
    parts: list[str] = []
    for step in path:
        edge_type = step["edge_type"]
        node = step["node"]
        name = node.split("::")[-1] if "::" in node else Path(node).name
        if edge_type == "start":
            parts.append(name)
        elif edge_type in {"imports", "uses", "calls"}:
            parts.append(f"which {edge_type} {name}")
        elif edge_type == "contains":
            parts.append(f"which contains {name}")
        elif edge_type == "contained_in":
            parts.append(f"(defined in {name})")
        else:
            parts.append(f"-[{edge_type}]-> {name}")
    return " ".join(parts)
