# Finding-Producing Code Path Audit

Maps every distinct path that produces a finding (something posted to a GitHub PR review)
to the tier it *actually* belongs to under the three-tier contract in STRATEGY.md §3.1.

Prepared against: `backend/webhook.py` + `backend/review.py` as of commit 332b26e.

---

## webhook.py — `_run_review()` graph paths

### 1. `graph_impact()` blast-radius traversal
**File:** `webhook.py:447`
**What it does:** Calls `graph_service.graph_impact(owner, repo, changed_files)`, which walks
the cached AST edge list to find files connected (directly) to the changed files.
Returns `affected_files` and a human-readable `summary` string.

**Current use:** The summary string is passed to `review.py` as `GR_GRAPH_CONTEXT` env var,
where it becomes the `impact_summary` context injected into the LLM prompt. It is **not**
posted to GitHub as a finding directly — it is raw context for the LLM agents.

**True tier:** `[AST-HEURISTIC: blast-radius]` — the edge list is static, but we do not
currently filter callers for dynamic dispatch patterns (getattr, importlib, etc.), and we do
not verify graph coverage completeness before reporting. Would become `[AST-FACT]` after
adding the uncertainty invariant (see implementation plan below).

**FP modes:**
- Dynamic dispatch (`getattr(obj, method_name)`) creates real callers invisible to the graph
- Partial graph (parse failure on one file) silently omits edges
- Import aliasing (`from sessions import SessionInterface as SI`) may not resolve correctly

---

### 2. `_gh_file_content()` for blast-radius affected files
**File:** `webhook.py:460`
**What it does:** Fetches the actual source of each blast-radius file from the GitHub API
and appends it to `GR_GRAPH_CONTEXT`. Provides file content for LLM context, not a finding.

**True tier:** Not a finding — it is context assembly for the LLM tier. No tier tag needed.

---

## review.py — context assembly (not findings, but preconditions)

### 3. `build_file_context()` — base + head file fetch
**File:** `review.py:112`
**What it does:** Fetches full content of changed files at `base_sha` and `head_sha` via the
GitHub contents API. Also calls `gh_search_imports()` to find importing files.
All output becomes `file_context` injected into LLM prompt.

**True tier:** Not a finding. Context only.

### 4. `gh_search_imports()` — GitHub code search for importers
**File:** `review.py:93`
**What it does:** GitHub `/search/code` API query for files that reference the stem of a
changed filename. Returns up to 5 candidate importer paths.

**Why not a finding:** GitHub code search is keyword-based, not semantic. Returns false
matches (comments, string literals, unrelated identifiers). This is weaker than the graph.

**True tier if used as a finding:** Would be `[LLM-HEURISTIC]` at best.
Currently: context only.

---

## review.py — deterministic pre-checks (currently untagged)

### 5. Missing-from-diff via regex over blast-radius string
**File:** `review.py` (inside `main()`, `det_missing` block)
**What it does:** Runs `re.findall(r'[\w./\-]+\.(?:py|ts|...)`, impact_summary)` to extract
file paths from the blast-radius summary text, then flags any not in `changed_files`.

**True tier:** `[LLM-HEURISTIC]` at best — the input is a natural-language string generated
by `graph_impact()`, not a structured edge list. Regex over prose is not graph traversal.

**FP modes:**
- Paths mentioned in summary prose that aren't actual importers
- Paths already handled by a re-export that IS in the diff
- The summary string format is not guaranteed stable

**Action:** Remove from deterministic pre-checks. The graph-based missing-from-diff belongs
in the graph tier with proper edge traversal, not string parsing.

### 6. Secrets scan via regex over diff +lines
**File:** `review.py` (inside `main()`, `det_secrets` block)
**What it does:** Applies 5 regex patterns to lines beginning with `+` in the diff text.
Patterns: generic `(api_key|secret|password|token)`, OpenAI `sk-`, GitHub `ghp_`, AWS `AKIA`,
PEM header.

**True tier:** `[LLM-HEURISTIC]` — regex pattern matching is not semantic and has well-known
FP modes. This is the same category of check that Trufflehog already does better.

**FP modes:**
- Test fixture values (fake keys used in unit tests)
- Example keys in README or comments included in the diff
- Variable names containing "token" with non-secret values

**Action:** Remove from pre-checks in this codebase. Pair with Trufflehog externally per
STRATEGY.md §3.4 ("Secret scanning — Trufflehog does this free").

---

## review.py — LLM agents (all `[LLM-HEURISTIC]`)

All three agents receive the full context (diff, base/head file content, graph context)
and produce findings via model inference. Every output is `[LLM-HEURISTIC]` regardless of
whether it references graph context, because the reasoning step is non-deterministic.

### 7. Architecture agent (`ARCH_PROMPT`) — inline_comments, missing_from_diff, jira_intent_check
**File:** `review.py:365`
**Produces:**
- `inline_comments` — refactor completeness, asymmetry, orphaned methods → `[LLM-HEURISTIC: arch-check]`
- `missing_from_diff` — LLM infers what should be in the diff → `[LLM-HEURISTIC: arch-check]`
- `jira_intent_check` — compares PR description to diff → `[LLM-HEURISTIC: arch-check]`

**Note on `graph_proven=true`:** The field exists in the schema but `graph_proven=true` set
by the model is unverified. The model may set it incorrectly. Currently treated as metadata
only, not used to upgrade tier.

### 8. Security agent (`SEC_PROMPT`) — inline_comments, sast_findings, secrets_found, attack_surface_delta
**File:** `review.py:394`
**Produces:**
- `inline_comments` — bugs, auth issues, data integrity → `[LLM-HEURISTIC: security-check]`
- `sast_findings` — security patterns cited by model → `[LLM-HEURISTIC: security-check]`
- `secrets_found` — model-identified secrets in context → `[LLM-HEURISTIC: security-check]`
- `attack_surface_delta` — model assessment of surface change → `[LLM-HEURISTIC: security-check]`

### 9. Quality agent (`QUAL_PROMPT`) — inline_comments, test_coverage_gaps, dead_code, complex_functions, duplicate_code
**File:** `review.py:415`
**Produces:**
- `inline_comments` — quality issues → `[LLM-HEURISTIC: quality-check]`
- `test_coverage_gaps` — model guesses at test coverage → `[LLM-HEURISTIC: quality-check]`
- `dead_code` — model uses graph context but reasoning is model-inferred → `[LLM-HEURISTIC: quality-check]`
  (Note: model is instructed "only if graph context confirms" but there is no code-level enforcement)
- `complex_functions` — heuristic complexity → `[LLM-HEURISTIC: quality-check]`
- `duplicate_code` — semantic similarity → `[LLM-HEURISTIC: quality-check]`

### 10. Hallucination verifier (`_verify_findings`)
**File:** `review.py:459`
**What it does:** Post-processes LLM output. Drops findings whose backtick-quoted code
snippets containing programming syntax are absent from the provided context string.

**Tier:** Not a finding producer — it is a filter on LLM findings. Does not change tier.

---

## Summary table

| # | Code path | File | Produces | Current tier | Should be |
|---|-----------|------|----------|--------------|-----------|
| 1 | `graph_impact()` traversal | webhook.py | blast-radius context for LLM | (context) | — if used as finding: `[AST-HEURISTIC]` |
| 2 | `_gh_file_content()` blast-radius | webhook.py | file content for LLM | (context) | — |
| 3 | `build_file_context()` | review.py | base/head file content for LLM | (context) | — |
| 4 | `gh_search_imports()` | review.py | candidate importer paths for LLM | (context) | — |
| 5 | missing-from-diff regex | review.py | `missing_from_diff` list | untagged | `[LLM-HEURISTIC]` or remove |
| 6 | secrets regex | review.py | `secrets_found` entries | untagged | remove (use Trufflehog) |
| 7 | arch agent | review.py | inline_comments, missing_from_diff | untagged | `[LLM-HEURISTIC: arch-check]` |
| 8 | sec agent | review.py | inline_comments, sast_findings, secrets | untagged | `[LLM-HEURISTIC: security-check]` |
| 9 | qual agent | review.py | inline_comments, test gaps, dead code | untagged | `[LLM-HEURISTIC: quality-check]` |
| 10 | hallucination verifier | review.py | (filter, not producer) | — | — |

**No path currently produces an `[AST-FACT]` finding.** The blast-radius traversal (path 1)
is the only candidate, and it is missing the uncertainty invariant needed to qualify.

---

## What would be needed to add `[AST-FACT: blast-radius]`

1. Run `graph_impact()` to get `affected_files` (already done).
2. For each affected file, fetch its source and scan for dynamic dispatch patterns
   (`getattr(`, `__import__(`, `importlib`, `eval(`). If any found → skip that caller.
3. Verify graph edge exists (not inferred) by checking the graph's edge list directly.
4. Verify graph was built from a complete parse (no `parse_error` nodes in graph JSON).
5. If all callers pass → post as `[AST-FACT: blast-radius]`.
6. If zero callers survive the filter → post nothing (invariant honored).
7. If graph not built → post nothing.

Only paths 5 and 6 produce output. Path 7 ensures no uncertain finding ships.
