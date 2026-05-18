# Session Context

## Current Task
Building GrapeRoot Review to beat Greptile — 8-check parallel pipeline with static detectors + graph semantic pre-injection, benchmarked against real PRs.

## Key Decisions
- **Static detectors always fire** (n_plus_one, falsy_traps, default_change, docstring, rust_bounds) — LLM checks (arch on o1, security on o1) are supplemental
- **gpt-4o for 6 pattern checks, o1 only for arch+security** — cuts review time from 120s → 66s
- **Graph semantic pre-injection**: sansio gold standards (`sansio/app.py`, `sansio/blueprints.py`) injected as context for arch check; Rust support added to `graph_builder_v6.2.py`

## Next Steps
- Fix remaining non-determinism: add `detect_orphaned_methods()` static detector (parse BASE vs HEAD defs, flag methods absent from new sansio module) + `temperature=0` on gpt-4o calls
- Cut LLM checks to 2 (arch + security), stub the rest, fix tagging to exactly one of `[AST-FACT]` / `[AST-HEURISTIC]` / `[LLM-HEURISTIC]` per finding
- Build snapshot test suite (10 pinned PRs, pytest fails on deviation) then run fresh 10-PR benchmark — **only then claim ready to beat Greptile**
