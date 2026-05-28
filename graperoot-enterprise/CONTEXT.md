# GrapeRoot Enterprise — Session Context

## Current Task
Building GrapeRoot Enterprise: full project intelligence layer targeting enterprise teams.
Adds Graphify feature parity + exclusive intelligence features on top of GrapeRoot Pro.
Competitive scrape of graphifylabs.ai completed May 28 2026 — plan updated with full parity list.

## What Is Enterprise
GrapeRoot Enterprise = GrapeRoot Pro (precision MCP context engine) +

**Graphify parity (Phase 1–4):**
- Multi-modal ingestion (PDFs, DOCX, XLSX, images via Claude vision, video/audio via Whisper, YouTube)
- Google Workspace ingestion (.gdoc, .gsheet, .gslides) — opt-in, requires gws auth
- SQL schema extraction (tree-sitter AST, no LLM needed)
- MCP config file ingestion (.mcp.json, claude_desktop_config.json as graph nodes)
- Community detection (Leiden clustering → feature clusters in graph_continue)
- Interactive graph viz (graph.html + GRAPH_REPORT.md, auto-generated)
- Neo4j export (push graph to Neo4j instance)
- SVG export of graph
- Cross-repo graphs (graphify clone equivalent — link multiple repos)
- Live file watcher (watchdog, debounced, no manual graph_scan)
- Design rationale extraction (# WHY, # NOTE, # HACK comments → dedicated graph nodes)
- Confidence tags on graph edges (EXTRACTED / INFERRED / AMBIGUOUS)
- God nodes in GRAPH_REPORT (most-connected concepts)
- Surprising connections in GRAPH_REPORT (cross-module links ranked by unexpectedness)
- Suggested questions in GRAPH_REPORT (4–5 questions the graph is positioned to answer)
- Language expansion: target 33+ languages (currently 7)

**GrapeRoot-exclusive intelligence (Phase 5):**
- Test coverage map (untested symbols flagged inline)
- Git hotspot analysis (churn, bus factor, last-touched)
- API surface map (HTTP/GraphQL routes as graph nodes)
- External API detection (Stripe, OpenAI, etc. catalogued per file)
- Environment/config graph (.env, Docker, k8s, CI/CD)
- TODO/FIXME tracker (tech debt linked to graph nodes)
- Dependency vulnerability scan (npm audit / pip-audit / cargo audit)
- PR intelligence (CI state, conflict risk, AI-ranked queue via GitHub API)

**Pro core advantages (unchanged):**
- Symbol-level reads (per-turn token savings 10–15x)
- Hard per-turn token caps enforced by graph_continue
- Session memory + decisions in context-store
- graph_register_edit (live edit tracking)

## Competitive Intelligence — Graphify (as of May 28 2026)
- **Tagline:** "Any input. One graph. Complete recall."
- **GitHub stars:** 55,370 | **PyPI downloads:** 900,000+ | **Enterprise waitlist:** 2,500+
- **YC S26** (announced May 22, 2026) — solo founder Safi Shamsi
- **Pricing:** Free MIT forever (CLI) — enterprise product "Penpax" is waitlist-only, NOT shipped
- **Privacy model:** code/video processed locally (tree-sitter + Whisper), docs sent to configured AI backend
- **Platforms:** Claude Code, Codex, Cursor, Gemini CLI, Copilot, Aider, Amp, Kiro, Devin, Factory Droid, Trae, Kimi Code, OpenClaw, Google Antigravity, Pi, Hermes, OpenCode, Trae CN + more (20 total)
- **Enterprise vision:** "Local-first enterprise brain, runs on your own infrastructure, shares nothing with any cloud"
- **Gap we exploit:** Enterprise product not shipped. We ship first with clear pricing + precision token layer.

## Key Decisions
- Enterprise lives in `graperoot-enterprise/` subfolder of the Pro repo
- New files: `graph_multimodal.py`, `graph_watcher.py`, `graph_viz.py`, `graph_intelligence.py`
- New server version: `mcp_graph_server_v7.6.py` (8 new MCP tools, 23 total)
- New builder version: `graph_builder_v6.3.py` (multi-modal + Leiden + rationale + confidence tags + intel)
- First-scan interactive setup: 5 prompts before scanning a new project
- Design rationale extraction and confidence tags added (Graphify differentiators worth matching)
- Score: 28/32 features matched or exceeded vs Graphify

## Next Steps
- Implement Phase 1: `graph_watcher.py` + `graph_watch` MCP tool
- Implement Phase 2: `graph_multimodal.py` + first-scan setup + SQL/GWS/MCP config ingestion
- Implement Phase 3: community detection + design rationale + confidence tags in `graph_builder_v6.3.py`
- Implement Phase 4: `graph_viz.py` + god nodes + surprising connections + suggested questions
- Implement Phase 5: `graph_intelligence.py` (all 8 intel extractors)
