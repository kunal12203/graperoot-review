# GrapeRoot Enterprise — Full Implementation Plan

## What We're Building

GrapeRoot Enterprise is GrapeRoot Pro + full project intelligence. It targets engineering teams
at mid-size to enterprise companies who need their AI coding assistants to understand not just
code structure, but the full project: docs, media, tests, git history, APIs, config, deps, and PRs.

**Competitive position:** Graphify (YC S26, 54k stars) has breadth. GrapeRoot Enterprise has
breadth + precision — every feature feeds back into `graph_continue`'s per-turn symbol-level
context, so the AI never wastes tokens reading what it doesn't need.

---

## Graphify Feature Comparison (what we're matching and exceeding)

| # | Feature | Graphify | GrapeRoot Enterprise |
|---|---|---|---|
| 1 | Code graph (AST, multi-language) | ✅ 32 langs | ✅ 7 langs (expanding) |
| 2 | PDF / DOCX / XLSX ingestion | ✅ | ✅ Phase 2 |
| 3 | Image ingestion (vision) | ✅ | ✅ Phase 2 |
| 4 | Audio / video transcription | ✅ Whisper | ✅ Phase 2 |
| 5 | YouTube URL ingestion | ✅ | ✅ Phase 2 |
| 6 | Google Sheets / SQL schemas | ✅ | Roadmap |
| 7 | Community detection (Leiden) | ✅ | ✅ Phase 3 |
| 8 | Interactive graph.html viz | ✅ | ✅ Phase 4 |
| 9 | GRAPH_REPORT.md | ✅ | ✅ Phase 4 |
| 10 | MCP server mode | ✅ secondary | ✅ primary (our core) |
| 11 | Slash-command queries | ✅ | via graph_continue |
| 12 | PR intelligence dashboard | ✅ | ✅ Phase 5h |
| 13 | CI state + conflict risk | ✅ | ✅ Phase 5h |
| 14 | Live file watcher | ❌ | ✅ Phase 1 |
| 15 | Symbol-level reads (token savings) | ❌ | ✅ Pro core |
| 16 | Per-turn token caps / gate | ❌ | ✅ Pro core |
| 17 | Session memory + decisions | ❌ | ✅ Pro core |
| 18 | Test coverage map | ❌ | ✅ Phase 5a |
| 19 | Git hotspot / bus factor | ❌ | ✅ Phase 5b |
| 20 | API surface map | ❌ | ✅ Phase 5c |
| 21 | External API detection | ❌ | ✅ Phase 5d |
| 22 | Env / config / infra graph | ❌ | ✅ Phase 5e |
| 23 | TODO/FIXME tracker | ❌ | ✅ Phase 5f |
| 24 | Dependency vulnerability scan | ❌ | ✅ Phase 5g |

**Score: 20/24 features matched or exceeded (vs 14/24 for Pro today)**

---

## New Files

| File | Purpose |
|---|---|
| `graph_watcher.py` | Watchdog-based live file watcher daemon |
| `graph_viz.py` | graph.html + GRAPH_REPORT.md generator |
| `graph_multimodal.py` | Multi-modal extractors (PDF, DOCX, XLSX, image, audio, YouTube) |
| `graph_intelligence.py` | Project intelligence extractors (8 subsystems) |

## Modified Files (new versions)

| File | Base | Changes |
|---|---|---|
| `graph_builder_v6.3.py` | v6.2 | Multi-modal pipeline + Leiden community detection + intelligence hooks |
| `mcp_graph_server_v7.6.py` | v7.5 | 6 new MCP tools + community/intel annotations in graph_continue |

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

**Deps:** `pdfplumber`, `python-docx`, `openpyxl`, `faster-whisper`, `yt-dlp`

### First-Scan Interactive Setup (4 prompts)

```
GrapeRoot Enterprise — First scan setup for <project_name>

[1/4] Include documents? (PDFs, Word, spreadsheets)
      Found: N .pdf, N .docx, N .xlsx  →  [Y/n]:

[2/4] Include images? (described via Claude Haiku vision)
      Found: N images  →  [y/N]:

[3/4] Include audio & video? (transcribed via Whisper)
      Found: N media files  →  [y/N]:

[4/4] Enable project intelligence?
      (git hotspots, test coverage, API surface, TODOs, vulns, PR triage)
      →  [Y/n]:

Saving to .dual-graph/<project>/scan_config.json
```

Prefs saved; future scans skip prompts. Re-run with `reconfigure=True`.

### Extractors (all return `{text, metadata}`)

1. **PDFExtractor** — `pdfplumber` → joined page text
2. **DocxExtractor** — `python-docx` → paragraphs + tables as TSV
3. **XlsxExtractor** — `openpyxl` → first sheet, max 500 rows, CSV
4. **ImageExtractor** — Claude Haiku vision (`claude-haiku-4-5-20251001`), cached by file hash
5. **AudioVideoExtractor** — `faster-whisper` base model, truncated to 2000 tokens
6. **YouTubeExtractor** — `yt-dlp` → tmp audio → AudioVideoExtractor, cached by YT ID

### Doc nodes in graph

```python
{
  "id": relative_path,
  "node_type": "doc",          # or "pdf" | "image" | "media"
  "keywords": [...],           # top 20 TF-IDF terms
  "summary": text[:500],
  "full_text_path": "..."      # stored separately, not in info_graph.json
}
```

`graph_read` on a doc node → returns summary + keywords only (token-safe).

---

## Phase 3 — Community Detection (in `graph_builder_v6.3.py`)

**Deps:** `python-igraph>=0.11`, `leidenalg>=0.10`

After graph build, run `_detect_communities()`:
1. Build undirected `igraph.Graph` from import edges
2. `leidenalg.find_partition(g, ModularityVertexPartition)`
3. Name each community by most-connected node (e.g. `"auth"`)
4. Write `community_index.json`

**In `graph_continue`:** append up to 2 same-community siblings when `confidence=high` and community size ≥ 3. Add `community: {id, name, size}` to response.

---

## Phase 4 — Interactive Visualization (`graph_viz.py`)

**New MCP tool:** `graph_visualize(project_root, output_dir=".")`
Auto-called at end of `graph_scan`.

### graph.html (self-contained, no server)
- D3.js v7 force-directed, bundled inline
- Nodes colored by community, sized by edge count
- Node shapes: code=circle, doc=square, image=diamond, media=triangle
- Hover: path + top 3 keywords; Click: symbol list / doc summary panel
- Filter bar: language, community, node type
- PR overlay: changed files highlighted by PR, colored by CI state (Phase 5h)

### GRAPH_REPORT.md
```
# Graph Report — <project>
## Summary: files, languages, communities, dead exports, cycles
## Communities table
## Most Connected Files (top 10)
## Dead Exports
## Circular Dependencies
## Test Coverage (% covered)
## Top Hotspots (by churn)
## Open PRs (from pr_intel.json)
```

---

## Phase 5 — Project Intelligence (`graph_intelligence.py`)

All run at `graph_scan` time (if enabled in `scan_config.json`). Write into `project_intel.json` + individual JSON files. All results feed annotations into `graph_continue`.

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
- Detect known SDK imports (stripe, openai, anthropic, sendgrid, twilio, aws-sdk, boto3, firebase, supabase, prisma, mongoose, redis, pg, mysql2, etc.)
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

## New MCP Tools Summary (6 new, 21 total)

| Tool | Description |
|---|---|
| `graph_watch` | Start/stop/status live file watcher |
| `graph_visualize` | Generate graph.html + GRAPH_REPORT.md |
| `graph_project_intel` | Full project intelligence summary |
| `graph_api_surface` | List HTTP/GraphQL/REST routes |
| `graph_tech_debt` | List TODO/FIXME/HACK by file |
| `graph_pr_triage` | Ranked PRs with CI state + conflict risk |

---

## Dependencies

```
watchdog>=4.0
pdfplumber>=0.11
python-docx>=1.1
openpyxl>=3.1
faster-whisper>=1.0
yt-dlp>=2024.11
python-igraph>=0.11
leidenalg>=0.10
pip-audit>=2.7
```

---

## Implementation Order

1. `graph_watcher.py` + `graph_watch` tool (Phase 1) — standalone, no deps on other phases
2. `graph_multimodal.py` + first-scan setup (Phase 2)
3. Community detection in `graph_builder_v6.3.py` (Phase 3)
4. `graph_viz.py` + `graph_visualize` tool (Phase 4)
5. `graph_intelligence.py` — implement 5a→5h in order (Phase 5)
6. Wire all annotations into `graph_continue` in `mcp_graph_server_v7.6.py`

---

## Verification Checklist

- [ ] First `graph_scan` shows 4 prompts with correct file counts
- [ ] `scan_config.json` written; re-scan skips prompts
- [ ] `graph_read("README.pdf")` returns summary + keywords
- [ ] `graph_continue("auth")` returns `community: {name: "auth", size: N}`
- [ ] Edit a .ts file → `info_graph.json` updates within 1s (watcher running)
- [ ] `graph_visualize` → `graph.html` opens, communities color-coded
- [ ] `GRAPH_REPORT.md` has communities table + coverage % + hotspots
- [ ] `graph_project_intel` → `hotspots` list shows top churned files
- [ ] `graph_continue("UserService")` → `"⚠ no tests"` if untested
- [ ] `graph_api_surface()` → returns all routes
- [ ] `graph_continue("payment")` → `"calls: Stripe"` annotation
- [ ] After vuln scan → `"⚠ CVE-XXXX (high)"` on affected files
- [ ] `graph_pr_triage()` → ranked PRs with conflict risk
