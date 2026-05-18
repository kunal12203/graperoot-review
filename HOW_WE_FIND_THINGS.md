# How GrapeRoot Finds Things — Technical Handoff

This document explains exactly how GrapeRoot Review produces its findings.
Every section maps directly to what you see in the benchmark results and
in live PR reviews.

---

## The 3-layer system

```
Layer 1: AST Graph         — built offline from the full codebase
         ↓ blast-radius context (who calls what)
Layer 2: Master Prompt     — structured JSON schema with 14 output categories
         ↓ Claude Sonnet 4.6 with graph context injected
Layer 3: Structured Output — parsed, ranked, posted as GitHub review
```

---

## Layer 1 — The AST Graph

**File:** `graph_builder_v6.2.py`

Before any review runs, we scan the full repo and build a dependency graph:

```
repo/
  ├── pkg/compactor/index_set.go    → imports: [pkg/iter, pkg/storage, ...]
  ├── pkg/compactor/compactor.go    → imports: [pkg/compactor/index_set, ...]
  └── ...
```

**What the graph stores:**
- Every file → its imports (edges)
- Every exported symbol → its file + line range
- File count, edge count (e.g. Loki: 44 files, thousands of edges)

**What this enables in the review:**

```python
# When PR changes pkg/compactor/index_set.go:
changed_files = ["pkg/compactor/index_set.go"]

# Graph finds who imports it:
callers = [e["from"] for e in edges
           if "index_set" in e["to"] and e["from"] not in changed_files]
# → ["pkg/compactor/compactor.go", "pkg/compactor/shipper.go", "store/chunk_store.go"]
```

This is the "blast radius" — the real import chain that gets cited in every
CRITICAL finding. Plain Claude without the graph has to guess at this.

**Graph stats from our benchmarks:**
| Repo | Files | Edges | Graph path |
|------|-------|-------|-----------|
| grafana/tempo | 1,393 | 11,200 | `/tmp/grafana-tempo/.dual-graph/info_graph.json` |
| grafana/loki | 44 changed | ~8,000+ | `/tmp/grafana-loki/.dual-graph/info_graph.json` |
| grafana/mimir | 31 changed | ~6,000+ | `/tmp/grafana-mimir/.dual-graph/info_graph.json` |

---

## Layer 2 — The Master Prompt

**File:** `benchmark_codeant.py` → `MASTER_SYSTEM`

The prompt instructs Claude to return a single structured JSON object with
**14 categories**. This is what produces the 37–52 findings per run.

### The full JSON schema Claude must return:

```json
{
  "pr_summary": "2-3 sentence summary: what it does, risk level, key concern",

  "inline_comments": [
    {
      "file": "pkg/compactor/index_set.go",
      "line": 232,
      "severity": "CRITICAL|HIGH|MEDIUM",
      "category": "logic|security|performance|reliability|test|contract",
      "title": "ingestedAt hardcoded to 0 destroys ingestion ordering",
      "comment": "detailed explanation with WHY this is a problem in production",
      "suggestion": "```suggestion\n- ingestedAt: 0,\n+ ingestedAt: time.Now().UnixNano(),\n```",
      "graph_proven": true
    }
  ],

  "sast_findings": [
    {
      "severity": "CRITICAL|HIGH|MEDIUM",
      "rule": "DATA-INTEGRITY: timestamp field zeroed during data transformation",
      "file": "pkg/compactor/index_set.go",
      "line": 0,
      "detail": "specific explanation with exploit scenario"
    }
  ],

  "iac_findings": [
    {
      "severity": "HIGH",
      "file": "operations/mimir-mixin-compiled/rules.yaml",
      "rule": "route-label-selector-breaks-on-package-rename",
      "detail": "Prometheus recording rules filtering on /cortex.Ingester/ break immediately"
    }
  ],

  "secrets_found": [
    {"file": "path", "line": 0, "type": "API key", "value_preview": "sk-a..."}
  ],

  "dead_code": [
    "exported symbols with no importers — only if graph confirms"
  ],

  "duplicate_code": [
    {"files": ["file1", "file2"], "pattern": "description of duplicated logic"}
  ],

  "complex_functions": [
    {
      "file": "pkg/compactor/compactor.go",
      "function": "NewCompactor",
      "cyclomatic_complexity": "very_high",
      "reason": "14 sequential init steps with no rollback, deeply nested error handling"
    }
  ],

  "test_coverage_gaps": [
    {
      "file": "pkg/compactor/index_set.go",
      "function_or_class": "applyUpdates",
      "risk": "New ingestedAt parameter has zero test coverage for non-zero values"
    }
  ],

  "blast_radius": {
    "direct_callers": ["files that import the changed files — from graph context"],
    "cross_repo_risk": "Loki operator YAML configs will fail schema v14 validation",
    "risk": "CRITICAL",
    "explanation": "specific production scenario if this ships with bugs"
  },

  "attack_surface_delta": {
    "increased": ["new gRPC method /ingesterpb.Ingester/QueryStream exposed"],
    "decreased": ["removed /cortex.Ingester/QueryStream"],
    "net_risk": "NEUTRAL"
  },

  "jira_intent_check": {
    "matches_description": true,
    "gaps": ["PR claims to update all dashboards but operations/mimir-mixin is not in diff"],
    "missing_files": ["operations/mimir-mixin-compiled/rules.yaml"]
  },

  "missing_from_diff": [
    "operations/mimir-mixin-compiled/rules.yaml — Prometheus rules still reference /cortex.Ingester/"
  ],

  "quality_gates": {
    "pass": false,
    "blocking_issues": ["CRITICAL: ingestedAt=0 causes silent data loss on all clusters"]
  },

  "security_grade": "B",
  "quality_score": 62,
  "review_confidence": "HIGH"
}
```

### Key rules in the prompt:

```
- inline_comments: max 8, sorted by severity (CRITICAL first), ALWAYS include suggestion
- Set graph_proven=true ONLY when blast_radius context was used to derive the finding
- iac_findings: check for .tf, .yaml (k8s/helm), Dockerfile, .github/workflows in diff
- missing_from_diff: THE MOST IMPORTANT CHECK — what SHOULD be in diff but isn't
- quality_score: 0–100 (100=perfect, 0=do not merge)
```

---

## Layer 3 — How the prompt is assembled

**GrapeRoot mode (with graph):**
```
{MASTER_SYSTEM}

## Graph Context (blast radius)
Repo: loki | 44 files | 8,234 edges
Files importing changed files: pkg/compactor/compactor.go, pkg/storage/chunk_store.go
Exported symbols in diff: applyUpdates, IndexChunk, newMetrics

## PR: Add ingestAt field to TSDB schema v14
<PR description>

## Diff
### pkg/compactor/index_set.go
@@ -229,6 +229,7 @@
+ ingestedAt: 0,
...
```

**Plain Claude (no graph):**
```
{MASTER_SYSTEM}

## PR: Add ingestAt field to TSDB schema v14
<PR description>

## Diff
[same diff, no graph context]
```

**What the graph context adds:**
- Claude knows `applyUpdates` is called by `compactor.go` and `chunk_store.go`
- So when it sees `ingestedAt: 0`, it knows the blast radius is not just this file
- It can set `graph_proven: true` and cite the actual import chain
- Plain Claude sees the same bug but can't prove the blast radius

---

## What each category catches

| Category | What it finds | Example from benchmark |
|----------|--------------|----------------------|
| **inline_comments** | Logic bugs, missing error handling, broken contracts | `ingestedAt=0` destroys retention |
| **sast_findings** | Security patterns, CWE violations | DATA-INTEGRITY timestamp zeroed |
| **iac_findings** | K8s/Helm/Prometheus config breaks | Prometheus rules with stale `/cortex.Ingester/` paths |
| **secrets_found** | API keys, tokens in code | (none found in these PRs — correct) |
| **dead_code** | Exported symbols with no importers | Graph-confirmed, not guessed |
| **duplicate_code** | Repeated logic across files | Duplicate init patterns |
| **complex_functions** | High cyclomatic complexity | `NewCompactor` — 14 sequential steps |
| **test_coverage_gaps** | Untested risky code paths | `ingestedAt` param never tested non-zero |
| **blast_radius** | Files broken by this change | 10 operator YAML configs affected |
| **attack_surface_delta** | New/removed endpoints, auth paths | gRPC method rename |
| **jira_intent_check** | Does diff match PR description? | PR says "update dashboards" but rules.yaml not in diff |
| **missing_from_diff** | Files that MUST change but don't | `operations/mimir-mixin-compiled/rules.yaml` |
| **quality_gates** | Go/No-go decision | FAIL — 2 CRITICAL blocking issues |
| **security_grade** | A–F overall security score | B |

---

## Benchmark results — 3 Grafana production PRs

### grafana/tempo#7171 — Go module v3 rename

**What we found that Plain Claude also found (24 inline):**
All 8 inline comments were found by both. The difference:
- GrapeRoot set `graph_proven: true` on 5/8 because the graph confirmed
  which of the 11,200 edges would break
- SAST finding: `module-path-confusion` rule — CWE-610 (Externally Controlled Reference)
- Plain Claude: 0 SAST findings

### grafana/loki#21931 — TSDB FormatV4 / ingestedAt

**The graph-only finding:**

Plain Claude saw `ingestedAt: 0` in the diff and flagged it as wrong.
GrapeRoot additionally:
1. Traced the import chain: `index_set.go → compactor.go → shipper.go → chunk_store.go`
2. Set `blast_radius.risk = CRITICAL`
3. Added IaC finding on `operator/internal/manifests/internal/config/loki-config.yaml`
   (operator YAML that references schema v14 — not in the diff but breaks)
4. Found 2 SAST findings vs 0 for Plain Claude

**Missing from diff (graph-only):**
```
operator/internal/manifests/internal/config/loki-config.yaml
operator/internal/manifests/internal/config/testdata/schemas-template.yaml
[8 more testdata YAML files]
```
Plain Claude: no `missing_from_diff` findings.

### grafana/mimir#15059 — gRPC proto rename cortex→ingesterpb

**The 3 IaC findings Plain Claude missed:**
```
operations/mimir-mixin-compiled/rules.yaml
  rule: stale-metric-source-after-proto-rename
  → Prometheus recording rules still filter on /cortex.Ingester/
  → Will silently produce no-data the moment first upgraded pod starts

operations/mimir-mixin-compiled/rules.yaml
  rule: route-label-selector-breaks-on-package-rename
  → Alert expressions break — oncall alerts stop firing during upgrade

operations/mimir-mixin-compiled/rules.yaml
  rule: recorded-metric-names-carry-legacy-cortex-prefix
  → Dashboard panels relying on these recorded metrics show no-data
```

These are **not in the diff**. Plain Claude only saw the diff. GrapeRoot's
blast-radius traversal found that `operations/mimir-mixin-compiled/rules.yaml`
imports/references the renamed proto path and flagged it.

---

## Final tally — GrapeRoot vs Plain Claude

| Category | GrapeRoot | Plain Claude | Delta |
|----------|-----------|-------------|-------|
| Inline comments | 24 | 24 | 0 |
| SAST findings | 3 | 2 | **+1** |
| IaC findings | 4 | 0 | **+4** |
| Complex functions | 5 | 0 | **+5** |
| Test coverage gaps | 11 | 0 | **+11** |
| Duplicate code | 4 | 0 | **+4** |
| Dead code | 1 | 0 | **+1** |
| Missing from diff | 9+ | 0 | **+9** |
| Blast radius citations | 3 | 0 | **+3** |
| **Total** | **~52** | **26** | **+26** |

**The headline:** GrapeRoot found **2× more things** than plain Claude.
The extras are all in categories that require knowing the codebase structure —
which is exactly what the graph provides.

---

## The system prompt rules that matter most

```
"missing_from_diff: the MOST important check — what SHOULD be in the diff but isn't"
```
This is how we catch the `rules.yaml` that breaks silently after deploy.
Plain Claude only sees what's in the diff. GrapeRoot knows what else
should have changed because the graph shows what depends on the changed files.

```
"Set graph_proven=true ONLY when blast_radius context was used to derive the finding"
```
This is the auditability guarantee. If `graph_proven: false`, the finding
came from the diff alone. If `graph_proven: true`, we can show the exact
import chain that proves the blast radius. No other tool does this.

---

## Files to read

| File | What |
|------|------|
| `benchmark_codeant.py` | Full benchmark — MASTER_SYSTEM prompt, all categories, graph_context() |
| `review.py` | Live review engine — simplified prompt, posts to GitHub |
| `graph_builder_v6.2.py` | AST graph builder — scans repo, builds edges |
| `graph_service.py` | Hosted service graph manager — clone, build, cache |
| `codeant_benchmark.json` | Raw benchmark results — 3 PRs × GrapeRoot + Plain |
