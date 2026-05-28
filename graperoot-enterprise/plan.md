# GrapeRoot Enterprise — Full Implementation Plan

## What We're Building

GrapeRoot Enterprise is GrapeRoot Pro + full project intelligence. It targets engineering teams
at mid-size to enterprise companies who need their AI coding assistants to understand not just
code structure, but the full project: docs, media, tests, git history, APIs, config, deps, and PRs.

**Competitive position:** Graphify (YC S26, 55k stars, 900k downloads) has breadth and
viral momentum. GrapeRoot Enterprise has breadth + precision — every feature feeds back into
`graph_continue`'s per-turn symbol-level context with hard token caps. Graphify's enterprise
product "Penpax" is waitlist-only as of May 28 2026. We ship first.

### Graphify at a glance (scraped May 28 2026)
- **Tagline:** "Any input. One graph. Complete recall."
- **Stars:** 55,370 GitHub | **Downloads:** 900,000+ PyPI | **Enterprise waitlist:** 2,500+
- **YC S26** — solo founder, Penpax (enterprise layer) not yet shipped
- **Pricing:** Free MIT forever — no paid tier announced
- **Privacy:** Code/video local (tree-sitter + Whisper). Docs sent to configured AI backend. No telemetry.

---

## Graphify Feature Comparison — Full Parity Matrix

| # | Feature | Graphify | GrapeRoot Enterprise | Phase |
|---|---|---|---|---|
| 1 | Code graph (AST, multi-language) | ✅ 33 langs | ✅ 7 → 33 langs (roadmap) | Builder |
| 2 | PDF ingestion | ✅ | ✅ | 2 |
| 3 | DOCX / XLSX ingestion | ✅ | ✅ | 2 |
| 4 | Image ingestion (Claude vision) | ✅ | ✅ | 2 |
| 5 | Audio / video transcription (Whisper) | ✅ | ✅ | 2 |
| 6 | YouTube URL ingestion | ✅ | ✅ | 2 |
| 7 | Google Workspace (.gdoc, .gsheet) | ✅ | ✅ opt-in | 2 |
| 8 | SQL schema extraction (tree-sitter) | ✅ v0.6.0 | ✅ | 2 |
| 9 | MCP config file ingestion | ✅ | ✅ | 2 |
| 10 | Community detection (Leiden) | ✅ | ✅ | 3 |
| 11 | Interactive graph.html viz | ✅ | ✅ | 4 |
| 12 | GRAPH_REPORT.md | ✅ | ✅ | 4 |
| 13 | God nodes (most-connected concepts) | ✅ | ✅ | 4 |
| 14 | Surprising connections | ✅ | ✅ | 4 |
| 15 | Suggested questions in report | ✅ | ✅ | 4 |
| 16 | Confidence tags (EXTRACTED/INFERRED/AMBIGUOUS) | ✅ | ✅ | 3 |
| 17 | Design rationale extraction (WHY/NOTE/HACK) | ✅ | ✅ | 3 |
| 18 | Cross-repo graphs | ✅ v0.5.0 | ✅ roadmap | Roadmap |
| 19 | Neo4j export | ✅ | ✅ opt-in | 4 |
| 20 | SVG export | ✅ | ✅ | 4 |
| 21 | MCP server mode | ✅ secondary | ✅ primary | Core |
| 22 | PR intelligence dashboard | ✅ | ✅ | 5h |
| 23 | CI state + conflict risk | ✅ | ✅ | 5h |
| 24 | Live file watcher | ❌ | ✅ | 1 |
| 25 | Symbol-level reads (token savings) | ❌ | ✅ Pro core | Core |
| 26 | Per-turn token caps / gate | ❌ | ✅ Pro core | Core |
| 27 | Session memory + decisions | ❌ | ✅ Pro core | Core |
| 28 | Test coverage map | ❌ | ✅ | 5a |
| 29 | Git hotspot / bus factor | ❌ | ✅ | 5b |
| 30 | API surface map | ❌ | ✅ | 5c |
| 31 | External API detection | ❌ | ✅ | 5d |
| 32 | Env / config / infra graph | ❌ | ✅ | 5e |
| 33 | TODO/FIXME tracker | ❌ | ✅ | 5f |
| 34 | Dependency vulnerability scan | ❌ | ✅ | 5g |

**Score: 30/34 features matched or exceeded (Graphify: 23/34)**

---

## New Files

| File | Purpose |
|---|---|
| `graph_watcher.py` | Watchdog-based live file watcher daemon |
| `graph_viz.py` | graph.html + GRAPH_REPORT.md generator (god nodes, surprising connections, suggested questions) |
| `graph_multimodal.py` | Multi-modal extractors (PDF, DOCX, XLSX, image, audio, YouTube, GWS, SQL, MCP config) |
| `graph_intelligence.py` | Project intelligence extractors (8 subsystems) |

## Modified Files (new versions)

| File | Base | Changes |
|---|---|---|
| `graph_builder_v6.3.py` | v6.2 | Multi-modal + Leiden + design rationale + confidence tags + intelligence hooks |
| `mcp_graph_server_v7.6.py` | v7.5 | 8 new MCP tools + community/intel/rationale annotations in graph_continue |

---

## Phase 1 — Live File Watcher (`graph_watcher.py`)

**Dep:** `watchdog>=4.0`

```
GraphWatcher(project_root, graph_builder, debounce_ms=500)
```
- `watchdog.observers.Observer` + `FileChangeHandler`
- `on_modified/created/deleted` → debounce 500ms → `graph_builder.rescan_file(path)`
- Excludes: `node_modules`, `.git`, `venv`, `__pycache__`, `dist`, `build`, DG data dir
- Daemon thread; PID in `~/.dual-graph/<project>/watcher.pid`

**New MCP tool:** `graph_watch(project_root, action: "start"|"stop"|"status")`
- Auto-starts after `graph_scan` (opt-out: `watch=False`)

---

## Phase 2 — Multi-Modal Ingestion (`graph_multimodal.py`)

**Deps:** `pdfplumber`, `python-docx`, `openpyxl`, `faster-whisper`, `yt-dlp`, `google-auth`, `google-api-python-client`

### First-Scan Interactive Setup (5 prompts)

```
GrapeRoot Enterprise — First scan setup for <project_name>

[1/5] Include documents? (PDFs, Word, spreadsheets, MCP configs, SQL schemas)
      Found: N .pdf, N .docx, N .xlsx, N .sql  →  [Y/n]:

[2/5] Include Google Workspace? (.gdoc, .gsheet, .gslides — requires gws auth)
      Found: N files  →  [y/N]:

[3/5] Include images? (described via Claude Haiku vision)
      Found: N images  →  [y/N]:

[4/5] Include audio & video? (transcribed via Whisper — local, no API calls)
      Found: N media files  →  [y/N]:

[5/5] Enable project intelligence?
      (git hotspots, test coverage, API surface, TODOs, vulns, PR triage)
      →  [Y/n]:

Saving to .dual-graph/<project>/scan_config.json
```

Prefs saved; future scans skip prompts. Re-run with `reconfigure=True`.

### Extractors (all return `{text, metadata}`)

1. **PDFExtractor** — `pdfplumber` → joined page text, strip headers/footers
2. **DocxExtractor** — `python-docx` → paragraphs + tables as TSV
3. **XlsxExtractor** — `openpyxl` → first sheet, max 500 rows, CSV
4. **ImageExtractor** — Claude Haiku vision (`claude-haiku-4-5-20251001`), cached by file hash; prompt: "Describe for code search index (max 100 words)"
5. **AudioVideoExtractor** — `faster-whisper` base model (local, no API), truncated to 2000 tokens; .mp3/.mp4/.wav/.m4a/.webm
6. **YouTubeExtractor** — `yt-dlp` → tmp audio → AudioVideoExtractor, cached by YT ID
7. **GoogleWorkspaceExtractor** — Google Drive API; .gdoc→text, .gsheet→CSV, .gslides→slide text; requires `gws` OAuth setup
8. **SQLExtractor** — tree-sitter SQL grammar; extracts tables, columns, indexes, foreign keys as typed nodes (`sql_table`, `sql_column`); no LLM needed
9. **MCPConfigExtractor** — parse `.mcp.json`, `mcp.json`, `claude_desktop_config.json`; extract server names, commands, env var names as `mcp_server` nodes

### Doc nodes in graph

```python
{
  "id": relative_path,
  "node_type": "doc",          # pdf | image | media | sql_table | mcp_server | gws_doc
  "keywords": [...],           # top 20 TF-IDF terms
  "summary": text[:500],
  "full_text_path": "..."      # stored separately
}
```

`graph_read` on doc node → returns summary + keywords only (token-safe).

---

## Phase 3 — Community Detection + Design Rationale + Confidence Tags (`graph_builder_v6.3.py`)

**Deps:** `python-igraph>=0.11`, `leidenalg>=0.10`

### 3a — Community Detection
After graph build, run `_detect_communities()`:
1. Build undirected `igraph.Graph` from import edges
2. `leidenalg.find_partition(g, ModularityVertexPartition)`
3. Name each community by most-connected node (e.g. `"auth"`)
4. Write `community_index.json`

**In `graph_continue`:** append up to 2 same-community siblings when `confidence=high` and size ≥ 3. Add `community: {id, name, size}` to response.

### 3b — Design Rationale Extraction
During `_walk_and_extract()`, scan every code file for inline rationale comments:
- Patterns: `# WHY:`, `# NOTE:`, `# HACK:`, `# REASON:`, `// WHY:`, `// NOTE:`, `// HACK:`, `/* WHY:`, `/* REASON:`
- Each hit → create a `rationale` node: `{id, file, line, type: "WHY"|"NOTE"|"HACK", text, linked_symbol}`
- Link to the containing symbol node via `"explains"` edge
- Write `design_rationale.json`: grouped by file
- **`graph_read` on a code symbol** → appends any linked rationale nodes inline
- **`graph_continue`** → if recommended file has rationale nodes, prepend count: `"3 design notes"`
- **GRAPH_REPORT.md** → new section: "Design Decisions" listing WHY comments by community

### 3c — Confidence Tags on Graph Edges
Every edge in `info_graph.json` gets a `confidence` field:
- `EXTRACTED` — directly parsed from AST (import statement, explicit call)
- `INFERRED` — derived from naming patterns, co-location, or keyword overlap
- `AMBIGUOUS` — multiple possible targets; edge exists but may be wrong

Rules:
- Import edges (explicit `import`/`require`) → `EXTRACTED`
- Same-community co-occurrence with no direct import → `INFERRED`
- Dynamic requires (`require(variable)`) → `AMBIGUOUS`
- Doc→code reference links → `INFERRED`

**In `graph_continue`:** `AMBIGUOUS` edges are deprioritized in ranking. `EXTRACTED` edges boost score.
**In `graph_viz.py`:** edge line style: solid=EXTRACTED, dashed=INFERRED, dotted=AMBIGUOUS.

---

## Phase 4 — Interactive Visualization (`graph_viz.py`)

**New MCP tools:**
- `graph_visualize(project_root, output_dir=".")` — auto-called at end of `graph_scan`
- `graph_export_neo4j(uri, user, password)` — push to Neo4j
- `graph_export_svg(output_path)` — export SVG

### graph.html (self-contained, no server)
- D3.js v7 force-directed, bundled inline (~200KB)
- Nodes colored by community, sized by edge count
- Node shapes: code=circle, doc=square, image=diamond, media=triangle, sql=hexagon, rationale=star
- Edge styles: solid=EXTRACTED, dashed=INFERRED, dotted=AMBIGUOUS
- Hover: path + top 3 keywords + confidence breakdown
- Click: symbol list / doc summary / rationale notes panel
- Filter bar: language, community, node type, edge confidence
- PR overlay: changed files highlighted by PR, colored by CI state (from Phase 5h)

### GRAPH_REPORT.md (updated structure)

```markdown
# Graph Report — <project>

## Summary
- Files: N (code: X, docs: Y, media: Z, sql: W)
- Languages: TypeScript (40%), Python (30%), ...
- Communities: N clusters
- Dead exports: N symbols
- Circular dependencies: N chains
- Design rationale notes: N (WHY: X, NOTE: Y, HACK: Z)
- Edge confidence: EXTRACTED N%, INFERRED N%, AMBIGUOUS N%

## God Nodes (most-connected concepts)
Top 10 nodes by total edge count — these are the structural core of the project.
| Rank | Node | Edges | Community | Type |
...

## Surprising Connections
Cross-community links ranked by unexpectedness (community distance × edge confidence).
| Connection | Communities | Confidence | Why surprising |
...

## Suggested Questions
4–5 questions the graph is uniquely positioned to answer based on structure and rationale nodes.
1. "Why does <auth> depend on <payments> — there are 3 WHY comments explaining this"
2. ...

## Communities
| ID | Name | Size | Key files | Rationale notes |
...

## Most Connected Files (top 10)
...

## Design Decisions (WHY/NOTE/HACK)
All design rationale comments, grouped by community.
...

## Dead Exports
...

## Circular Dependencies
...

## Test Coverage (% covered)
...

## Top Hotspots (by churn)
...

## Open PRs
...
```

---

## Phase 5 — Project Intelligence (`graph_intelligence.py`)

All run at `graph_scan` time (if enabled). Write into `project_intel.json` + individual JSON files. All results feed annotations into `graph_continue`.

### 5a — Test Coverage Map
- Walk `*.test.*`, `*.spec.*`, `__tests__/`, `test_*.py`
- Map test imports → source nodes → `has_tests: true/false`
- Output: `coverage_map.json`
- Annotation: `"⚠ no tests"` on recommended files

### 5b — Git Hotspot Analysis
- `git log --follow` per file → churn (90d), authors, bus_factor, last_modified, age_days
- Output: `git_hotspots.json` (top 20)
- Annotations: `"🔥 hotspot (N commits/90d)"`, `"⚠ bus factor 1"`

### 5c — API Surface Map
- Detect Express/Fastify, FastAPI/Flask, Go net/http, GraphQL SDL
- Create `api_route` nodes linked to handler symbols
- Output: `api_surface.json`
- New MCP tool: `graph_api_surface(prefix?)`

### 5d — External API Detection
- Detect known SDK imports: stripe, openai, anthropic, sendgrid, twilio, aws-sdk, boto3, firebase, supabase, prisma, mongoose, redis, pg, mysql2, etc.
- Detect raw `fetch`/`axios`/`requests` to external URLs
- Output: `external_apis.json`
- Annotation: `"calls: Stripe, OpenAI"` on relevant files

### 5e — Environment / Config Graph
- `.env*` → env var names as `env_var` nodes
- `docker-compose.yml` → services/ports/volumes as `infra` nodes
- `k8s/`, `helm/` → deployments, services, config maps
- `.github/workflows/*.yml` → CI job names + triggers
- `Dockerfile` → base image, exposed ports
- Link env_var nodes to code files that reference them
- Output: `config_graph.json`

### 5f — TODO/FIXME Tracker
- `graph_grep_all("TODO|FIXME|HACK|XXX|BUG|OPTIMIZE")`
- Resolve containing symbol via symbol_index
- Output: `tech_debt.json` sorted by hotspot score
- Annotation: `"has TODOs (N)"` on files
- New MCP tool: `graph_tech_debt(file_prefix?)`

### 5g — Dependency Vulnerability Scan
- `package.json` → `npm audit --json`
- `requirements.txt` / `Pipfile` → `pip-audit --json`
- `Cargo.toml` → `cargo audit --json`
- Tag import nodes with `vuln: {cve, severity, fix_version}`
- Output: `vuln_report.json`
- Annotation: `"⚠ CVE-XXXX (high)"` on affected files

### 5h — PR Intelligence (GitHub API)
- Requires `GITHUB_TOKEN`
- Fetch open PRs: title, author, CI status, review state, changed files
- Map changed files → graph nodes → `impact_score` via `graph_impact`
- Detect conflict risk: PRs in same community cluster
- Claude Haiku triage ranking (impact + staleness + CI state)
- Output: `pr_intel.json`; refreshed when `.git/refs/` changes
- New MCP tool: `graph_pr_triage()`
- `graph_visualize` adds PR overlay to graph.html

---

## New MCP Tools Summary (8 new, 23 total)

| Tool | Description |
|---|---|
| `graph_watch` | Start/stop/status live file watcher |
| `graph_visualize` | Generate graph.html + GRAPH_REPORT.md |
| `graph_export_neo4j` | Push graph to Neo4j instance |
| `graph_export_svg` | Export graph as SVG |
| `graph_project_intel` | Full project intelligence summary |
| `graph_api_surface` | List HTTP/GraphQL/REST routes |
| `graph_tech_debt` | List TODO/FIXME/HACK by file |
| `graph_pr_triage` | Ranked PRs with CI state + conflict risk |

---

## Dependencies

```
# Phase 1
watchdog>=4.0

# Phase 2
pdfplumber>=0.11
python-docx>=1.1
openpyxl>=3.1
faster-whisper>=1.0
yt-dlp>=2024.11
google-auth>=2.28
google-api-python-client>=2.120

# Phase 3
python-igraph>=0.11
leidenalg>=0.10

# Phase 5
pip-audit>=2.7
```

---

## Implementation Order

1. `graph_watcher.py` + `graph_watch` tool — Phase 1 (standalone)
2. `graph_multimodal.py` + first-scan setup — Phase 2 (PDF/DOCX/image/audio/SQL/MCP config/GWS)
3. `graph_builder_v6.3.py` — Phase 3 (Leiden + design rationale + confidence tags)
4. `graph_viz.py` + `graph_visualize` + Neo4j + SVG — Phase 4 (god nodes, surprising connections, suggested questions)
5. `graph_intelligence.py` — Phase 5 (5a→5h in order)
6. Wire all annotations + rationale + confidence into `graph_continue` in `mcp_graph_server_v7.6.py`

---

## Verification Checklist

- [ ] First `graph_scan` shows 5 prompts with correct file counts
- [ ] `scan_config.json` written; re-scan skips prompts
- [ ] `graph_read("README.pdf")` returns summary + keywords
- [ ] `graph_read("schema.sql")` returns table/column nodes
- [ ] `graph_continue("auth")` returns `community: {name: "auth", size: N}`
- [ ] Edit a .ts file → `info_graph.json` updates within 1s (watcher running)
- [ ] `graph_visualize` → `graph.html` opens with god nodes highlighted, edge styles correct
- [ ] `GRAPH_REPORT.md` has god nodes, surprising connections, suggested questions, design decisions
- [ ] `# WHY:` comment in code → shows as rationale node in graph_read output
- [ ] Import edges tagged `EXTRACTED`, co-occurrence edges tagged `INFERRED`
- [ ] `graph_project_intel` → hotspots list shows top churned files
- [ ] `graph_continue("UserService")` → `"⚠ no tests"` if untested
- [ ] `graph_api_surface()` → returns all routes
- [ ] `graph_continue("payment")` → `"calls: Stripe"` annotation
- [ ] After vuln scan → `"⚠ CVE-XXXX (high)"` on affected files
- [ ] `graph_pr_triage()` → ranked PRs with conflict risk
- [ ] `graph_export_neo4j(uri, user, pass)` → nodes visible in Neo4j browser
