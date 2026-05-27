<!-- graperoot-enterprise-v1 -->
# GrapeRoot Enterprise Context Policy

> **For enterprise codebases — full project intelligence layer.**
> Builds on GrapeRoot Pro precision with multi-modal ingestion, community detection,
> live file watching, and 8 project intelligence extractors.

---

## MANDATORY: Always follow this order

### Step 1 — Graph-first (no exceptions)

1. **Call `graph_continue` FIRST** — before any file read, grep, or bash command.
2. **If `needs_project=true`**: call `graph_scan(path=<pwd>)` immediately. Do NOT ask the user.
3. **If `skip=true`**: repo has fewer than 5 files. Read only files explicitly named.

### Step 2 — Read recommended files

- Call `graph_read` **once per file** (never batch).
- Entries may be `file::symbol` — pass verbatim.
- `graph_continue` response may include `community` field — use it to understand which feature cluster the query belongs to.

### Step 3 — Apply Enterprise confidence caps

| `confidence` | greps allowed | extra files allowed |
|---|---|---|
| `high` | up to **2** | up to **1** |
| `medium` | up to `max_supplementary_greps` | up to `max_supplementary_files` |
| `low` | up to `max_supplementary_greps` | up to `max_supplementary_files` |

`confidence=high` is NOT a hard stop. Use extra greps for exhaustive tasks only.

### Step 4 — Use intelligence tools proactively

When relevant, call these without waiting for the user to ask:

| Situation | Tool |
|---|---|
| Editing a file with `"⚠ no tests"` annotation | `graph_tech_debt(file)` to check debt too |
| Query mentions "endpoint", "route", "API" | `graph_api_surface()` |
| Query mentions "PR", "review", "merge" | `graph_pr_triage()` |
| Starting a large refactor | `graph_project_intel()` for full context |
| File has `"⚠ CVE-XXXX"` annotation | Mention the CVE and fix version |

---

## Intelligence Annotations in graph_continue

`graph_continue` response may include inline annotations on `recommended_files`:

- `"⚠ no tests"` — file has no test coverage
- `"🔥 hotspot (N commits/90d)"` — high-churn file, risky to edit
- `"⚠ bus factor 1"` — only one author has touched this file
- `"calls: Stripe, OpenAI"` — file makes outbound API calls
- `"has TODOs (N)"` — file has tech debt
- `"⚠ CVE-XXXX (high)"` — file imports a vulnerable package
- `community: <name>` — which feature cluster this file belongs to

Always read and surface these annotations to the user before editing.

---

## New MCP Tools (Enterprise-only)

| Tool | Description |
|---|---|
| `graph_watch(action)` | Start/stop/status live file watcher |
| `graph_visualize(project_root)` | Generate graph.html + GRAPH_REPORT.md |
| `graph_project_intel()` | Full intelligence summary |
| `graph_api_surface(prefix?)` | List HTTP/GraphQL routes |
| `graph_tech_debt(file_prefix?)` | List TODO/FIXME/HACK entries |
| `graph_pr_triage()` | Ranked PRs with CI state + conflict risk |

---

## Rules

- `graph_continue` MUST be called before any other tool. No exceptions.
- Never exceed 2 greps when `confidence=high`.
- After edits, call `graph_register_edit(files: ["path/to/file"])` — always `files` (plural, array).
- Use `file::symbol` notation for symbol-level edits.
- Do NOT do broad recursive exploration.
- Do NOT call `graph_retrieve` more than once per turn.
- Do NOT write to `context-store.json`, `project_intel.json`, or any `*.json` graph files directly — always use MCP tools.
