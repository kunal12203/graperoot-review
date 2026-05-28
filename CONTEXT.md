# Session Context

## Current Task
Building GrapeRoot Enterprise: full project intelligence layer targeting enterprise engineering teams.
`graperoot-enterprise/` folder created with CONTEXT.md, MIGRATION.md, CLAUDE.md, plan.md.

## Competitive Intel (scraped May 28 2026)
- Graphify: 55,370 stars, 900k downloads, YC S26, solo founder
- Tagline: "Any input. One graph. Complete recall."
- Enterprise product "Penpax" = waitlist only, NOT shipped — we ship first
- Pricing: free MIT forever, no paid tier announced

## Key Decisions
- **Enterprise = Pro + 16 new features**: full Graphify parity + 8 exclusive intel features
- **30/34 features matched or exceeded** vs Graphify (23/34)
- Added from scrape: design rationale extraction (WHY/NOTE/HACK), confidence tags (EXTRACTED/INFERRED/AMBIGUOUS), god nodes, surprising connections, suggested questions, SQL ingestion, MCP config ingestion, Google Workspace, Neo4j export, SVG export
- **New server: mcp_graph_server_v7.6.py** (8 new MCP tools, 23 total)
- **New builder: graph_builder_v6.3.py** (multi-modal + Leiden + rationale + confidence + intel)
- **First-scan setup: 5 prompts** (added GWS prompt)

## Next Steps
- Implement Phase 1: graph_watcher.py + graph_watch MCP tool
- Implement Phase 2: graph_multimodal.py + SQL/GWS/MCP config ingestion + 5-prompt setup
- Implement Phase 3: Leiden + design rationale + confidence tags in graph_builder_v6.3.py
- Then Phase 4 (viz: god nodes, surprising connections, suggested questions) and Phase 5 (8 intel)
