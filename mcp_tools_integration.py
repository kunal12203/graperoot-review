from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ══════════════════════════════════════════════════════════════════════════════
# TOOL GRAPH — navigable edges between tools
# Agents receive these with every response so they ALWAYS know what to try next.
# This replaces the "flat list of 67 tools" problem.
# ══════════════════════════════════════════════════════════════════════════════

_TOOL_GRAPH: dict[str, dict] = {
    # ── Fallback edges: "if this tool returned 0 findings, try THESE next" ──
    "fallback": {
        "graph_connection_pool_misconfigs": {
            "try_next": ["graph_config_misconfigs"],
            "reason": "Pool config may be in PgBouncer .ini or Spring YAML, not in code",
        },
        "graph_scan_secrets": {
            "try_next": ["graph_log_secret_leakage"],
            "reason": "Secrets may not be hardcoded — they may be interpolated into log calls",
        },
        "graph_health_checks": {
            "try_next": ["graph_config_misconfigs"],
            "reason": "Health endpoint exists but may return static 200 without checking DB",
        },
        "graph_idempotency_gaps": {
            "try_next": ["graph_config_misconfigs"],
            "reason": "enable.auto.commit=true in kafka.properties causes same symptom (data loss on crash)",
        },
        "graph_race_conditions": {
            "try_next": ["graph_goroutine_leaks"],
            "reason": "Race-like symptoms may actually be goroutine/context leaks (cancel never called)",
        },
        "graph_http_timeouts": {
            "try_next": ["graph_cache_stampede", "graph_circuit_breakers"],
            "reason": "Timeout may be hit due to thundering herd on cold cache, or missing circuit breaker",
        },
        "graph_resource_leaks": {
            "try_next": ["graph_goroutine_leaks"],
            "reason": "Resource exhaustion in Go often means goroutine leak, not just unclosed file handle",
        },
        "graph_missing_indexes": {
            "try_next": ["graph_n_plus_one", "graph_cache_stampede"],
            "reason": "Slow queries may be N+1 pattern or cache miss amplification, not just missing index",
        },
    },
    # ── Co-occur edges: "if you called X, also call Y" ──────────────────────
    "co_occur": {
        "graph_connection_pool_misconfigs": ["graph_goroutine_leaks", "graph_config_misconfigs"],
        "graph_cache_stampede": ["graph_missing_pagination", "graph_n_plus_one"],
        "graph_http_timeouts": ["graph_circuit_breakers", "graph_retry_backoff"],
        "graph_unsafe_migrations": ["graph_missing_indexes"],
        "graph_log_secret_leakage": ["graph_scan_secrets"],
        "graph_goroutine_leaks": ["graph_resource_leaks", "graph_http_timeouts"],
        "graph_config_misconfigs": ["graph_connection_pool_misconfigs", "graph_idempotency_gaps"],
    },
    # ── Symptom router: natural language → tool(s) ───────────────────────────
    "symptoms": {
        "OOM|out of memory|memory leak|SIGKILL|oom-killed": [
            "graph_goroutine_leaks", "graph_resource_leaks",
        ],
        "connection refused|too many connections|pool exhausted|connection timeout|pgbouncer|hikari|connection exhaustion": [
            "graph_connection_pool_misconfigs", "graph_config_misconfigs", "graph_health_checks",
        ],
        "messages lost|data loss|events dropped|kafka|consumer crash": [
            "graph_config_misconfigs", "graph_idempotency_gaps",
        ],
        "slow|latency|p99|timeout|hanging": [
            "graph_http_timeouts", "graph_cache_stampede", "graph_n_plus_one", "graph_missing_indexes",
        ],
        "secret|api key|token|credential|password.*log": [
            "graph_log_secret_leakage", "graph_scan_secrets",
        ],
        "health check|readiness|liveness|pod restart|CrashLoopBackOff": [
            "graph_health_checks", "graph_goroutine_leaks", "graph_resource_leaks",
        ],
        "thundering herd|cache miss|cache cold|stampede|redis restart|cache restart|db overload": [
            "graph_cache_stampede",
        ],
        "migration|ALTER TABLE|deploy broke|lock|DDL": [
            "graph_unsafe_migrations", "graph_config_misconfigs",
        ],
        "cascading|circuit breaker|all services down|one service slow": [
            "graph_http_timeouts", "graph_circuit_breakers", "graph_cache_stampede",
        ],
        "rebalance|consumer group|kafka poll|session timeout": [
            "graph_config_misconfigs",
        ],
        "goroutine|context cancel|defer|waitgroup": [
            "graph_goroutine_leaks",
        ],
        "race condition|concurrent|mutex|shared state": [
            "graph_race_conditions", "graph_goroutine_leaks",
        ],
    },
}


def _get_navigation_hints(tool_name: str, findings_count: int) -> dict[str, Any]:
    """Return navigation hints for a tool response — agents use this to decide what to call next."""
    hints: dict[str, Any] = {}

    if findings_count == 0:
        # Fallback: "this returned empty, try these"
        fallback = _TOOL_GRAPH["fallback"].get(tool_name)
        if fallback:
            hints["try_next"] = fallback["try_next"]
            hints["reason"] = fallback["reason"]
        else:
            hints["try_next"] = ["graph_production_readiness"]
            hints["reason"] = "No findings — run the master audit for a full picture"
    else:
        # Co-occur: "you found stuff, also check these related tools"
        co = _TOOL_GRAPH["co_occur"].get(tool_name, [])
        if co:
            hints["also_check"] = co
            hints["reason"] = "Related checks that often fire together"

    return hints


def _route_symptom(description: str) -> list[str]:
    """Given a natural-language symptom description, return the best tool(s) to call."""
    import re
    description_lower = description.lower()
    matches: list[tuple[int, list[str]]] = []
    for pattern, tools in _TOOL_GRAPH["symptoms"].items():
        keywords = pattern.split("|")
        score = sum(1 for kw in keywords if kw.lower() in description_lower)
        if score > 0:
            matches.append((score, tools))
    matches.sort(key=lambda x: -x[0])
    if matches:
        return matches[0][1]
    return ["graph_production_readiness"]


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

        _EXT_ORM_MAP = {
            ".rb":     "activerecord",
            ".java":   "jpa",
            ".kt":     "jpa",
            ".prisma": "prisma",
        }

        # Keywords in node metadata -> orm_type
        _KEYWORD_ORM_MAP = [
            ("typeorm",      "typeorm"),
            ("sequelize",    "sequelize"),
            ("mongoose",     "mongoose"),
            ("gorm",         "gorm"),
            ("drizzle",      "drizzle"),
            ("sqlalchemy",   "sqlalchemy"),
            ("prisma",       "prisma"),
            ("django",       "django"),
            ("jpa",          "jpa"),
            ("activerecord", "activerecord"),
        ]

        # Content fingerprints for orm_type detection
        import re as _re
        _CONTENT_ORM_SIGS = [
            (r"@Entity|@Column|@ManyToOne|@OneToMany|@JoinColumn|@Table\b",   "typeorm"),
            (r"DataTypes\.|Model\.init\s*\(|sequelize\.define",               "sequelize"),
            (r"new\s+Schema\s*\(|mongoose\.model\s*\(",                       "mongoose"),
            (r"db\.Model\b|gorm\.Model\b",                                    "gorm"),
            (r"pgTable\s*\(|mysqlTable\s*\(|drizzle\s*\(",                    "drizzle"),
            (r"Column\s*\(|ForeignKey\s*\(|declarative_base|Base\s*=\s*declarative", "sqlalchemy"),
            (r"models\.Model\b|from\s+django",                                "django"),
            (r"@javax\.persistence\.|@jakarta\.persistence\.",                "jpa"),
            (r"belongs_to\s+:|has_many\s+:|ActiveRecord::Base",               "activerecord"),
        ]

        def _detect_orm_type(file_path: str, node: dict) -> str:
            """Detect ORM type from file extension, node keywords, or file content."""
            suffix = Path(file_path).suffix.lower()
            if suffix in _EXT_ORM_MAP:
                return _EXT_ORM_MAP[suffix]

            # Check node-level orm_type field first
            node_orm = str(node.get("orm_type", "")).lower()
            for kw, otype in _KEYWORD_ORM_MAP:
                if kw in node_orm:
                    return otype

            # Check node keywords list
            for kw_item in node.get("keywords", []):
                for kw, otype in _KEYWORD_ORM_MAP:
                    if kw in str(kw_item).lower():
                        return otype

            # Python files: check content
            if suffix == ".py":
                try:
                    content = Path(file_path).read_text(encoding="utf-8", errors="replace")
                    for pattern, otype in _CONTENT_ORM_SIGS:
                        if _re.search(pattern, content):
                            return otype
                    # Fallback: "models" in filename -> Django
                    if "models" in Path(file_path).name:
                        return "django"
                except Exception:
                    pass
                return "sqlalchemy"

            # TS/JS files: check content
            if suffix in {".ts", ".tsx", ".js", ".jsx"}:
                try:
                    content = Path(file_path).read_text(encoding="utf-8", errors="replace")
                    for pattern, otype in _CONTENT_ORM_SIGS:
                        if _re.search(pattern, content):
                            return otype
                except Exception:
                    pass

            return "unknown"

        # ------------------------------------------------------------------
        # Step 1: collect models from info_graph.json
        # ------------------------------------------------------------------
        # model_name -> model dict  (for dedup)
        models_by_name: dict[str, dict] = {}

        for node in nodes:
            if node.get("symbol_type") != "model":
                continue
            file_path = node.get("file", "")
            name      = node.get("name", "")
            if not name:
                continue
            orm_type = _detect_orm_type(file_path, node)
            entry = {
                "name":       name,
                "file":       file_path,
                "orm_type":   orm_type,
                "line_start": node.get("line_start"),
                "fields":     [],
            }
            models_by_name.setdefault(name, entry)

        # ------------------------------------------------------------------
        # Step 2: supplement with graph_builder_orm.py if available
        # ------------------------------------------------------------------
        try:
            import importlib.util as _ilu
            import os as _os

            _orm_module_path = Path(get_project_root()) / "graph_builder_orm.py"
            if not _orm_module_path.exists():
                # Also check the directory containing this file
                _orm_module_path = Path(__file__).parent / "graph_builder_orm.py"

            if _orm_module_path.exists():
                _spec = _ilu.spec_from_file_location("graph_builder_orm", str(_orm_module_path))
                _orm_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
                _spec.loader.exec_module(_orm_mod)  # type: ignore[union-attr]

                _extract_fn   = getattr(_orm_mod, "extract_orm_symbols", None)
                _supports_fn  = getattr(_orm_mod, "supports_orm", None)

                if _extract_fn and _supports_fn:
                    _SKIP_DIRS = {
                        ".git", "node_modules", "__pycache__", ".venv", "venv",
                        "dist", "build", "out", "target", ".next", ".nuxt",
                    }
                    _SOURCE_EXTS = {
                        ".py", ".ts", ".tsx", ".js", ".jsx",
                        ".rb", ".java", ".kt", ".go", ".prisma",
                    }
                    _proj = str(get_project_root())
                    for dirpath, dirnames, filenames in _os.walk(_proj):
                        dirnames[:] = [
                            d for d in dirnames
                            if d not in _SKIP_DIRS and not d.startswith(".")
                        ]
                        for fname in filenames:
                            fp = Path(dirpath) / fname
                            if fp.suffix.lower() not in _SOURCE_EXTS:
                                continue
                            try:
                                _content = fp.read_text(encoding="utf-8", errors="ignore")
                                _ext = fp.suffix.lower()
                                if not _supports_fn(_content, str(fp), _ext):
                                    continue
                                orm_symbols = _extract_fn(_content, str(fp), _ext)
                                for sym in (orm_symbols or []):
                                    sym_name = sym.get("name", "")
                                    if not sym_name or sym_name in models_by_name:
                                        continue
                                    rel_fp = str(fp.relative_to(_proj))
                                    node_stub: dict = {
                                        "orm_type": sym.get("orm_type", ""),
                                        "keywords": sym.get("keywords", []),
                                    }
                                    models_by_name[sym_name] = {
                                        "name":       sym_name,
                                        "file":       rel_fp,
                                        "orm_type":   _detect_orm_type(str(fp), node_stub),
                                        "line_start": sym.get("line_start"),
                                        "fields":     [],
                                    }
                            except Exception:
                                continue
        except ImportError:
            pass

        models = list(models_by_name.values())
        return {"ok": True, "total": len(models), "models": models}

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

        result = {
            "ok": True,
            "total": len(filtered),
            "findings": filtered[:50],  # cap output to 50 for readability
            "by_severity": by_severity,
        }
        result["_next"] = _get_navigation_hints("graph_scan_secrets", len(filtered))
        return result

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

    @mcp.tool()
    def graph_iac_security(severity_min: str = "low") -> dict[str, Any]:
        """Scan Terraform, Kubernetes YAML, and Dockerfiles for security misconfigurations."""
        try:
            from security import scan_iac_misconfigs as _scan_iac_misconfigs  # type: ignore
            all_findings = _scan_iac_misconfigs(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        min_level = _SEVERITY_ORDER.get(severity_min.lower(), 3)
        filtered = [
            f for f in all_findings
            if _SEVERITY_ORDER.get(f.get("severity", "low").lower(), 3) <= min_level
        ]

        by_rule: dict[str, int] = {}
        for f in filtered:
            rid = f.get("rule_id", "unknown")
            by_rule[rid] = by_rule.get(rid, 0) + 1

        return {
            "ok": True,
            "total": len(filtered),
            "findings": filtered,
            "by_rule": by_rule,
        }

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

    @mcp.tool()
    def graph_sentry_coverage() -> dict[str, Any]:
        """Show Sentry error tracking instrumentation across the codebase."""
        try:
            from observability import extract_sentry_instrumentation as _extract_sentry  # type: ignore
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}

        project_root = str(get_project_root())
        source_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".kt", ".java"}
        total_files_scanned = 0
        files_with_sentry: list[str] = []
        sentry_calls: list[dict] = []

        try:
            import os as _os
            for dirpath, dirnames, filenames in _os.walk(project_root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                                 "dist", "build", "target", ".tox"}
                ]
                for fname in filenames:
                    _, ext = _os.path.splitext(fname.lower())
                    if ext not in source_exts:
                        continue
                    fpath = _os.path.join(dirpath, fname)
                    total_files_scanned += 1
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                            content = fh.read()
                    except Exception:
                        continue
                    syms = _extract_sentry(content, fpath, ext)
                    if syms:
                        files_with_sentry.append(fpath)
                        for s in syms:
                            sentry_calls.append({
                                "file": fpath,
                                "line": s["line_start"],
                                "kind": s["name"],
                                "symbol_type": s["symbol_type"],
                            })
        except Exception as e:
            return {"ok": False, "error": str(e)}

        coverage_pct = (
            round(len(files_with_sentry) / total_files_scanned * 100, 1)
            if total_files_scanned > 0 else 0.0
        )
        return {
            "ok": True,
            "total_files_scanned": total_files_scanned,
            "files_with_sentry": len(files_with_sentry),
            "sentry_calls": sentry_calls,
            "coverage_pct": coverage_pct,
        }

    @mcp.tool()
    def graph_datadog_coverage() -> dict[str, Any]:
        """Show Datadog APM tracing instrumentation across the codebase."""
        try:
            from observability import extract_datadog_instrumentation as _extract_dd  # type: ignore
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}

        project_root = str(get_project_root())
        source_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".kt", ".java"}
        total_files_scanned = 0
        files_with_datadog: list[str] = []
        datadog_spans: list[dict] = []

        try:
            import os as _os
            for dirpath, dirnames, filenames in _os.walk(project_root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in {"node_modules", ".git", "__pycache__", ".venv", "venv",
                                 "dist", "build", "target", ".tox"}
                ]
                for fname in filenames:
                    _, ext = _os.path.splitext(fname.lower())
                    if ext not in source_exts:
                        continue
                    fpath = _os.path.join(dirpath, fname)
                    total_files_scanned += 1
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                            content = fh.read()
                    except Exception:
                        continue
                    syms = _extract_dd(content, fpath, ext)
                    if syms:
                        files_with_datadog.append(fpath)
                        for s in syms:
                            operation = s["name"]
                            # Surface any operation name from keywords
                            kws = [k for k in s.get("keywords", [])
                                   if k not in ("datadog", "dd", s["name"].split(":")[-1])]
                            datadog_spans.append({
                                "file": fpath,
                                "line": s["line_start"],
                                "operation": kws[0] if kws else operation,
                                "kind": operation,
                                "symbol_type": s["symbol_type"],
                            })
        except Exception as e:
            return {"ok": False, "error": str(e)}

        coverage_pct = (
            round(len(files_with_datadog) / total_files_scanned * 100, 1)
            if total_files_scanned > 0 else 0.0
        )
        return {
            "ok": True,
            "total_files_scanned": total_files_scanned,
            "files_with_datadog": len(files_with_datadog),
            "datadog_spans": datadog_spans,
            "coverage_pct": coverage_pct,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 9 — Database Health Tools
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool()
    def graph_missing_pagination() -> dict[str, Any]:
        """Find database query calls without pagination limits (N+1 / full-table-scan risk)."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass

        try:
            from structural_bugs import find_missing_pagination as _find_missing_pagination  # type: ignore
            findings = _find_missing_pagination(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        by_pattern: dict[str, int] = {}
        for f in findings:
            p = f.get("pattern", "unknown")
            by_pattern[p] = by_pattern.get(p, 0) + 1

        return {
            "ok": True,
            "total": len(findings),
            "findings": findings,
            "by_pattern": by_pattern,
        }

    @mcp.tool()
    def graph_missing_indexes() -> dict[str, Any]:
        """Detect foreign key fields that likely lack a database index."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass

        try:
            from structural_bugs import find_missing_indexes as _find_missing_indexes  # type: ignore
            findings = _find_missing_indexes(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        by_file: dict[str, int] = {}
        for f in findings:
            fp = f.get("file", "unknown")
            by_file[fp] = by_file.get(fp, 0) + 1

        result = {
            "ok": True,
            "total": len(findings),
            "findings": findings,
            "by_file": by_file,
        }
        result["_next"] = _get_navigation_hints("graph_missing_indexes", len(findings))
        return result

    @mcp.tool()
    def graph_n_plus_one() -> dict[str, Any]:
        """Detect patterns that strongly suggest N+1 query risk (DB query inside a loop)."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass

        try:
            from structural_bugs import find_n_plus_one_risk as _find_n_plus_one_risk  # type: ignore
            findings = _find_n_plus_one_risk(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        by_file: dict[str, int] = {}
        for f in findings:
            fp = f.get("file", "unknown")
            by_file[fp] = by_file.get(fp, 0) + 1

        return {
            "ok": True,
            "total": len(findings),
            "findings": findings,
            "by_file": by_file,
        }

    # ── IaC Extended tools ────────────────────────────────────────────────────

    @mcp.tool()
    def graph_cdk_stacks() -> dict[str, Any]:
        """List all AWS CDK stacks (v1 and v2) found in the project."""
        try:
            from graph_builder_iac_extended import get_cdk_stacks as _get_cdk_stacks  # type: ignore
            return _get_cdk_stacks(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "graph_builder_iac_extended.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_cfn_resources(resource_type: str = "") -> dict[str, Any]:
        """List CloudFormation resources, optionally filtered by type (e.g. 'AWS::S3::Bucket')."""
        try:
            from graph_builder_iac_extended import get_cfn_resources as _get_cfn_resources  # type: ignore
            result = _get_cfn_resources(str(get_project_root()))
            if resource_type:
                result["resources"] = [r for r in result.get("resources", [])
                                        if resource_type.lower() in r.get("resource_type", "").lower()]
                result["total"] = len(result["resources"])
            return result
        except ImportError:
            return {"ok": False, "error": "graph_builder_iac_extended.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_pulumi_resources() -> dict[str, Any]:
        """List Pulumi resources declared in the project."""
        try:
            from graph_builder_iac_extended import get_pulumi_resources as _get_pulumi_resources  # type: ignore
            return _get_pulumi_resources(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "graph_builder_iac_extended.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_iac_extended_summary() -> dict[str, Any]:
        """Full IaC summary: CDK stacks, CFN resources, Pulumi resources, Bicep modules."""
        try:
            from graph_builder_iac_extended import get_iac_extended_summary as _get_iac_summary  # type: ignore
            return _get_iac_summary(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "graph_builder_iac_extended.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── CI Extended tools ─────────────────────────────────────────────────────

    @mcp.tool()
    def graph_ci_extended_summary() -> dict[str, Any]:
        """CI/CD summary: Travis, Drone, Bitbucket, ArgoCD, Tekton, Flux, TeamCity."""
        try:
            from graph_builder_ci_extended import get_ci_extended_summary as _get_ci_ext_summary  # type: ignore
            return _get_ci_ext_summary(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "graph_builder_ci_extended.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Lang Extended tools ───────────────────────────────────────────────────

    @mcp.tool()
    def graph_lang_extended_summary() -> dict[str, Any]:
        """Language summary: Elixir/Phoenix, Swift/Vapor, Dart/Flutter, Groovy/Gradle."""
        try:
            from graph_builder_lang_extended import get_lang_extended_summary as _get_lang_summary  # type: ignore
            return _get_lang_summary(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "graph_builder_lang_extended.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Observability Extended tools ──────────────────────────────────────────

    @mcp.tool()
    def graph_apm_coverage() -> dict[str, Any]:
        """APM/tracing coverage: New Relic, Dynatrace, Honeycomb, Jaeger, Zipkin."""
        try:
            from observability import get_apm_coverage as _get_apm_coverage  # type: ignore
            return _get_apm_coverage(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool()
    def graph_newrelic_coverage() -> dict[str, Any]:
        """New Relic instrumentation coverage across the project."""
        try:
            from observability import get_newrelic_coverage as _get_nr_coverage  # type: ignore
            return _get_nr_coverage(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "observability.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Structural checks: env vars, ports, races ─────────────────────────────

    @mcp.tool()
    def graph_unused_env_vars() -> dict[str, Any]:
        """Find environment variables declared in .env files but never used in source."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_unused_env_vars as _find_unused_env_vars  # type: ignore
            findings = _find_unused_env_vars(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "total": len(findings), "findings": findings}

    @mcp.tool()
    def graph_missing_env_vars() -> dict[str, Any]:
        """Find environment variables used in code but absent from all .env files."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_missing_env_vars as _find_missing_env_vars  # type: ignore
            findings = _find_missing_env_vars(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "total": len(findings), "findings": findings}

    @mcp.tool()
    def graph_port_conflicts() -> dict[str, Any]:
        """Detect the same port bound in two or more different files (excludes 80/443)."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_port_conflicts as _find_port_conflicts  # type: ignore
            findings = _find_port_conflicts(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "total": len(findings), "findings": findings}

    @mcp.tool()
    def graph_race_conditions() -> dict[str, Any]:
        """Heuristic race condition detection for Go, Python threads, and JS Promise.all."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_race_conditions as _find_race_conditions  # type: ignore
            findings = _find_race_conditions(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_lang: dict[str, int] = {}
        for f in findings:
            lang = f.get("language", "unknown")
            by_lang[lang] = by_lang.get(lang, 0) + 1
        result = {"ok": True, "total": len(findings), "by_language": by_lang, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_race_conditions", len(findings))
        return result

    # ── Resilience checks ─────────────────────────────────────────────────────

    @mcp.tool()
    def graph_http_timeouts() -> dict[str, Any]:
        """Find HTTP calls (Go/Python/JS) missing explicit timeout configuration."""
        try:
            from resilience_checks import find_http_calls_without_timeout as _check  # type: ignore
            findings = _check({}, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "resilience_checks.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_file: dict[str, int] = {}
        for f in findings:
            by_file[f.get("file", "?")] = by_file.get(f.get("file", "?"), 0) + 1
        result = {"ok": True, "total": len(findings), "by_file": by_file, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_http_timeouts", len(findings))
        return result

    @mcp.tool()
    def graph_retry_backoff() -> dict[str, Any]:
        """Find retry loops using fixed sleep instead of exponential backoff."""
        try:
            from resilience_checks import find_retries_without_backoff as _check  # type: ignore
            findings = _check({}, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "resilience_checks.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "total": len(findings), "findings": findings}

    @mcp.tool()
    def graph_feature_flag_fallbacks() -> dict[str, Any]:
        """Find LaunchDarkly/Unleash/Statsig feature flag calls without fallback defaults."""
        try:
            from resilience_checks import find_feature_flags_without_fallback as _check  # type: ignore
            findings = _check({}, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "resilience_checks.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "total": len(findings), "findings": findings}

    @mcp.tool()
    def graph_circuit_breakers() -> dict[str, Any]:
        """Find files making 3+ external HTTP calls without a circuit breaker library."""
        try:
            from resilience_checks import find_missing_circuit_breakers as _check  # type: ignore
            findings = _check({}, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "resilience_checks.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "total": len(findings), "findings": findings}

    @mcp.tool()
    def graph_resilience_summary() -> dict[str, Any]:
        """Full resilience report: timeouts, backoff, feature flags, circuit breakers with score."""
        try:
            from resilience_checks import get_resilience_summary as _get_summary  # type: ignore
            return _get_summary(str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "resilience_checks.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Idempotency, resource leaks, migration safety ─────────────────────────

    @mcp.tool()
    def graph_idempotency_gaps() -> dict[str, Any]:
        """Find MQ consumers and batch jobs with non-idempotent side effects (no dedup/checkpoint)."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_idempotency_gaps as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_severity: dict[str, int] = {}
        for f in findings:
            s = f.get("severity", "?")
            by_severity[s] = by_severity.get(s, 0) + 1
        result = {"ok": True, "total": len(findings), "by_severity": by_severity, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_idempotency_gaps", len(findings))
        return result

    @mcp.tool()
    def graph_resource_leaks() -> dict[str, Any]:
        """Find unclosed file handles, HTTP response bodies, and DB connections."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_resource_leaks as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {"ok": True, "total": len(findings), "by_rule": by_rule, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_resource_leaks", len(findings))
        return result

    @mcp.tool()
    def graph_connection_pool_misconfigs() -> dict[str, Any]:
        """Detect database connection pools misconfigured for production (no max, unlimited size)."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_connection_pool_misconfigs as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {"ok": True, "total": len(findings), "by_rule": by_rule, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_connection_pool_misconfigs", len(findings))
        return result

    @mcp.tool()
    def graph_health_checks() -> dict[str, Any]:
        """Find missing health endpoints and Kubernetes Deployments without readinessProbe."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_missing_health_checks as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        result = {"ok": True, "total": len(findings), "findings": findings}
        result["_next"] = _get_navigation_hints("graph_health_checks", len(findings))
        return result

    @mcp.tool()
    def graph_unsafe_migrations() -> dict[str, Any]:
        """Find ALTER TABLE without online DDL markers, missing lock timeouts, DROP in app code."""
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_unsafe_migrations as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {"ok": True, "total": len(findings), "by_rule": by_rule, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_unsafe_migrations", len(findings))
        return result

    @mcp.tool()
    def graph_goroutine_leaks() -> dict[str, Any]:
        """Detect Go goroutine/context leaks: cancel() not deferred, goroutines
        in loops without WaitGroup, and channels never closed.

        Rule IDs: GLEAK-001 (cancel not deferred), GLEAK-002 (goroutine in loop),
        GLEAK-003 (channel never closed).

        Grounded in real on-call incidents:
          - Go services OOM'd every 6-8h from accumulating goroutines
          - DynamoDB outage: latent race between two goroutines on shared DNS state
          - go.dev/blog/context: defer cancel() is the canonical fix
        """
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_goroutine_leaks as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {"ok": True, "total": len(findings), "by_rule": by_rule, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_goroutine_leaks", len(findings))
        return result

    @mcp.tool()
    def graph_cache_stampede() -> dict[str, Any]:
        """Detect thundering-herd / cache stampede risks: cache GET without
        singleflight/mutex protection, TTL=0, and SET with no expiry.

        Rule IDs: STMP-001 (no stampede guard), STMP-002 (TTL≤0),
        STMP-003 (set without TTL).

        Grounded in real on-call incidents:
          - Instagram 2012: thundering herd after cache flush
          - Reddit K8s: cold-start pod storm saturated DB
          - Every Redis restart: all pods miss simultaneously → DB overload
        """
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_cache_stampede_risks as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {"ok": True, "total": len(findings), "by_rule": by_rule, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_cache_stampede", len(findings))
        return result

    @mcp.tool()
    def graph_config_misconfigs() -> dict[str, Any]:
        """Parse PgBouncer .ini, Kafka .properties, and Spring HikariCP YAML for
        dangerous defaults that cause connection exhaustion or data loss.

        Rule IDs: CFG-001 (PgBouncer pool_mode=session),
        CFG-002 (max_client_conn >> pool_size), CFG-003 (Kafka auto.commit=true),
        CFG-004 (max.poll.records too high), CFG-005 (HikariCP default pool).

        Grounded in real on-call incidents:
          - CircleCI: connection queue saturated at peak Wednesday-afternoon load
          - Kafka consumers: auto-commit before processing = silent message loss
          - HikariCP default pool of 10 too small for Spring MVC apps
        """
        graph_json = get_dg_data_dir() / "info_graph.json"
        graph: dict = {}
        if graph_json.exists():
            try:
                graph = json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            from structural_bugs import find_config_file_misconfigs as _check  # type: ignore
            findings = _check(graph, str(get_project_root()))
        except ImportError:
            return {"ok": False, "error": "structural_bugs.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {"ok": True, "total": len(findings), "by_rule": by_rule, "findings": findings}
        result["_next"] = _get_navigation_hints("graph_config_misconfigs", len(findings))
        return result

    @mcp.tool()
    def graph_log_secret_leakage() -> dict[str, Any]:
        """Detect secret variable values interpolated into log statements across
        Python, JS/TS, Go, and Java/Kotlin.

        Rule IDs: LOG-001 (Python f-string/%), LOG-002 (JS template literal),
        LOG-003 (Go format verb + secret arg), LOG-004 (Java/Kotlin log call).

        Grounded in real on-call incidents:
          - Cloudflare parser bug 2017: auth tokens, cookies, POST bodies leaked
            from memory into HTTP responses, some cached by search engines
          - Common pattern: logging.info(f'Calling Stripe key={api_key}') ships
            secrets to Datadog/Splunk where they persist for months
        """
        root = str(get_project_root())
        try:
            from security import scan_log_secret_leakage as _scan  # type: ignore
            findings = _scan(root)
        except ImportError:
            return {"ok": False, "error": "security.py not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        by_rule: dict[str, int] = {}
        for f in findings:
            r = f.get("rule_id", "?")
            by_rule[r] = by_rule.get(r, 0) + 1
        result = {
            "ok": True,
            "total": len(findings),
            "by_rule": by_rule,
            "findings": findings,
            "note": (
                "CWE-532: Insertion of Sensitive Information into Log File. "
                "Rotate any exposed keys immediately."
            ),
        }
        result["_next"] = _get_navigation_hints("graph_log_secret_leakage", len(findings))
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # DISCOVERY + COMPOSITE TOOLS
    # These are the entry points — call these first, not individual tools.
    # ══════════════════════════════════════════════════════════════════════════

    # Tool catalogue — structured so AI can navigate by intent
    _TOOL_CATALOGUE: dict[str, dict] = {
        "routes": {
            "description": "Find, list, and trace HTTP routes and API endpoints",
            "tools": {
                "graph_find_route": "Find which handler serves GET /payments or POST /users",
                "graph_list_routes": "List all routes, optionally filtered by prefix or method",
                "graph_trace_event": "Follow a Kafka/SQS/Redis event through the whole system",
                "graph_who_publishes": "Who publishes to a given topic or queue?",
            },
        },
        "models": {
            "description": "Database models, ORM relationships, and schema analysis",
            "tools": {
                "graph_db_models": "List all ORM models/entities and their relationships",
                "graph_tf_resources": "List Terraform/IaC resources by type",
                "graph_cdk_stacks": "List AWS CDK stacks (v1 and v2)",
                "graph_cfn_resources": "List CloudFormation resources, filter by type",
                "graph_pulumi_resources": "List Pulumi resources",
            },
        },
        "security": {
            "description": "Security audit: secrets, SAST, vulnerabilities, licenses, IaC misconfigs",
            "tools": {
                "graph_scan_secrets": "Scan for hardcoded secrets and credentials",
                "graph_sast_findings": "Static analysis security findings by language",
                "graph_log_secret_leakage": "Secret vars (tokens/keys) interpolated into log calls — CWE-532",
                "graph_scan_vulnerabilities": "OSV vulnerability scan of dependencies",
                "graph_license_audit": "License compliance audit",
                "graph_iac_security": "IaC misconfiguration scan (Terraform, K8s, CDK)",
                "graph_idempotency_gaps": "MQ consumers/batch jobs missing idempotency guards",
                "graph_unsafe_migrations": "DB migrations missing online DDL or lock timeouts",
            },
            "composite": "graph_security_audit",
        },
        "resilience": {
            "description": "Production resilience: timeouts, retries, circuit breakers, feature flags",
            "tools": {
                "graph_http_timeouts": "HTTP calls missing explicit timeout configuration",
                "graph_retry_backoff": "Retry loops using fixed sleep instead of exponential backoff",
                "graph_feature_flag_fallbacks": "Feature flag calls without fallback defaults",
                "graph_circuit_breakers": "Files with 3+ external calls and no circuit breaker",
                "graph_resilience_summary": "Full resilience score across all 4 checks",
            },
            "composite": "graph_resilience_audit",
        },
        "ops": {
            "description": "Operational health: connection pools, health endpoints, port conflicts, race conditions",
            "tools": {
                "graph_connection_pool_misconfigs": "DB pools without max size or hardcoded connection strings",
                "graph_config_misconfigs": "PgBouncer/Kafka/HikariCP config file dangerous defaults",
                "graph_health_checks": "Missing /health endpoints and K8s readinessProbe",
                "graph_goroutine_leaks": "Go context.WithCancel cancel not deferred, goroutines in loops",
                "graph_cache_stampede": "Cache GET without singleflight/mutex, TTL=0, SET without expiry",
                "graph_port_conflicts": "Same port bound in multiple files",
                "graph_race_conditions": "Heuristic race condition detection (Go/Python/JS)",
                "graph_unused_env_vars": "Env vars declared in .env but never used",
                "graph_missing_env_vars": "Env vars used in code but missing from .env",
            },
            "composite": "graph_ops_audit",
        },
        "code_quality": {
            "description": "Code quality: dead code, N+1 queries, missing pagination, resource leaks",
            "tools": {
                "graph_n_plus_one": "N+1 query risk patterns (DB query inside loop)",
                "graph_missing_pagination": "Query endpoints without pagination",
                "graph_missing_indexes": "Foreign keys and filter fields without DB indexes",
                "graph_resource_leaks": "Unclosed file handles, HTTP bodies, DB connections",
                "graph_debt_score": "Technical debt score by file",
                "graph_dead_exports": "Exported symbols never imported anywhere (use graph_system_health)",
            },
            "composite": "graph_code_quality_audit",
        },
        "observability": {
            "description": "Observability coverage: tracing, metrics, logging, alerting",
            "tools": {
                "graph_observability_summary": "Full observability coverage score",
                "graph_otel_topology": "OpenTelemetry trace topology",
                "graph_prometheus_alerts": "Prometheus alert rules",
                "graph_sentry_coverage": "Sentry error tracking coverage",
                "graph_datadog_coverage": "Datadog APM and metrics coverage",
                "graph_apm_coverage": "APM coverage: New Relic, Dynatrace, Honeycomb, Jaeger, Zipkin",
                "graph_newrelic_coverage": "New Relic instrumentation coverage",
            },
        },
        "infra": {
            "description": "Infrastructure: CI/CD, IaC, Kubernetes, kustomize overlays",
            "tools": {
                "graph_ci_topology": "CI/CD pipeline topology (all 12 systems)",
                "graph_ci_extended_summary": "Extended CI/CD: Travis, Drone, Bitbucket, ArgoCD, Tekton, Flux, TeamCity",
                "graph_iac_extended_summary": "Full IaC summary: CDK, CFN, Pulumi, Bicep",
                "graph_kustomize_overlays": "Kustomize overlay structure",
                "graph_lang_extended_summary": "Language breakdown: Elixir, Swift, Dart, Groovy",
            },
        },
        "impact": {
            "description": "Change impact, ownership, and cross-service analysis",
            "tools": {
                "graph_pr_impact": "What breaks if these files change? (provide list of changed files)",
                "graph_who_owns": "CODEOWNERS-aware file ownership",
                "graph_explain_path": "Plain-English explanation of how two components connect",
                "graph_test_coverage": "Test coverage by scope",
                "graph_diff": "What changed across all services since a given commit",
                "graph_version_audit": "Dependency version conflicts",
            },
        },
    }

    @mcp.tool()
    @mcp.tool()
    def graph_route(symptom: str = "") -> dict[str, Any]:
        """Describe your problem in natural language and get the exact tools to call.

        This is a SYMPTOM ROUTER — pass what you're seeing ("OOM every 6h",
        "messages dropped", "API key in Datadog logs", "cache restart killed DB")
        and it returns the 1-3 tools to call, in order, plus what to try if the
        first returns empty.

        Without arguments: returns the full tool graph (categories + edges).
        With a symptom: returns prioritized tools matching that symptom.
        """
        if symptom:
            tools = _route_symptom(symptom)
            result: dict[str, Any] = {
                "ok": True,
                "symptom": symptom,
                "call_these": tools,
                "if_empty": {},
            }
            for t in tools:
                fb = _TOOL_GRAPH["fallback"].get(t)
                if fb:
                    result["if_empty"][t] = fb
            co_related: list[str] = []
            for t in tools:
                co_related.extend(_TOOL_GRAPH["co_occur"].get(t, []))
            if co_related:
                result["also_check"] = list(set(co_related) - set(tools))
            return result

        return {
            "ok": True,
            "tip": "Pass a symptom description to get routed to the right tools. Or use graph_production_readiness() for a full audit.",
            "categories": {
                name: {
                    "description": data["description"],
                    "tool_count": len(data["tools"]),
                    "composite": data.get("composite"),
                }
                for name, data in _TOOL_CATALOGUE.items()
            },
            "composite_tools": {
                "graph_production_readiness": "Run ALL audits → top-10 action punch list",
                "graph_security_audit": "Run full security audit → prioritized findings",
                "graph_resilience_audit": "Run full resilience audit → prioritized findings",
                "graph_ops_audit": "Run full ops health audit → prioritized findings",
                "graph_code_quality_audit": "Run full code quality audit → prioritized findings",
            },
            "symptom_examples": [
                "OOM every 6h",
                "messages silently dropped",
                "API key visible in logs",
                "connection refused at peak",
                "cache restart killed DB",
                "ALTER TABLE locked prod",
            ],
        }

    @mcp.tool()
    def graph_help(category: str = "") -> dict[str, Any]:
        """List tools in a category. Call graph_route(symptom) instead if you
        have a specific problem to solve.

        Categories: security, resilience, ops, code_quality, observability, infra, impact, routes, models.
        """
        if not category:
            return {
                "ok": True,
                "tip": "Use graph_route('your symptom') for intent-based routing. Or pass a category name here.",
                "categories": {
                    name: {
                        "description": data["description"],
                        "tool_count": len(data["tools"]),
                        "composite": data.get("composite"),
                    }
                    for name, data in _TOOL_CATALOGUE.items()
                },
            }
        cat = _TOOL_CATALOGUE.get(category.lower())
        if not cat:
            available = ", ".join(_TOOL_CATALOGUE.keys())
            return {"ok": False, "error": f"Unknown category '{category}'. Available: {available}"}
        return {
            "ok": True,
            "category": category,
            "description": cat["description"],
            "composite": cat.get("composite"),
            "tools": cat["tools"],
        }

    def _make_punch_list(all_findings: list[dict], top_n: int = 10) -> list[dict]:
        """Sort findings by severity and return the top N as an actionable list."""
        _SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_findings = sorted(
            all_findings,
            key=lambda f: (_SEV_RANK.get(f.get("severity", "low"), 4), f.get("file", ""))
        )
        punch = []
        for i, f in enumerate(sorted_findings[:top_n], 1):
            punch.append({
                "rank": i,
                "severity": f.get("severity", "?"),
                "rule_id": f.get("rule_id", f.get("check", "?")),
                "file": f.get("file", "?"),
                "line": f.get("line", 0),
                "action": f.get("message", f.get("description", "See finding details")),
            })
        return punch

    def _load_graph(get_dg_data_dir: Any) -> dict:
        graph_json = get_dg_data_dir() / "info_graph.json"
        if graph_json.exists():
            try:
                return json.loads(graph_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    @mcp.tool()
    def graph_security_audit() -> dict[str, Any]:
        """
        Run the full security audit in one call. Covers: secrets, SAST, OSV
        vulnerabilities, license compliance, IaC misconfigs, and idempotency
        gaps. Returns a prioritized top-10 punch list.
        """
        graph = _load_graph(get_dg_data_dir)
        root = str(get_project_root())
        all_findings: list[dict] = []

        def _run(fn_import: str, module: str, *args: Any) -> list[dict]:
            try:
                mod = __import__(module)
                fn = getattr(mod, fn_import)
                return fn(*args) or []
            except Exception:
                return []

        # Secrets
        try:
            from security import scan_secrets as _scan_secrets  # type: ignore
            results = _scan_secrets(root, severity_min="medium")
            all_findings += [dict(f, rule_id=f.get("rule", "SEC"), severity=f.get("severity", "high")) for f in results]
        except Exception:
            pass

        # SAST
        try:
            from security import run_sast as _run_sast  # type: ignore
            results = _run_sast(root)
            all_findings += [dict(f, rule_id=f.get("rule", "SAST"), severity=f.get("severity", "medium")) for f in results]
        except Exception:
            pass

        # IaC security
        try:
            from security import scan_iac_misconfigs as _scan_iac  # type: ignore
            results = _scan_iac(root)
            all_findings += [dict(f, rule_id=f.get("rule", "IAC"), severity=f.get("severity", "medium")) for f in results]
        except Exception:
            pass

        # Idempotency gaps
        try:
            from structural_bugs import find_idempotency_gaps as _idem  # type: ignore
            all_findings += _idem(graph, root)
        except Exception:
            pass

        # Unsafe migrations
        try:
            from structural_bugs import find_unsafe_migrations as _migr  # type: ignore
            all_findings += _migr(graph, root)
        except Exception:
            pass

        # Log secret leakage (CWE-532)
        try:
            from security import scan_log_secret_leakage as _log_secrets  # type: ignore
            results = _log_secrets(root)
            all_findings += [dict(f, rule_id=f.get("rule_id", "LOG"), severity=f.get("severity", "high")) for f in results]
        except Exception:
            pass

        punch = _make_punch_list(all_findings)
        return {
            "ok": True,
            "total_findings": len(all_findings),
            "categories_run": ["secrets", "sast", "log_leakage", "iac_misconfigs", "idempotency", "migrations"],
            "top10_action_list": punch,
            "all_findings": all_findings,
        }

    @mcp.tool()
    def graph_resilience_audit() -> dict[str, Any]:
        """
        Run the full resilience audit in one call. Covers: HTTP calls without
        timeout, retry loops without exponential backoff, feature flag calls
        without fallback, and files missing circuit breakers. Returns a
        prioritized top-10 punch list with a resilience score.
        """
        root = str(get_project_root())
        all_findings: list[dict] = []
        scores: dict[str, int] = {}

        try:
            from resilience_checks import get_resilience_summary as _res  # type: ignore
            summary = _res(root)
            all_findings = summary.get("findings", [])
            scores["resilience_score"] = summary.get("resilience_score", 100)
        except Exception:
            pass

        punch = _make_punch_list(all_findings)
        return {
            "ok": True,
            "total_findings": len(all_findings),
            "resilience_score": scores.get("resilience_score", 100),
            "categories_run": ["http_timeouts", "retry_backoff", "feature_flag_fallbacks", "circuit_breakers"],
            "top10_action_list": punch,
            "all_findings": all_findings,
        }

    @mcp.tool()
    def graph_ops_audit() -> dict[str, Any]:
        """
        Run the full operational health audit in one call. Covers: connection
        pool misconfigs, missing health endpoints, port conflicts, race
        conditions, unused/missing env vars. Returns a prioritized top-10
        punch list.
        """
        graph = _load_graph(get_dg_data_dir)
        root = str(get_project_root())
        all_findings: list[dict] = []

        checks = [
            ("find_connection_pool_misconfigs", "structural_bugs"),
            ("find_config_file_misconfigs", "structural_bugs"),
            ("find_missing_health_checks", "structural_bugs"),
            ("find_goroutine_leaks", "structural_bugs"),
            ("find_cache_stampede_risks", "structural_bugs"),
            ("find_port_conflicts", "structural_bugs"),
            ("find_race_conditions", "structural_bugs"),
            ("find_unused_env_vars", "structural_bugs"),
            ("find_missing_env_vars", "structural_bugs"),
        ]
        for fn_name, mod_name in checks:
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                fn = getattr(mod, fn_name)
                all_findings += fn(graph, root) or []
            except Exception:
                pass

        punch = _make_punch_list(all_findings)
        return {
            "ok": True,
            "total_findings": len(all_findings),
            "categories_run": [
                "pool_misconfigs", "config_files", "health_checks",
                "goroutine_leaks", "cache_stampede",
                "port_conflicts", "race_conditions", "env_vars",
            ],
            "top10_action_list": punch,
            "all_findings": all_findings,
        }

    @mcp.tool()
    def graph_code_quality_audit() -> dict[str, Any]:
        """
        Run the full code quality audit in one call. Covers: N+1 query risk,
        missing pagination, missing indexes, resource leaks (unclosed handles),
        and structural bugs. Returns a prioritized top-10 punch list.
        """
        graph = _load_graph(get_dg_data_dir)
        root = str(get_project_root())
        all_findings: list[dict] = []

        checks = [
            ("find_n_plus_one_risk", "structural_bugs"),
            ("find_missing_pagination", "structural_bugs"),
            ("find_missing_indexes", "structural_bugs"),
            ("find_resource_leaks", "structural_bugs"),
            ("find_bean_collisions", "structural_bugs"),
            ("find_missing_config_siblings", "structural_bugs"),
        ]
        for fn_name, mod_name in checks:
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                fn = getattr(mod, fn_name)
                all_findings += fn(graph, root) or []
            except Exception:
                pass

        punch = _make_punch_list(all_findings)
        return {
            "ok": True,
            "total_findings": len(all_findings),
            "categories_run": ["n_plus_one", "pagination", "indexes", "resource_leaks", "structural"],
            "top10_action_list": punch,
            "all_findings": all_findings,
        }

    @mcp.tool()
    def graph_production_readiness() -> dict[str, Any]:
        """
        THE master audit tool. Runs security + resilience + ops + code quality
        in one call and returns a single prioritized top-10 punch list of the
        most critical issues to fix before going to production.

        Use this when you want a full picture without knowing which specific
        tool to call. Each item in the punch list tells you exactly what to fix
        and where.
        """
        graph = _load_graph(get_dg_data_dir)
        root = str(get_project_root())
        all_findings: list[dict] = []
        categories_run: list[str] = []
        scores: dict[str, Any] = {}

        import importlib

        # Security
        for fn_name, mod_name, cat in [
            ("find_idempotency_gaps", "structural_bugs", "idempotency"),
            ("find_unsafe_migrations", "structural_bugs", "migrations"),
            ("find_resource_leaks", "structural_bugs", "resource_leaks"),
        ]:
            try:
                fn = getattr(importlib.import_module(mod_name), fn_name)
                findings = fn(graph, root) or []
                all_findings += findings
                categories_run.append(cat)
            except Exception:
                pass

        try:
            from security import scan_secrets as _ss  # type: ignore
            results = _ss(root, severity_min="high")
            all_findings += [dict(f, rule_id="SEC-SECRET", severity="critical") for f in (results or [])]
            categories_run.append("secrets")
        except Exception:
            pass

        # Resilience
        try:
            from resilience_checks import get_resilience_summary as _res  # type: ignore
            summary = _res(root)
            all_findings += summary.get("findings", [])
            scores["resilience_score"] = summary.get("resilience_score", 100)
            categories_run.append("resilience")
        except Exception:
            pass

        # Ops
        for fn_name, cat in [
            ("find_connection_pool_misconfigs", "pool_misconfigs"),
            ("find_missing_health_checks", "health_checks"),
            ("find_port_conflicts", "port_conflicts"),
            ("find_race_conditions", "race_conditions"),
        ]:
            try:
                fn = getattr(importlib.import_module("structural_bugs"), fn_name)
                findings = fn(graph, root) or []
                all_findings += findings
                categories_run.append(cat)
            except Exception:
                pass

        # Code quality
        for fn_name, cat in [
            ("find_n_plus_one_risk", "n_plus_one"),
            ("find_missing_pagination", "pagination"),
            ("find_missing_indexes", "indexes"),
        ]:
            try:
                fn = getattr(importlib.import_module("structural_bugs"), fn_name)
                findings = fn(graph, root) or []
                all_findings += findings
                categories_run.append(cat)
            except Exception:
                pass

        # Health summary score
        try:
            from structural_bugs import get_health_summary as _hs  # type: ignore
            hs = _hs(graph, root)
            scores["health_score"] = hs.get("health_score", 100)
        except Exception:
            pass

        punch = _make_punch_list(all_findings, top_n=10)

        # Severity breakdown
        sev_counts: dict[str, int] = {}
        for f in all_findings:
            s = f.get("severity", "low")
            sev_counts[s] = sev_counts.get(s, 0) + 1

        return {
            "ok": True,
            "total_findings": len(all_findings),
            "severity_breakdown": sev_counts,
            "scores": scores,
            "categories_run": categories_run,
            "top10_action_list": punch,
            "tip": "Fix critical/high items first. Use graph_help('category') to deep-dive any area.",
        }


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
