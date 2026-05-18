# GrapeRoot Review Bot — Strategy & Build Context

**Purpose of this document:** Drop-in context for Claude Code (or any AI coding agent) when working on GrapeRoot's PR review bot. Contains the competitive landscape, positioning thesis, technical architecture decisions, and execution plan. Read top-to-bottom once, then reference sections as needed.

**Author/Owner:** KK (solo founder, GrapeRoot — graperoot.dev)
**Last updated:** May 2026

---

## 0. TL;DR for the Agent

You are helping build a PR review bot inside GrapeRoot, a local-first MCP server that already maintains an AST + dependency graph of the user's codebase. The review bot is a wedge product — a feature of the larger GrapeRoot offering — designed to compete in the AI code review market dominated by CodeRabbit, Greptile, and CodeAnt.

**Single most important strategic principle:** Do NOT try to out-feature the incumbents. Pick one wedge — **local-first, structurally-grounded review** — and refuse to dilute it.

**The user's code never leaves their machine.** Every architectural decision must serve this.

---

## 1. The Market (As of May 2026)

### 1.1 The category landscape

The "AI code review" market is unsettled but crowded. Three positions are taken:

| Position | Owner | Their pitch | Their weakness |
|---|---|---|---|
| Deepest review | Greptile | "Full-codebase graph catches 82% of bugs" | High false-positive rate, GitHub-heavy, $30/seat |
| Easiest review | CodeRabbit | "2M repos, works on every platform, low noise" | Diff-only, scored 1/5 on completeness in independent benchmarks |
| Bundle play | CodeAnt | "Review + SAST + secrets + IaC + DORA in one tool" | Bundle is a stack of mid-tier components, not best-in-class |

Independent benchmark (Martian, Feb 2026, 300K real PRs):
- All tools sit at 50-60% effectiveness
- False positives are the #1 complaint across every tool
- No tool has won on signal-to-noise yet

**The unfilled job-to-be-done:** Teams that need PR review but **cannot send code to a cloud LLM**. This includes Indian financial services under DPDP Act 2023, EU companies under strict GDPR, healthcare under HIPAA, defence contractors, government, German Mittelstand. None of CodeRabbit / Greptile / CodeAnt / BugBot serves them well because their architectures all assume cloud inference.

### 1.2 Why GrapeRoot can win this slice

- GrapeRoot is **already local-first** by architecture. The MCP server runs on the dev's machine.
- The **AST + dependency graph already exists** for the token-cost-savings product. Re-using it for review is near-zero marginal engineering cost.
- Incumbents **cannot follow without breaking their own business model**. Cloud inference is their unit economics; local inference is yours.
- Your existing distribution (Claude Code plugin, Cursor integration via MCP) gets reused.

### 1.3 What we will NOT compete on

- Per-seat per-PR feature parity with CodeRabbit (they have 2 years and $30M+ of polish)
- Bundled SAST (Semgrep is free, Snyk is the gorilla)
- Auto-fix (trust deficit kills adoption when AI applies its own code)
- PR summaries / docstrings (table stakes, not differentiation)
- 30+ language support out of the gate (do 3 perfectly first)

---

## 2. Positioning

### 2.1 The category we are creating

We are NOT entering "AI code review" as a category. We are creating **"Local Code Intelligence"** (or "Sovereign Code Review" — pick one and own it).

The frame:
> CodeRabbit, Greptile, and CodeAnt are cloud reviewers. They send your code to OpenAI, Anthropic, or their own servers. GrapeRoot is the first reviewer that runs entirely on your machine — your code never leaves the perimeter, your review cost is fixed, and the dependency graph is always fresh because it lives next to your code, not in a stale cloud index.

### 2.2 The three-claim positioning

Every public asset (landing page, README, launch post, ads) should hit these three claims in this order:

1. **Sovereignty.** Your code never leaves your machine. No OpenAI, no Anthropic-cloud, no vendor seeing your IP.
2. **Determinism.** Every finding carries one of three provenance tags: `[AST-FACT]` (zero false positives by construction), `[AST-HEURISTIC]` (graph-derived, known FP modes disclosed), or `[LLM-HEURISTIC]` (model guess, explicitly flagged). Trust is built by showing your work.
3. **Economics.** No per-seat pricing. No per-PR inference cost. Pay once for GrapeRoot; review is free.

### 2.3 Tone

- Anti-marketing. Sound like a senior engineer who built this for themselves and is sharing it.
- Specific, not superlative. "Catches breaking changes across 14 call sites" beats "world-class bug detection."
- Show benchmarks honestly. If Greptile catches more bugs in one category, say so — and explain why our trade-off is worth it for the people we serve.

---

## 3. The Product — What We Are Actually Building

### 3.1 The three-tier output model (the core feature)

Every comment GrapeRoot Review posts on a PR carries exactly one provenance tag. No exceptions.

**Tier 1: `[AST-FACT: <check-name>]`** — Derived purely from the dependency graph by deterministic traversal. Zero false positives by construction.

Contract: if any input to the check is uncertain — dynamic dispatch detected, parse failure in a caller, missing or incomplete graph edge, import aliasing that cannot be statically resolved — the check returns **no finding** rather than an uncertain one. A silent miss is acceptable; an incorrect `[AST-FACT]` is not.

Examples of valid [AST-FACT] findings:
- "This signature change has 11 static callers: [list with file:line]" ← only after dynamic dispatch is ruled out per caller
- "This new function has zero callers in the static call graph" ← only if graph coverage for this module is confirmed complete

Examples that are NOT [AST-FACT]:
- "This function probably has no callers" — the word "probably" disqualifies it
- Dead-code in Python/TypeScript — dynamic import, `__all__`, decorators, and monkey patching create too many undetectable call paths
- Untested public API in Python/TypeScript — test detection relies on file-name heuristics, not CFG

**Tier 2: `[AST-HEURISTIC: <check-name>]`** — Derived from the graph but with known false-positive modes that cannot be eliminated by construction. The graph provides the signal; uncertainty is inherent in the check.

Examples:
- "This module now imports X for the first time" — coupling drift. FP mode: transitive re-export may have existed before.
- "These files import from the changed module and may be affected" — blast radius when dynamic dispatch cannot be confirmed absent per caller.
- "This exported symbol has no references in `*test*` files" — FP mode: tests may be in a different repo or use dynamic test generation.

**Tier 3: `[LLM-HEURISTIC: <check-name>]`** — Model-generated. Explicitly marked as a guess. Can be wrong. Must still cite the exact code from context to be posted.

Examples:
- "This loop may be O(n²) on input size N"
- "make_null_session body never references app — asymmetric with sansio siblings"
- "accessed default changed False→True: behavioral change for callers"

Competitors blend all three. GrapeRoot separates them. The provenance tag is the single most defensible UX feature — it solves the #1 complaint across the entire category (false positives) by making uncertainty explicit rather than hiding it.

### 3.2 The 10 deterministic graph checks (v1 scope)

These are the launch features. All ten can be answered from the AST + dep graph **without an LLM call**. They are the moat.

1. **Breaking-change blast radius.** Signature changed → enumerate every caller, ranked by call-graph depth.
2. **Dead code on PR.** New function/class with zero callers from any entry point in the graph.
3. **Untested public API.** Exported function with no references in `*test*` files.
4. **Test gap on changed branch.** Function modified, but its existing test does not exercise the new branch (CFG diff).
5. **Coupling drift.** Module A now imports from Module B for the first time. Flag with the boundary policy.
6. **Layer violation.** Controller code now calls DB directly, bypassing service layer present elsewhere.
7. **Convention break.** New code uses a naming/style pattern that contradicts ≥80% of the same construct elsewhere.
8. **Duplicate logic.** New function is semantically near-identical to an existing one (AST similarity above threshold).
9. **Unsafe pattern reintroduction.** This PR adds a pattern that was previously refactored out of the codebase.
10. **Cycle introduction.** This change creates a new cycle in the module dependency graph.

Each of these returns exact, citeable evidence. No "we think." Only "we know, here is the proof."

### 3.3 The LLM tier (v1.5 scope)

Once the deterministic tier is solid, layer in LLM checks for:
- Logic bugs in changed functions (off-by-one, null handling, error swallowing)
- Performance heuristics (algorithmic complexity, N+1 patterns)
- Naming/intent mismatches

These are LATE additions. Do not let them slow the deterministic tier shipping.

### 3.4 What we explicitly do NOT build in v1

- PR summaries (CodeRabbit owns this; it's table stakes)
- Mermaid diagrams in comments (cosmetic)
- Chat interface in the PR (Greptile owns it)
- Auto-fix / patch suggestions (trust risk)
- SAST scanning (Semgrep does this free)
- Secret scanning (Trufflehog does this free)
- Quality gates / DORA dashboards (CodeAnt's territory)
- Self-hosted SaaS dashboard (we ARE local-first; no dashboard to host)
- Custom rules engine (v2 feature)

---

## 4. Technical Architecture

### 4.1 Where the review runs

**Default mode (Local Pro):**
- GrapeRoot MCP server runs on dev's machine
- AST + dep graph is already built and cached
- PR triggered locally (pre-push hook, IDE command, CLI, or git hook)
- Inference uses the dev's existing LLM credentials via MCP, OR a local model
- Output: markdown comment posted to GitHub via the user's gh CLI token

**Optional mode (Team Hosted — what review.graperoot.dev is):**
- Thin GitHub App that triggers a runner
- Runner can be self-hosted in the team's VPC OR opt-in cloud
- The graph is built in the runner, not on Greptile's servers
- This mode is opt-in and clearly marked — sovereignty is the default

**Critical commitment:** The graph never leaves the user's infrastructure in default mode.

### 4.2 Reusing the existing GrapeRoot graph

GrapeRoot already has:
- AST parser per language
- Dependency graph (file → file, function → function, module → module)
- Token-cost-saving retrieval layer

New code needed:
- PR-diff parser → maps changed lines to AST nodes
- Blast-radius walker on the graph
- Comment formatter (markdown with `[AST-FACT]` / `[LLM-HEURISTIC]` tags)
- GitHub/GitLab PR API integration
- Hook into MCP so coding agents can invoke "review this PR" as a tool

### 4.3 Language support priority

1. **Python** — strongest AST parser, ML/data audience
2. **TypeScript / JavaScript** — biggest market
3. **Go** — devops/infra crowd, clean ASTs

Do NOT add a language until the previous one passes a real-world test on 100+ PRs.

### 4.4 Performance budgets

- **Deterministic tier:** < 5 seconds on a 500-LOC diff in a 100K-LOC repo
- **LLM tier:** < 30 seconds end-to-end
- **First-time index build:** < 2 minutes for 100K LOC

---

## 5. Distribution Plan

### 5.1 The wedge motion

The review bot is a **free feature** of GrapeRoot in v1. Goal: user acquisition for the core product.

### 5.2 Launch sequence

**Week 1-2:** `graperoot review` CLI command on current branch diff
**Week 3-4:** Claude Code plugin + Cursor MCP integration
**Week 5-8:** GitHub App (opt-in hosted runner)

### 5.3 Content plan

1. GrapeRoot vs CodeRabbit
2. GrapeRoot vs Greptile
3. GrapeRoot vs CodeAnt
4. The local-first code review manifesto
5. Why your PR reviewer should not see your code

---

## 6. Pricing

- **v1 (months 0-6):** Free. Goal is install base.
- **v2 (months 6-12):** GrapeRoot Pro — $9/dev/month or $99/year
- **v2.5 (year 1+):** GrapeRoot Team — $19-29/dev/month
- **Never charge per-PR.** Flat pricing is the structural advantage.
- **Always have a free tier for OSS and solo devs.**

---

## 7. Risks & Pre-Mortems

1. **Greptile clones provenance tagging.** Mitigation: local-first is the deeper moat; ship local-first story first.
2. **CodeRabbit launches a local mode.** Probability low. Mitigation: speed.
3. **Multi-language AST harder than expected.** Mitigation: ship 3 languages flawlessly before adding more.
4. **Solo founder burnout.** The review bot is a FEATURE of GrapeRoot, not a separate product.
5. **Cursor/Anthropic ships native PR review.** They did (March 2026, cloud-first). Same local-first defense.
6. **Local-first audience too small.** DPDP enforcement (May 2027), EU AI Act making it grow.

---

## 8. Implementation Order

### Phase 1: CLI MVP (weeks 1-3)
- [ ] `graperoot review` command
- [ ] Reads current branch diff vs origin/main
- [ ] Maps changed lines to AST nodes
- [ ] 3 deterministic checks: blast-radius, dead-code, untested-public-API
- [ ] Tag every finding `[AST-FACT: <check-name>]`
- [ ] Python support only

### Phase 2: Remaining deterministic checks (weeks 3-5)
- [ ] Test-gap-on-branch, coupling drift, layer violation
- [ ] Convention break, duplicate logic, unsafe pattern, cycle introduction
- [ ] TypeScript support

### Phase 3: LLM tier (weeks 5-7)
- [ ] Local model inference (Ollama)
- [ ] Optional BYOK remote inference
- [ ] Logic-bug, performance, naming heuristics
- [ ] Tag all `[LLM-HEURISTIC: <check-name>]`
- [ ] Hard separation: deterministic tier never depends on LLM

### Phase 4: Integration (weeks 7-9)
- [ ] MCP tool: `review_pr` callable from Claude Code, Cursor
- [ ] GitHub comment posting via user's gh CLI
- [ ] Pre-push git hook
- [ ] Go support

### Phase 5: GitHub App / hosted (weeks 9-12) ← WHERE WE ARE NOW
- [ ] Thin GitHub App (built ✓)
- [ ] Self-hosted runner (Docker image)
- [ ] Optional managed runner (cloud, opt-in) ← review.graperoot.dev
- [ ] Team dashboard ← dashboard at review.graperoot.dev

### Phase 6: Polish + benchmark (weeks 12-14)
- [ ] Submit to Martian Code Review Bench
- [ ] Comparison landing pages
- [ ] Show HN launch

---

## 9. Coding Conventions

- **Match existing GrapeRoot codebase style.**
- **Prefer deterministic over LLM-based logic wherever possible.**
- **No silent failures.** Log clearly on the user's machine.
- **No telemetry by default.** Opt-in only.
- **No external network calls in the deterministic tier.** Airgapped must work.
- **Every comment posted to a PR carries exactly one tier tag.** Format: `[AST-FACT: <check-name>]`, `[AST-HEURISTIC: <check-name>]`, or `[LLM-HEURISTIC: <check-name>]`. A comment with no tag or an ambiguous tag must not be posted. No exceptions.
- **Error messages help, not embarrass.**
- **Tests live next to code.** `checks/blast_radius.py` → `checks/blast_radius_test.py`
- **Every check has 3 tests:** happy path, edge case, false-positive-prevention.

---

## 10. The North Star

Every decision routes through:

> Does this make GrapeRoot more obviously the choice for someone who cannot send their code to a cloud LLM?

If yes, do it. If no, don't.

If we ever ship a feature that requires sending the user's code to Anthropic, OpenAI, or any other cloud LLM **by default**, we have lost the war.

---

## Appendix A: Competitor Quick Reference

**CodeRabbit** — Diff-only, cloud, $15-30/dev. 1/5 completeness. Attack: "too niche." Response: "popular = cloud; ours = never cloud."

**Greptile** — Graph-based, cloud, $30/seat. Noisy. Attack: "we have the graph too." Response: "theirs is in their cloud, ours is on your laptop."

**CodeAnt** — Bundle, cloud, $24-40/dev. Mediocre across board. Attack: "we do more." Response: "we do one thing locally, better."

**Cursor BugBot / Copilot / Claude Code Review** — IDE-locked, cloud. Attack: native integration. Response: "same model that wrote the code is reviewing it — we're the independent reviewer."

**Qodo Merge (PR-Agent)** — Open source, self-hostable. Attack: also open source. Response: "we open-source the graph and deterministic checks; we charge for the team layer."

---

## Appendix B: Glossary

- **AST** — Abstract Syntax Tree
- **CFG** — Control Flow Graph
- **MCP** — Model Context Protocol (Anthropic's standard)
- **Blast radius** — set of code locations affected by a change
- **Provenance** — where a finding came from (AST vs LLM)
- **DPDP** — Digital Personal Data Protection Act 2023 (India), enforcement May 2027
- **Sovereignty** — code does not leave user's infrastructure
