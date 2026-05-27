# GrapeRoot Enterprise — Session Context

## Current Task
Building GrapeRoot Enterprise: full project intelligence layer targeting enterprise teams.
Adds Graphify feature parity + 8 new intelligence features on top of GrapeRoot Pro.

## What Is Enterprise
GrapeRoot Enterprise = GrapeRoot Pro (precision MCP context engine) +
- Multi-modal ingestion (PDFs, DOCX, XLSX, images, video, YouTube)
- Community detection (Leiden clustering → feature clusters in graph_continue)
- Interactive graph viz (graph.html + GRAPH_REPORT.md, auto-generated)
- Live file watcher (watchdog, debounced, no manual graph_scan)
- Test coverage map (untested symbols flagged inline)
- Git hotspot analysis (churn, bus factor, last-touched)
- API surface map (HTTP/GraphQL routes as graph nodes)
- External API detection (Stripe, OpenAI, etc. catalogued per file)
- Environment/config graph (.env, Docker, k8s, CI/CD)
- TODO/FIXME tracker (tech debt linked to graph nodes)
- Dependency vulnerability scan (npm audit / pip-audit / cargo audit)
- PR intelligence (CI state, conflict risk, AI-ranked queue via GitHub API)

## Key Decisions
- Enterprise lives in `graperoot-enterprise/` subfolder of the Pro repo
- New files: `graph_multimodal.py`, `graph_watcher.py`, `graph_viz.py`, `graph_intelligence.py`
- New server version: `mcp_graph_server_v7.6.py` (6 new MCP tools)
- New builder version: `graph_builder_v6.3.py` (multi-modal + Leiden + intelligence hooks)
- First-scan interactive setup: 4 prompts before scanning a new project

## Next Steps
- Implement Phase 1: `graph_watcher.py` + `graph_watch` MCP tool
- Implement Phase 2: `graph_multimodal.py` + first-scan setup flow
- Implement Phase 3: community detection in `graph_builder_v6.3.py`
- Implement Phase 4: `graph_viz.py` + `graph_visualize` MCP tool
- Implement Phase 5: `graph_intelligence.py` (all 8 intel extractors)
