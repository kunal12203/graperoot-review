from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def register_all_new_tools(
    mcp,
    get_dg_data_dir,        # callable() -> Path — returns DG_DATA_DIR
    get_project_root,       # callable() -> Path — returns PROJECT_ROOT
    get_turn_state,         # callable() -> dict — returns TURN_STATE proxy
) -> None:
    """Register all Phase 2-8 MCP tools onto the mcp instance."""

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 2 — Route Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_find_route(method: str = "", path: str = "") -> dict[str, Any]:
        """Find which files/handlers handle a specific HTTP route."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        nodes = graph.get("nodes", [])
        routes = []
        method_upper = method.upper() if method else ""
        for node in nodes:
            if node.get("symbol_type") != "api_route":
                continue
            name = node.get("name", "")
            # route_method / route_path set by graph_builder_routes.py;
            # fall back to parsing from "METHOD /path" name format
            route_method = node.get("route_method", "").upper()
            route_path = node.get("route_path", "")
            if not route_method and " " in name:
                parts = name.split(" ", 1)
                route_method = parts[0].upper()
                route_path = parts[1] if len(parts) > 1 else ""

            path_match = (not path) or (path in route_path) or (path in name)
            method_match = (not method_upper) or (method_upper == route_method) or (method_upper in name)

            combined = f"{method_upper} {path}".strip()
            name_match = combined and combined in name

            if (path_match and method_match) or name_match:
                routes.append(
                    {
                        "file": node.get("path", node.get("file", "")),
                        "name": name,
                        "line_start": node.get("line_start"),
                        "handler": node.get("handler", name),
                        "method": route_method,
                        "path": route_path,
                    }
                )

        return {"ok": True, "routes": routes}

    @mcp.tool()
    def graph_list_routes(prefix: str = "", method: str = "") -> dict[str, Any]:
        """List all detected routes in the project."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        nodes = graph.get("nodes", [])
        method_upper = method.upper() if method else ""
        by_file: dict[str, list[dict]] = {}

        for node in nodes:
            if node.get("symbol_type") != "api_route":
                continue
            name = node.get("name", "")
            route_method = node.get("route_method", "").upper()
            route_path = node.get("route_path", "")
            # Fall back to parsing from "METHOD /path" name format
            if not route_method and " " in name:
                parts = name.split(" ", 1)
                route_method = parts[0].upper()
                route_path = parts[1] if len(parts) > 1 else ""

            if prefix and not route_path.startswith(prefix):
                continue
            if method_upper and route_method and route_method != method_upper:
                continue

            file_key = node.get("path", node.get("file", ""))
            entry = {
                "file": file_key,
                "name": name,
                "method": route_method,
                "path": route_path,
                "line_start": node.get("line_start"),
            }
            by_file.setdefault(file_key, []).append(entry)

        all_routes = [r for routes in by_file.values() for r in routes]
        return {"ok": True, "total": len(all_routes), "routes": all_routes}

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 3 — Service Graph Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_trace_event(topic: str) -> dict[str, Any]:
        """Follow an event/message queue topic across services."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        edges = graph.get("edges", [])
        publishers = []
        subscribers = []
        broker = None

        for edge in edges:
            rel = edge.get("rel", "")
            edge_topic = edge.get("topic", "") or edge.get("target", "")
            if topic and topic not in edge_topic:
                continue
            if rel == "publishes_to":
                publishers.append(
                    {
                        "file": edge.get("source_file", edge.get("from", "")),
                        "line": edge.get("line"),
                    }
                )
                if not broker:
                    broker = edge.get("broker")
            elif rel == "subscribes_from":
                subscribers.append(
                    {
                        "file": edge.get("source_file", edge.get("from", "")),
                        "line": edge.get("line"),
                    }
                )
                if not broker:
                    broker = edge.get("broker")

        return {
            "ok": True,
            "topic": topic,
            "publishers": publishers,
            "subscribers": subscribers,
            "broker": broker,
        }

    @mcp.tool()
    def graph_who_publishes(topic: str = "") -> dict[str, Any]:
        """Find all message publishers in the project."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        edges = graph.get("edges", [])
        publishers = []

        for edge in edges:
            if edge.get("rel") != "publishes_to":
                continue
            edge_topic = edge.get("topic", "") or edge.get("target", "")
            if topic and topic not in edge_topic:
                continue
            publishers.append(
                {
                    "file": edge.get("source_file", edge.get("from", "")),
                    "topic": edge_topic,
                    "broker": edge.get("broker", "unknown"),
                    "line": edge.get("line"),
                }
            )

        # Group by broker type
        by_broker: dict[str, list] = {}
        for p in publishers:
            by_broker.setdefault(p["broker"], []).append(p)

        return {"ok": True, "publishers": publishers, "by_broker": by_broker}

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 4 — Infrastructure Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_tf_resources(resource_type: str = "") -> dict[str, Any]:
        """List all Terraform resources."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        nodes = graph.get("nodes", [])
        resources = []
        modules = []

        for node in nodes:
            name = node.get("name", "")
            if name.startswith("resource:"):
                rtype = node.get("resource_type", name.split(":")[1] if ":" in name else "")
                if resource_type and resource_type not in rtype:
                    continue
                resources.append(
                    {
                        "name": name,
                        "type": rtype,
                        "file": node.get("file", ""),
                        "line": node.get("line_start"),
                    }
                )
            elif name.startswith("module:"):
                rtype = node.get("resource_type", "module")
                if resource_type and resource_type not in rtype:
                    continue
                modules.append(
                    {
                        "name": name,
                        "type": rtype,
                        "file": node.get("file", ""),
                        "line": node.get("line_start"),
                    }
                )

        return {"ok": True, "resources": resources, "modules": modules}

    @mcp.tool()
    def graph_db_models() -> dict[str, Any]:
        """List all ORM/database model definitions."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        nodes = graph.get("nodes", [])
        models = []

        _orm_map = {
            ".rb": "ActiveRecord",
            ".java": "JPA",
            ".kt": "JPA",
            ".prisma": "Prisma",
        }

        for node in nodes:
            if node.get("symbol_type") != "model":
                continue
            file_path = node.get("file", "")
            suffix = Path(file_path).suffix

            if suffix in _orm_map:
                orm_type = _orm_map[suffix]
            elif suffix == ".py":
                fname = Path(file_path).name
                if "models" in fname:
                    orm_type = "Django"
                else:
                    orm_type = node.get("orm_type", "SQLAlchemy")
            else:
                orm_type = node.get("orm_type", "unknown")

            models.append(
                {
                    "name": node.get("name", ""),
                    "file": file_path,
                    "orm_type": orm_type,
                    "line": node.get("line_start"),
                }
            )

        return {"ok": True, "models": models, "total": len(models)}

    @mcp.tool()
    def graph_kustomize_overlays() -> dict[str, Any]:
        """Show kustomize overlay structure."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        edges = graph.get("edges", [])
        base_paths: list[str] = []
        overlays_map: dict[str, dict] = {}

        for edge in edges:
            src = edge.get("from", edge.get("source", ""))
            tgt = edge.get("to", edge.get("target", ""))
            rel = edge.get("rel", "")

            if "kustomization" not in src.lower() and "kustomization" not in tgt.lower():
                continue

            if rel in ("bases", "extends", "references"):
                overlay_name = Path(src).parent.name
                if overlay_name not in overlays_map:
                    overlays_map[overlay_name] = {"name": overlay_name, "base": tgt, "patches": []}
                else:
                    overlays_map[overlay_name]["base"] = tgt
                if tgt not in base_paths:
                    base_paths.append(tgt)
            elif rel in ("patches", "patch"):
                overlay_name = Path(src).parent.name
                if overlay_name not in overlays_map:
                    overlays_map[overlay_name] = {"name": overlay_name, "base": "", "patches": [tgt]}
                else:
                    overlays_map[overlay_name]["patches"].append(tgt)

        return {
            "ok": True,
            "base_paths": base_paths,
            "overlays": list(overlays_map.values()),
        }

    @mcp.tool()
    def graph_ci_topology() -> dict[str, Any]:
        """Show CI/CD pipeline structure."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        nodes = graph.get("nodes", [])
        pipelines_map: dict[str, dict] = {}

        _ci_patterns = [
            (".github/workflows", "github-actions"),
            (".gitlab-ci.yml", "gitlab-ci"),
            ("Jenkinsfile", "jenkins"),
            (".circleci", "circleci"),
            ("azure-pipelines.yml", "azure-devops"),
        ]

        for node in nodes:
            file_path = node.get("file", "")
            ci_type = None
            for pattern, label in _ci_patterns:
                if pattern in file_path:
                    ci_type = label
                    break
            if not ci_type:
                continue

            pipeline = pipelines_map.setdefault(
                file_path,
                {"file": file_path, "type": ci_type, "jobs": []},
            )
            if node.get("symbol_type") in ("job", "stage", "step"):
                pipeline["jobs"].append(
                    {
                        "name": node.get("name", ""),
                        "needs": node.get("needs", []),
                        "uses": node.get("uses", ""),
                    }
                )

        return {"ok": True, "pipelines": list(pipelines_map.values())}

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 5 — Security Tools
    # ──────────────────────────────────────────────────────────────────────────

    _SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

    @mcp.tool()
    def graph_scan_secrets(severity_min: str = "medium") -> dict[str, Any]:
        """Run secret detection on the project."""
        try:
            from security import scan_secrets as _scan_secrets  # type: ignore
            result = _scan_secrets(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # scan_secrets returns list[dict] directly
        all_findings = result if isinstance(result, list) else result.get("findings", [])
        min_level = _SEVERITY_ORDER.get(severity_min.lower(), 2)
        filtered = [
            f for f in all_findings
            if _SEVERITY_ORDER.get(f.get("severity", "info").lower(), 4) <= min_level
        ]

        by_severity: dict[str, int] = {}
        for f in filtered:
            sev = f.get("severity", "unknown")
            by_severity[sev] = by_severity.get(sev, 0) + 1

        return {
            "ok": True,
            "total": len(filtered),
            "findings": filtered[:50],  # cap output to 50 for readability
            "by_severity": by_severity,
        }

    @mcp.tool()
    def graph_sast_findings(language: str = "auto") -> dict[str, Any]:
        """Run SAST analysis."""
        try:
            from security import scan_sast as _scan_sast  # type: ignore
            result = _scan_sast(str(get_project_root()), language=language)
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # scan_sast returns list[dict] directly
        findings = result if isinstance(result, list) else result.get("findings", [])
        by_rule: dict[str, list] = {}
        by_severity: dict[str, list] = {}
        for f in findings:
            by_rule.setdefault(f.get("rule", "unknown"), []).append(f)
            by_severity.setdefault(f.get("severity", "unknown"), []).append(f)

        return {
            "ok": True,
            "total": len(findings),
            "findings": findings,
            "by_rule": {k: len(v) for k, v in by_rule.items()},
            "by_severity": {k: len(v) for k, v in by_severity.items()},
        }

    @mcp.tool()
    def graph_scan_vulnerabilities() -> dict[str, Any]:
        """Check dependencies for known CVEs via OSV.dev."""
        try:
            from security import scan_vulnerabilities as _scan_vulnerabilities  # type: ignore
            return _scan_vulnerabilities(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_license_audit() -> dict[str, Any]:
        """Audit dependency licenses."""
        try:
            from security import scan_licenses as _scan_licenses  # type: ignore
            result = _scan_licenses(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        copyleft = result.get("copyleft", [])
        permissive = result.get("permissive", [])
        unknown = result.get("unknown", [])
        by_license: dict[str, list] = {}
        for item in copyleft + permissive + unknown:
            by_license.setdefault(item.get("license", "unknown"), []).append(item)

        return {
            "ok": True,
            "copyleft": copyleft,
            "permissive": permissive,
            "unknown": unknown,
            "by_license": by_license,
        }

    @mcp.tool()
    def graph_debt_score(file_prefix: str = "") -> dict[str, Any]:
        """Compute technical debt score."""
        try:
            from security import compute_debt_score as _compute_debt_score  # type: ignore
            result = _compute_debt_score(str(get_project_root()), file_prefix=file_prefix)
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {
            "ok": True,
            "score": result.get("score"),
            "grade": result.get("grade"),
            "breakdown": result.get("breakdown", {}),
            "recommendations": result.get("recommendations", []),
        }

    @mcp.tool()
    def graph_sbom(format: str = "summary") -> dict[str, Any]:
        """Generate Software Bill of Materials."""
        try:
            from security import generate_sbom as _generate_sbom  # type: ignore
            return _generate_sbom(str(get_project_root()), format=format)
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 6 — Structural Health Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_system_health() -> dict[str, Any]:
        """Run all structural bug checks and return health report."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from structural_bugs import get_health_summary as _get_health_summary  # type: ignore
            result = _get_health_summary(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {
            "ok": True,
            "score": result.get("score"),
            "grade": result.get("grade"),
            "issues": result.get("issues", []),
            "summary": result.get("summary", ""),
        }

    @mcp.tool()
    def graph_find_bean_collisions() -> dict[str, Any]:
        """Find duplicate Spring bean / service name collisions."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from structural_bugs import find_bean_collisions as _find_bean_collisions  # type: ignore
            collisions = _find_bean_collisions(graph)
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {"ok": True, "collisions": collisions}

    @mcp.tool()
    def graph_check_config_parity() -> dict[str, Any]:
        """Check for missing config key siblings (Kafka serializer pairs, etc)."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from structural_bugs import find_missing_config_siblings as _find_missing_config_siblings  # type: ignore
            return _find_missing_config_siblings(graph)
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 7 — Analysis Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_test_coverage(scope: str = "") -> dict[str, Any]:
        """Static analysis of test coverage for exported symbols."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from advanced_tools import compute_test_coverage as _compute_test_coverage  # type: ignore
            return _compute_test_coverage(graph, scope=scope)
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_pr_impact(changed_files: list[str], max_hops: int = 3) -> dict[str, Any]:
        """Analyze blast radius of changed files."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from advanced_tools import compute_pr_impact as _compute_pr_impact  # type: ignore
            return _compute_pr_impact(graph, changed_files=changed_files, max_hops=max_hops)
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_version_audit() -> dict[str, Any]:
        """Detect version conflicts across package manifests."""
        try:
            from advanced_tools import version_audit as _version_audit  # type: ignore
            return _version_audit(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_diff(since: str = "HEAD~1") -> dict[str, Any]:
        """What changed since a git ref."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from advanced_tools import compute_graph_diff as _compute_graph_diff  # type: ignore
            return _compute_graph_diff(graph, str(get_project_root()), since=since)
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_snapshot() -> dict[str, Any]:
        """Save current API surface as snapshot."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        snapshots_dir = get_dg_data_dir() / "snapshots"
        try:
            snapshots_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"Cannot create snapshots dir: {e}"}

        try:
            from advanced_tools import take_snapshot as _take_snapshot  # type: ignore
            result = _take_snapshot(graph, str(snapshots_dir))
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {
            "ok": True,
            "snapshot_file": result.get("snapshot_file", ""),
            "routes_captured": result.get("routes_captured", 0),
            "models_captured": result.get("models_captured", 0),
        }

    @mcp.tool()
    def graph_api_diff(snapshot_file: str) -> dict[str, Any]:
        """Compare current API against a snapshot."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        snapshot_path = Path(snapshot_file)
        if not snapshot_path.is_absolute():
            snapshot_path = get_dg_data_dir() / "snapshots" / snapshot_file
        if not snapshot_path.exists():
            return {"ok": False, "error": f"Snapshot not found: {snapshot_path}"}

        try:
            from advanced_tools import compare_snapshot as _compare_snapshot  # type: ignore
            return _compare_snapshot(graph, str(snapshot_path))
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_fix(fix_type: str = "dead_methods", dry_run: bool = True) -> dict[str, Any]:
        """Suggest or apply automatic code fixes."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # Safety guard: always default dry_run=True
        if not dry_run:
            # Honour the caller's explicit opt-in but surface a warning
            pass

        try:
            from advanced_tools import suggest_fixes as _suggest_fixes  # type: ignore
            return _suggest_fixes(
                graph,
                str(get_project_root()),
                fix_type=fix_type,
                dry_run=dry_run,
            )
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_who_owns(file_path: str) -> dict[str, Any]:
        """Look up CODEOWNERS for a file."""
        try:
            from advanced_tools import who_owns as _who_owns  # type: ignore
            return _who_owns(str(get_project_root()), file_path=file_path)
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_explain_path(source: str, target: str) -> dict[str, Any]:
        """Explain connection between two files/symbols."""
        if not get_turn_state().get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first.",
                "action_required": "graph_continue",
            }
        graph_json = get_dg_data_dir() / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            from advanced_tools import explain_path as _explain_path  # type: ignore
            return _explain_path(graph, source=source, target=target)
        except ImportError:
            return {"ok": False, "error": "advanced_tools.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 8 — Observability Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_otel_topology() -> dict[str, Any]:
        """Parse OpenTelemetry collector config and show pipeline topology."""
        try:
            from observability import find_otel_configs as _find_otel_configs  # type: ignore
            from observability import parse_otel_config as _parse_otel_config  # type: ignore
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}

        try:
            config_paths = _find_otel_configs(str(get_project_root()))
            merged: dict[str, Any] = {
                "configs": config_paths,
                "receivers": [],
                "exporters": [],
                "pipelines": [],
            }
            for cfg_path in config_paths:
                parsed = _parse_otel_config(cfg_path)
                merged["receivers"] = _merge_unique(merged["receivers"], parsed.get("receivers", []))
                merged["exporters"] = _merge_unique(merged["exporters"], parsed.get("exporters", []))
                merged["pipelines"].extend(parsed.get("pipelines", []))
            merged["ok"] = True
            return merged
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_prometheus_alerts(severity: str = "") -> dict[str, Any]:
        """List all Prometheus alert rules."""
        try:
            from observability import find_prometheus_configs as _find_prometheus_configs  # type: ignore
            from observability import parse_prometheus_alerts as _parse_prometheus_alerts  # type: ignore
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}

        try:
            config_paths = _find_prometheus_configs(str(get_project_root()))
            all_alerts: list[dict] = []
            for cfg_path in config_paths:
                alerts = _parse_prometheus_alerts(cfg_path)
                all_alerts.extend(alerts)

            if severity:
                all_alerts = [
                    a for a in all_alerts
                    if a.get("severity", "").lower() == severity.lower()
                ]

            by_severity: dict[str, list] = {}
            for alert in all_alerts:
                by_severity.setdefault(alert.get("severity", "unknown"), []).append(alert)

            return {
                "ok": True,
                "total_alerts": len(all_alerts),
                "alerts": [
                    {
                        "name": a.get("name", ""),
                        "severity": a.get("severity", ""),
                        "expr": a.get("expr", ""),
                        "for": a.get("for", ""),
                    }
                    for a in all_alerts
                ],
                "by_severity": by_severity,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_observability_summary() -> dict[str, Any]:
        """Overall observability coverage."""
        try:
            from observability import get_observability_summary as _get_observability_summary  # type: ignore
            return _get_observability_summary(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (module-level, not tools)
# ──────────────────────────────────────────────────────────────────────────────

def _merge_unique(base: list, additions: list) -> list:
    """Merge two lists, deduplicating by string value or by 'name' key."""
    seen: set = set()
    result = list(base)
    for item in additions:
        key = item if isinstance(item, str) else item.get("name", str(item))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
