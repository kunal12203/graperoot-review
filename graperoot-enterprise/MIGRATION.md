# Migrating from GrapeRoot Pro → GrapeRoot Enterprise

## What Changes

| Area | Pro | Enterprise |
|---|---|---|
| MCP server | `mcp_graph_server_v7.5.py` | `mcp_graph_server_v7.6.py` |
| Graph builder | `graph_builder_v6.2.py` | `graph_builder_v6.3.py` |
| New files | — | `graph_multimodal.py`, `graph_watcher.py`, `graph_viz.py`, `graph_intelligence.py` |
| New MCP tools | 15 tools | 21 tools (+6) |
| New output files | — | `community_index.json`, `project_intel.json`, `coverage_map.json`, `git_hotspots.json`, `api_surface.json`, `external_apis.json`, `config_graph.json`, `tech_debt.json`, `vuln_report.json`, `pr_intel.json` |

## Step-by-Step Migration

### 1. Install new dependencies

```bash
pip install watchdog>=4.0 pdfplumber>=0.11 python-docx>=1.1 openpyxl>=3.1 \
            faster-whisper>=1.0 yt-dlp>=2024.11 python-igraph>=0.11 \
            leidenalg>=0.10 pip-audit>=2.7
```

### 2. Stop your existing Pro MCP server

```bash
# Find and kill the running v7.5 server
pkill -f mcp_graph_server_v7.5
```

### 3. Start the Enterprise server

```bash
python mcp_graph_server_v7.6.py
```

### 4. Update your MCP config

Replace the server script path in your MCP config file:

**Claude Code** (`~/.claude/mcp_config.json` or `.mcp.json`):
```json
{
  "graperoot-pro": {
    "command": "python",
    "args": ["/path/to/GrapeRoot Pro/mcp_graph_server_v7.6.py"]
  }
}
```

**Cursor** (`.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "graperoot-pro": {
      "command": "python",
      "args": ["/path/to/GrapeRoot Pro/mcp_graph_server_v7.6.py"]
    }
  }
}
```

### 5. Re-scan your project (first-time enterprise setup)

The first `graph_scan` after upgrading will trigger the interactive setup:

```
GrapeRoot Enterprise — First scan setup for <your-project>

[1/4] Include documents? (PDFs, Word, spreadsheets)
      Found: N files  →  [Y/n]:

[2/4] Include images? (described via Claude vision)
      Found: N files  →  [y/N]:

[3/4] Include audio & video? (transcribed via Whisper)
      Found: N files  →  [y/N]:

[4/4] Enable project intelligence?
      (git hotspots, test coverage, API surface, TODOs, vulns, PR triage)
      →  [Y/n]:
```

Your answers are saved to `.dual-graph/<project>/scan_config.json` — you won't be asked again.

To reconfigure later: `graph_scan(project_root=".", reconfigure=True)`

### 6. Start the file watcher (optional but recommended)

```
graph_watch(project_root=".", action="start")
```

The watcher auto-starts after `graph_scan` by default. It debounces file changes (500ms) and updates only the changed file — no full rescan needed.

---

## New MCP Tools Reference

| Tool | When to use |
|---|---|
| `graph_watch(action="start\|stop\|status")` | Live file watching |
| `graph_visualize(project_root, output_dir)` | Generate graph.html + GRAPH_REPORT.md |
| `graph_project_intel()` | Full intelligence summary (hotspots, coverage, vulns, APIs) |
| `graph_api_surface(prefix?)` | List HTTP/GraphQL routes, optionally filtered |
| `graph_tech_debt(file_prefix?)` | List TODO/FIXME/HACK entries |
| `graph_pr_triage()` | Ranked open PRs with CI state + conflict risk |

---

## Backwards Compatibility

- All 15 existing Pro MCP tools are unchanged in v7.6
- `info_graph.json` format is extended (new fields on nodes) but backwards-compatible
- `scan_config.json` is new — if absent, Pro-mode scan runs (no multi-modal, no intel)
- The CLAUDE.md policy is unchanged — `graph_continue` still required first every turn

---

## Rollback

To roll back to Pro, simply restart `mcp_graph_server_v7.5.py` and update your MCP config path. No data is lost — Enterprise output files are additive.

---

## Requirements

| Requirement | Details |
|---|---|
| Python | ≥ 3.11 |
| `ANTHROPIC_API_KEY` | Required for image extraction (Claude Haiku vision) |
| `GITHUB_TOKEN` | Required for PR intelligence (optional otherwise) |
| git | Required for git hotspot analysis |
| npm / pip / cargo | Required for vulnerability scanning (whichever applies to your project) |
