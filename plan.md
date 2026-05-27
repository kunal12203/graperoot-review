# Plan: GrapeRoot Pro — Full Project Intelligence

## Context

GrapeRoot Pro is a precision context engine (MCP server) for AI coding assistants. This plan adds Graphify feature parity (multi-modal, community detection, viz, file watcher) plus a full suite of project intelligence features — making GrapeRoot the most complete codebase-aware context layer for AI agents.

User-selected scope:
- **Multi-modal ingestion** (PDFs, DOCX, XLSX, images via Claude vision, audio/video via Whisper, YouTube via yt-dlp)
- **Community detection** (Leiden algorithm — cluster related files so graph_continue can recommend feature clusters)
- **Interactive graph viz** (`graph.html` + `GRAPH_REPORT.md` auto-generated on scan)
- **File watcher** (live auto-rescan when files change on disk — no manual graph_scan)
- **Test coverage map** (which functions have tests, which don't — gaps visible before editing)
- **Git hotspot analysis** (churn rate, bus factor, last-touched — AI knows risky files)
- **API surface map** (HTTP routes, GraphQL, REST endpoints as graph nodes)
- **External API detection** (outbound calls to Stripe, OpenAI, SendGrid etc. catalogued)
- **Environment/config graph** (.env vars, Docker, k8s, CI/CD mapped into graph)
- **TODO/FIXME tracker** (tech debt linked to file nodes, surfaced by graph_continue)
- **Dependency vulnerability scan** (npm audit / pip-audit / cargo audit embedded in graph)
- **PR intelligence** (CI state, review status, conflict risk, AI-ranked PR queue via GitHub API)

---

## New Files

| File | Purpose |
|---|---|
| `graph_watcher.py` | Watchdog-based file watcher daemon |
| `graph_viz.py` | HTML + Markdown report generator |
| `graph_multimodal.py` | Multi-modal extractors (PDF, DOCX, XLSX, image, audio, YouTube) |
| `graph_intelligence.py` | Project intelligence extractors (git, tests, APIs, env, todos, vulns, PRs) |

## Modified Files

| File | Changes |
|---|---|
| `graph_builder_v6.3.py` | Multi-modal pipeline + Leiden community detection + intelligence layer hooks |
| `mcp_graph_server_v7.6.py` | New MCP tools: `graph_watch`, `graph_visualize`, `graph_project_intel`, `graph_pr_triage`; community hints + intel in `graph_continue` |

---

## Phase 1 — File Watcher (`graph_watcher.py`)

**Dependency:** `watchdog`

**Design:**
- `GraphWatcher(project_root, graph_builder, debounce_ms=500)` class
- Uses `watchdog.observers.Observer` + custom `FileChangeHandler`
- On `on_modified` / `on_created` / `on_deleted`: debounce (500ms), then call `graph_builder.rescan_file(path)` for the changed file only
- Excludes: `node_modules`, `.git`, `venv`, `__pycache__`, `dist`, `build`, the graph data dir itself
- Runs as a background daemon thread; stores `watcher_pid` in `~/.dual-graph/<project>/watcher.pid`

**New MCP tool: `graph_watch`** (in mcp_graph_server_v7.6.py)
```
graph_watch(project_root: str, action: "start"|"stop"|"status") -> {watching: bool, pid: int}
```
- `start`: spawns watcher thread, saves PID
- `stop`: kills watcher thread
- `status`: returns whether watcher is running, last-updated timestamp
- Also auto-starts watcher when `graph_scan` completes (can be opted out with `watch=False`)

---

## Phase 2 — Multi-Modal Ingestion (`graph_multimodal.py`)

**Dependencies:** `pdfplumber`, `python-docx`, `openpyxl`, `faster-whisper`, `yt-dlp`, `anthropic` (already present)

### First-Scan Interactive Setup

On the **very first** `graph_scan` of a project (detected by absence of `scan_config.json` in `DG_DATA_DIR`), the CLI/MCP server asks three sequential yes/no questions before scanning:

```
GrapeRoot Pro — First scan setup for <project_name>

[1/3] Include documents in your graph?
      (PDFs, Word docs, spreadsheets, Markdown)
      Found: 3 .pdf, 1 .docx, 2 .xlsx  →  [Y/n]:

[2/3] Include images?
      (PNG, JPG, SVG, GIF — described via Claude vision)
      Found: 12 images  →  [y/N]:

[3/3] Include audio & video?
      (MP3, MP4, WAV, M4A — transcribed via Whisper; YouTube URLs in code)
      Found: 0 media files  →  [y/N]:

Saving preferences to .dual-graph/<project>/scan_config.json
```

- Defaults: docs=yes, images=no, video=no (shown as `[Y/n]` vs `[y/N]`)
- Pre-scan, do a quick count of each type so the user knows what they're opting into
- Preferences saved to `scan_config.json` — future `graph_scan` calls (and the file watcher) use saved prefs, no re-asking
- `graph_watch` (`start`) also re-uses saved prefs
- User can re-run setup with `graph_scan(project_root=..., reconfigure=True)`

**Extractor classes — all return `{text: str, metadata: dict}`:**

1. **PDFExtractor** — `pdfplumber.open(path)` → page text joined; strip boilerplate headers/footers
2. **DocxExtractor** — `python-docx Document(path)` → paragraph text; table cells as TSV rows
3. **XlsxExtractor** — `openpyxl load_workbook(path)` → sheet rows as CSV; first sheet only (cap 500 rows)
4. **ImageExtractor** — Claude Haiku vision API (`claude-haiku-4-5-20251001`); prompt: "Describe this image concisely for a code search index (max 100 words)"; caches result by file hash
5. **AudioVideoExtractor** — `faster-whisper` model `base` (CPU); supports .mp3, .mp4, .wav, .m4a, .webm; transcribes and truncates to 2000 tokens
6. **YouTubeExtractor** — `yt-dlp` downloads audio stream to tmp file → AudioVideoExtractor; caches by YouTube ID

**Integration into graph_builder_v6.3.py:**
- In `_walk_and_extract()`: after code file handling, add `elif` branches for each modal type
- Create a "doc node" per non-code file:
  ```python
  {
    "id": relative_path,
    "node_type": "doc",  # or "pdf" | "image" | "media"
    "keywords": extract_keywords(text),  # top 20 TF-IDF terms
    "summary": text[:500],  # truncated for graph storage
    "full_text_path": f"{DG_DATA_DIR}/doc_texts/{hash}.txt"  # stored separately
  }
  ```
- Link doc nodes to code nodes: scan code files for string literals matching doc filenames → add `"references"` edge
- `graph_read` on a doc node returns its summary + keywords (not full text, to save tokens)

---

## Phase 3 — Community Detection (graph_builder_v6.3.py)

**Dependencies:** `python-igraph`, `leidenalg`

**Design:**
- After full graph build, run `_detect_communities()`:
  1. Build `igraph.Graph` from import edges (directed → undirected for clustering)
  2. Run `leidenalg.find_partition(g, leidenalg.ModularityVertexPartition)`
  3. Assign `community_id` (int) to each node
  4. Name each community by its most-connected node (e.g. `"auth"` if `src/auth.ts` has most edges)
  5. Write `community_index.json`: `{community_id: {name, files: [...]}, ...}`

**Integration into graph_continue (mcp_graph_server_v7.6.py):**
- After selecting `recommended_files`, look up their community IDs
- If `confidence=high` and community has ≥3 files: append up to 2 same-community siblings to `recommended_files`
- Add `community` field to response: `{id, name, size}` — lets the AI mention "you're working in the auth cluster"

---

## Phase 4 — Interactive Visualization (`graph_viz.py`)

**New MCP tool: `graph_visualize`** (in mcp_graph_server_v7.6.py)
```
graph_visualize(project_root: str, output_dir: str = ".") -> {html_path: str, report_path: str}
```
- Also called automatically at end of `graph_scan`

**`graph_viz.py` — two outputs:**

**A. `graph.html`** — self-contained, no server needed
- D3.js v7 force-directed layout (bundled inline, ~200KB)
- Nodes: colored by community (pastel palette), sized by edge count
- Edges: import edges (thin gray), reference edges (doc→code, blue dashed)
- Node types: code (circle), doc (square), image (diamond), media (triangle)
- Hover: shows file path + top 3 keywords
- Click: pins node, shows full symbol list / doc summary in side panel
- Filter bar: language selector, community selector, node type toggle
- Generated from `info_graph.json` + `community_index.json`

**B. `GRAPH_REPORT.md`**
```markdown
# Graph Report — <project_name>

## Summary
- Files: N (code: X, docs: Y, media: Z)
- Languages: TypeScript (40%), Python (30%), ...
- Communities: N clusters
- Dead exports: N symbols
- Circular dependencies: N chains

## Communities
| ID | Name | Size | Key files |
|---|---|---|---|
| 0 | auth | 12 | src/auth.ts, src/middleware/session.ts |
...

## Most Connected Files (top 10)
...

## Dead Exports
...

## Circular Dependencies
...
```

---

## Phase 5 — Project Intelligence (`graph_intelligence.py`)

All extractors run at `graph_scan` time and write their output into `info_graph.json` as enrichment on existing nodes, plus a `project_intel.json` summary file. All are opt-in via the first-scan setup (one additional prompt group: "Enable project intelligence? [Y/n]").

---

### 5a — Test Coverage Map

- Walk all test files (patterns: `*.test.*`, `*.spec.*`, `__tests__/`, `tests/`, `test_*.py`)
- For each test file: extract which source symbols/files it imports → mark those source nodes with `"has_tests": true`
- Source nodes with no matching test: `"has_tests": false`
- Write `coverage_map.json`: `{file: {tested: bool, test_files: [...], untested_symbols: [...]}}`
- **MCP integration:** `graph_continue` appends `"⚠ no tests"` note to recommended files where `has_tests=false`

---

### 5b — Git Hotspot Analysis

- Runs `git log --follow --format="%ad %an" --date=short -- <file>` per file (batched)
- Computes per file:
  - `churn`: commit count in last 90 days
  - `authors`: unique author count
  - `bus_factor`: 1 if single author owns >80% of commits
  - `last_modified`: ISO date
  - `age_days`: days since first commit
- Stores on each graph node; writes `git_hotspots.json` (top 20 by churn)
- **MCP integration:** `graph_continue` appends `"🔥 hotspot (N commits/90d)"` or `"⚠ bus factor 1"` notes on risky files

---

### 5c — API Surface Map

- Scan for HTTP route declarations via patterns:
  - Express/Fastify: `app.get(`, `router.post(`, `app.use(`
  - FastAPI/Flask: `@app.route(`, `@router.get(`, `@app.post(`
  - Go net/http: `mux.HandleFunc(`, `http.HandleFunc(`
  - GraphQL: `type Query {`, `type Mutation {`, SDL files (`.graphql`, `.gql`)
- Create special `"api_route"` nodes: `{method, path, handler_symbol, file}`
- Link route node → handler function node via `"handles"` edge
- Write `api_surface.json`: full route list grouped by prefix
- **MCP integration:** New tool `graph_api_surface()` → returns route list; `graph_continue` on queries mentioning "endpoint", "route", "API" appends matching routes

---

### 5d — External API Detection

- Scan import statements and call sites for known SDK patterns:
  - `import stripe` / `from stripe` / `require('stripe')`
  - `openai`, `anthropic`, `sendgrid`, `twilio`, `aws-sdk`, `boto3`, `firebase`, `supabase`, `prisma`, `mongoose`, `redis`, `pg`, `mysql2`, etc.
  - Raw `fetch`/`axios`/`requests` calls to external URLs (regex: `https?://[^localhost]`)
- Tag each file node with `"external_apis": ["stripe", "openai"]`
- Write `external_apis.json`: `{api: {files: [...], call_count: N}}`
- **MCP integration:** `graph_continue` prepends `"calls: Stripe, OpenAI"` on files that use external APIs

---

### 5e — Environment / Config Graph

- Parse and index:
  - `.env`, `.env.*` — extract var names (not values) as `env_var` nodes
  - `docker-compose.yml` — services, ports, volumes as `infra` nodes
  - `k8s/*.yaml` / `helm/` — deployments, services, config maps
  - `.github/workflows/*.yml` — job names, triggers, steps
  - `Dockerfile` — base image, exposed ports
- Link `env_var` nodes to code files that reference them (`process.env.X`, `os.environ['X']`)
- Write `config_graph.json`
- **MCP integration:** `graph_continue` on infra/deploy queries returns relevant config nodes; warns if a referenced env var has no `.env` definition

---

### 5f — TODO/FIXME Tracker

- `graph_grep_all("TODO|FIXME|HACK|XXX|BUG|OPTIMIZE")` across all code files
- For each hit: `{file, line, type, text, symbol}` — resolve containing symbol via symbol_index
- Link to file node as `"debt"` edge
- Write `tech_debt.json`: grouped by type, sorted by file hotspot score
- **MCP integration:** `graph_continue` appends `"has TODOs (N)"` on files with debt; new tool `graph_tech_debt(file_prefix?)` returns filtered debt list

---

### 5g — Dependency Vulnerability Scan

- Detect package manager: `package.json` → `npm audit --json`, `Pipfile`/`requirements.txt` → `pip-audit --json`, `Cargo.toml` → `cargo audit --json`
- Parse audit output → extract CVEs with severity (critical/high/medium/low)
- Write `vuln_report.json`: `{package, version, cve, severity, fix_version}`
- Tag import nodes: if `import stripe@2.0` and stripe@2.0 has a CVE → node gets `"vuln": {cve, severity}`
- **MCP integration:** `graph_continue` prepends `"⚠ CVE-XXXX (high)"` on files that import vulnerable packages

---

### 5h — PR Intelligence (GitHub API)

- Requires `GITHUB_TOKEN` env var (already present from webhook.py)
- Fetches open PRs via GitHub REST API: title, author, CI status, review state, labels, changed files
- For each PR: map changed files to graph nodes → compute `impact_score` via existing `graph_impact` logic
- Detect merge conflict risk: PRs sharing the same community cluster = conflict candidates
- AI triage ranking: Claude Haiku scores each PR by impact + staleness + CI state
- Write `pr_intel.json`; refresh on `graph_watch` event when `.git/refs/` changes
- **New MCP tool: `graph_pr_triage()`** → returns ranked PR list with risk flags
- **`graph_visualize`**: adds PR overlay to `graph.html` — changed files highlighted per PR, color by CI state

---

## Dependencies to Add

```
# requirements.txt additions
watchdog>=4.0
pdfplumber>=0.11
python-docx>=1.1
openpyxl>=3.1
faster-whisper>=1.0
yt-dlp>=2024.11
python-igraph>=0.11
leidenalg>=0.10
# pip-audit for Python vuln scanning (npm audit + cargo audit are CLI tools, no pip dep needed)
pip-audit>=2.7
```

Image extraction uses the existing `anthropic` SDK — no new dep.
PR intelligence uses the existing `GITHUB_TOKEN` + `requests` — no new dep.

---

## Versioning

- `graph_builder_v6.3.py` — copy of v6.2 + Phase 2 + Phase 3 + Phase 5 (intelligence hooks) additions
- `mcp_graph_server_v7.6.py` — copy of v7.5 + `graph_watch`, `graph_visualize`, `graph_project_intel`, `graph_pr_triage`, `graph_api_surface`, `graph_tech_debt` + community/intel hints in `graph_continue`
- `graph_multimodal.py` — new file, imported by graph_builder
- `graph_watcher.py` — new file, imported by MCP server
- `graph_viz.py` — new file, imported by MCP server
- `graph_intelligence.py` — new file, imported by graph_builder + MCP server

---

## First-Scan Setup Flow (updated)

```
GrapeRoot Pro — First scan setup for <project_name>

[1/4] Include documents? (PDFs, Word, spreadsheets)
      Found: 3 .pdf, 1 .docx  →  [Y/n]:

[2/4] Include images? (described via Claude vision)
      Found: 12 images  →  [y/N]:

[3/4] Include audio & video? (transcribed via Whisper)
      Found: 0 media files  →  [y/N]:

[4/4] Enable project intelligence?
      (git hotspots, test coverage, API surface, env vars,
       TODOs, vulnerability scan, PR triage)  →  [Y/n]:

Saving preferences to .dual-graph/<project>/scan_config.json
Scanning...
```

---

## New MCP Tools Summary

| Tool | Description |
|---|---|
| `graph_watch` | Start/stop/status live file watcher |
| `graph_visualize` | Generate graph.html + GRAPH_REPORT.md |
| `graph_project_intel` | Return full project intelligence summary |
| `graph_api_surface` | List all HTTP/GraphQL routes |
| `graph_tech_debt` | List TODO/FIXME/HACK entries by file |
| `graph_pr_triage` | Ranked open PRs with CI state + conflict risk |

---

## Verification

1. **Multi-modal setup:** First `graph_scan` → 4 prompts with correct file counts → prefs saved to `scan_config.json` → re-scan uses saved prefs
2. **Multi-modal content:** `graph_read("README.pdf")` returns summary + keywords
3. **Community detection:** `graph_continue("auth login")` response includes `community` field
4. **File watcher:** Edit a .ts file → `info_graph.json` updates within 1s, no manual scan
5. **Viz:** `graph_visualize` → `graph.html` opens, communities color-coded; `GRAPH_REPORT.md` has full tables
6. **Git hotspots:** `graph_project_intel` → `hotspots` list shows top churned files with commit counts
7. **Test coverage:** `graph_continue("UserService")` → response notes `"⚠ no tests"` if untested
8. **API surface:** `graph_api_surface()` → returns all routes; route nodes visible in `graph.html`
9. **External APIs:** `graph_continue("payment")` → file using Stripe annotated with `"calls: Stripe"`
10. **Vulns:** After `npm audit` finds a CVE → `graph_continue` on file importing that package shows `"⚠ CVE-XXXX (high)"`
11. **PR triage:** `graph_pr_triage()` → returns ranked PRs with conflict risk; `graph.html` shows PR overlay
