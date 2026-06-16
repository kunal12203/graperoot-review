#!/usr/bin/env python3
"""MCP gateway exposing dual-graph retrieval/read/impact tools.

v7.6 — Session-aware tool enforcement.
  * All 7 context tools (graph_read, fallback_rg, graph_impact, graph_neighbors,
    graph_dead_exports, graph_find_cycles, graph_grep_all) now hard-gate behind
    graph_continue — return {ok:false, action_required:graph_continue} if skipped.
  * Session-aware TURN_STATE: each mcp-session-id gets its own persistent state
    so graph_continue_called=True survives across sequential tool calls within
    one Claude conversation (stateless_http was resetting state per-request).
  * Session middleware: saves/restores TURN_STATE per session on every request.
  * Session isolation: User A's graph_continue does not unlock tools for User B.
  * FastMCP instructions= field embeds the policy — no CLAUDE.md dependency.

v7.5 — Fixes v7.4 regressions + further cost trim.
  * reuse_gate OFF by default — blocked 34% of reads in v7.4 (each wasted an error response
    that still entered session cache).
  * Confidence reverted to top_score ≥ 10 for `high` (v7.4's ≥15 only shifted 31% to medium
    and didn't actually help P13/P17/P18). KEEP the safety-net grep at `high` — that's v7.4's
    actual improvement.
  * Restore `task_type` in graph_continue response — 1 word of scaffolding; the v7.4 strip
    caused a 13-pt drop on P1 with no other explanation.
  * Compressed tool docstrings — they sit in every turn's system prompt.

v7.4 — Lean context architecture. Targets session-cache bloat (87% of v7.3 cost).
  * Lean graph_continue: drop query echo, intent, task_type, memories, coverage_score,
    model_hint, and re-broadcast hints; keep only actionable fields every turn.
  * Cross-turn dedup: graph_read returns a 1-line pointer when the same file was already
    read earlier in the session (instead of re-sending cached content that re-bills every turn).
  * Priority-queue action log: importance = kind_weight × exp(-age) → evict least important,
    not just the oldest.
  * Confidence recalibration: `high` now requires top_score ≥ 15 AND ≥2 strong hits
    (v7.3's score ≥ 10 threshold caused P13/P17/P18 quality regressions).
  * Hint-once: exhaustive/behavioral/package hints emit on first graph_continue only.

v7.3 — symbol-level recommendations (file::symbol) to cut graph_read token cost 10-15x.
"""

import hashlib
import json
import logging
import math
import os
import posixpath
import re
import socket
import subprocess
import threading
import time
import urllib.request

# Suppress all INFO-level logs so the Claude CLI terminal stays clean
logging.basicConfig(level=logging.ERROR)
for _logger in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "anyio"):
    logging.getLogger(_logger).setLevel(logging.ERROR)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # noqa: BLE001
    FastMCP = None  # type: ignore[assignment]

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))

# v7.1: Use local graph_builder_v6.2 (has C/C++ support) instead of compiled .so
try:
    import importlib.util as _imputil
    _gb_spec = _imputil.spec_from_file_location(
        "graph_builder_v6_2",
        str(Path(__file__).resolve().parent / "graph_builder_v6.2.py"),
    )
    _gb_mod = _imputil.module_from_spec(_gb_spec)
    _gb_mod.__name__ = "graph_builder_v6_2"
    _sys.modules["graph_builder_v6_2"] = _gb_mod
    _gb_spec.loader.exec_module(_gb_mod)
    _gb_scan = _gb_mod.scan
except Exception:  # noqa: BLE001
    # Fall back to compiled module if local script fails
    try:
        from graperoot.graph_builder import scan as _gb_scan
    except Exception:  # noqa: BLE001
        _gb_scan = None  # type: ignore[assignment]

try:
    from graperoot.dg import retrieve as _dg_retrieve, classify_intent as _dg_classify_intent
except Exception:  # noqa: BLE001
    _dg_retrieve = None  # type: ignore[assignment]
    _dg_classify_intent = None  # type: ignore[assignment]


DG_BASE = os.environ.get("DG_BASE_URL", "http://127.0.0.1:8787")
DG_API_TOKEN = os.environ.get("DG_API_TOKEN", "").strip()
DG_DATA_DIR = Path(
    os.environ.get("DG_DATA_DIR", str(Path(__file__).resolve().parent / "data"))
)
_explicit_root = os.environ.get("DUAL_GRAPH_PROJECT_ROOT")
if _explicit_root:
    PROJECT_ROOT = Path(_explicit_root).resolve()
elif DG_DATA_DIR.name in (".dual-graph", ".dual-graph-pro"):
    PROJECT_ROOT = DG_DATA_DIR.parent.resolve()
else:
    PROJECT_ROOT = Path("/app/project").resolve()
LOG_FILE = DG_DATA_DIR / "mcp_tool_calls.jsonl"
ACTION_GRAPH_FILE = DG_DATA_DIR / "chat_action_graph.json"
RETRIEVAL_CACHE_FILE = DG_DATA_DIR / "retrieval_cache.json"
SYMBOL_INDEX_FILE = DG_DATA_DIR / "symbol_index.json"
CONTEXT_STORE_FILE = DG_DATA_DIR / "context-store.json"
HARD_MAX_READ_CHARS = int(os.environ.get("DG_HARD_MAX_READ_CHARS", "20000"))
TURN_READ_BUDGET_CHARS = int(os.environ.get("DG_TURN_READ_BUDGET_CHARS", "60000"))
ENFORCE_REUSE_GATE = str(os.environ.get("DG_ENFORCE_REUSE_GATE", "0")).strip() not in {"0", "false", "False"}  # v7.5: default OFF — blocked 34% of reads in v7.4
ENFORCE_SINGLE_RETRIEVE = str(os.environ.get("DG_ENFORCE_SINGLE_RETRIEVE", "1")).strip() not in {"0", "false", "False"}
ENFORCE_READ_ALLOWLIST = str(os.environ.get("DG_ENFORCE_READ_ALLOWLIST", "0")).strip() not in {"0", "false", "False"}
FALLBACK_MAX_CALLS_PER_TURN = int(os.environ.get("DG_FALLBACK_MAX_CALLS_PER_TURN", "1"))
# v7.2: Single source of truth for exhaustive grep cap — used in both graph_continue and fallback_rg.
_EXHAUSTIVE_FALLBACK_CAP = int(os.environ.get("DG_EXHAUSTIVE_FALLBACK_CAP", "15"))
RETRIEVE_CACHE_TTL_SEC = int(os.environ.get("DG_RETRIEVE_CACHE_TTL_SEC", "900"))
# Memory-first routing requires at least this many overlapping query terms to avoid
# false positives from single-word coincidental matches across sessions.
MEMORY_FIRST_MIN_OVERLAP = int(os.environ.get("DG_MEMORY_FIRST_MIN_OVERLAP", "3"))
# Per-type score multipliers for memory entries: explicit decisions outweigh auto-generated ones.
MEMORY_TYPE_MULTIPLIERS: dict[str, float] = {
    "decision": 1.5,
    "fact": 1.3,
    "blocker": 1.3,
    "next": 1.1,
    "edit_observation": 1.1,
    "interaction_pattern": 1.0,
    "query_association": 0.8,
    "stale_marker": 0.5,
}
# Explore-mode read budget: allows more total chars and larger per-file reads for
# architecture/trace queries that need to span many files.
EXPLORE_READ_BUDGET_CHARS = int(os.environ.get("DG_EXPLORE_READ_BUDGET_CHARS", "36000"))
EXPLORE_MAX_READ_CHARS = int(os.environ.get("DG_EXPLORE_MAX_READ_CHARS", "6000"))
# Session epoch: used to penalise query_association entries from prior sessions.
_SESSION_START_EPOCH: int = int(time.time())

# ── Language-aware retrieval reranking ──────────────────────────────────────
# For mixed-language codebases (e.g. C++ project with vendored Python), detect
# the dominant language family and demote files from minority languages so they
# don't drown out relevant results.

_LANG_FAMILIES: dict[str, str] = {
    ".c": "cpp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".h": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".py": "python",
    ".go": "go",
    ".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js",
    ".swift": "swift",
    ".cs": "dotnet", ".xaml": "dotnet",
    ".php": "php",
}
_LANG_CACHE: dict[str, str] = {}  # keyed by DG_DATA_DIR string


def _detect_dominant_language(gdata: dict[str, Any]) -> str | None:
    """Return the dominant language family from graph node extensions.

    Only returns a value when one family accounts for ≥60% of code files
    (excludes .json/.yaml/.yml/.md which are language-neutral).
    """
    cache_key = str(DG_DATA_DIR)
    if cache_key in _LANG_CACHE:
        return _LANG_CACHE[cache_key]

    counts: dict[str, int] = {}
    total = 0
    for node in gdata.get("nodes", []):
        if node.get("kind") != "file":
            continue
        ext = node.get("ext", "")
        family = _LANG_FAMILIES.get(ext)
        if family:
            counts[family] = counts.get(family, 0) + 1
            total += 1

    if total < 10:
        _LANG_CACHE[cache_key] = ""
        return None

    dominant = max(counts, key=counts.get)
    ratio = counts[dominant] / total
    if ratio >= 0.60:
        _LANG_CACHE[cache_key] = dominant
        return dominant

    _LANG_CACHE[cache_key] = ""
    return None


def _rerank_by_language(graph_files: list[dict], dominant_lang: str) -> list[dict]:
    """Rerank retrieval results: boost dominant-language files, demote others.

    Files from the dominant language keep their score. Files from other code
    languages get score halved. Config/docs (.json/.md/.yaml) are untouched.
    """
    for f in graph_files:
        ext = f.get("ext", "")
        family = _LANG_FAMILIES.get(ext)
        if family and family != dominant_lang:
            f["_score"] = int(f.get("_score", 0)) // 2
            f["_lang_demoted"] = True

    graph_files.sort(key=lambda f: int(f.get("_score", 0)), reverse=True)
    return graph_files


# Process-local state for adaptive budgeting and dedupe.
def _default_turn_state() -> dict[str, Any]:
    return {
        "query_key": "",
        "used_chars": 0,
        "seen_reads": {},
        "reuse_gate_candidates": [],
        "reuse_gate_satisfied": False,
        "retrieved_files": [],
        "retrieve_count": 0,
        "last_retrieve_out": None,
        "fallback_calls": 0,
        "total_read_chars": 0,
        "total_grep_calls": 0,
        "grep_all_calls": 0,
        "turn_count": 0,
        "task_type": "unknown",
        "graph_continue_called": False,
        "turn_tokens_served": 0,
        "turn_tokens_avoided": 0,
        "turn_full_file_tokens": 0,
        "turn_tool_hits": {},
    }

# Session store: mcp-session-id → persistent state dict
_SESSION_STATES: dict[str, dict[str, Any]] = {}
# Current request's session ID — set by ASGI middleware before each tool call
_CURRENT_SID: str = "default"


def _get_session_state(session_id: str) -> dict[str, Any]:
    sid = session_id or "default"
    if sid not in _SESSION_STATES:
        _SESSION_STATES[sid] = _default_turn_state()
    if len(_SESSION_STATES) > 50:
        for k in list(_SESSION_STATES.keys())[:-50]:
            del _SESSION_STATES[k]
    return _SESSION_STATES[sid]


def _sanitize_sid(raw: str) -> str:
    """Sanitize session ID: alphanumeric + hyphen/underscore only, max 128 chars."""
    import re as _re
    clean = _re.sub(r"[^a-zA-Z0-9_\-]", "_", raw or "default")[:128]
    return clean or "default"


def _set_session(session_id: str) -> None:
    """Set the current session ID. Tool calls use _get_current_state() to read/write."""
    global _CURRENT_SID
    _CURRENT_SID = _sanitize_sid(session_id)
    _get_session_state(_CURRENT_SID)  # ensure it exists


def _get_current_state() -> dict[str, Any]:
    """Return the persistent state dict for the current request's session."""
    return _SESSION_STATES.get(_CURRENT_SID, TURN_STATE)


class _TurnStateProxy(dict):
    """Proxy TURN_STATE to the current session's persistent dict.

    All 76+ existing `TURN_STATE[key]` references route through here.
    Reads/writes go to _SESSION_STATES[_CURRENT_SID] so state persists
    across sequential tool calls within one Claude conversation.
    """
    def _s(self) -> dict:
        return _SESSION_STATES.get(_CURRENT_SID, self)

    def __getitem__(self, k):           return self._s()[k]
    def __setitem__(self, k, v):        self._s()[k] = v
    def __delitem__(self, k):           del self._s()[k]
    def __contains__(self, k):          return k in self._s()
    def get(self, k, default=None):     return self._s().get(k, default)
    def update(self, *a, **kw):         self._s().update(*a, **kw)
    def clear(self):                    self._s().clear()
    def setdefault(self, k, d=None):    return self._s().setdefault(k, d)
    def keys(self):                     return self._s().keys()
    def values(self):                   return self._s().values()
    def items(self):                    return self._s().items()
    def pop(self, *a):                  return self._s().pop(*a)
    def __repr__(self):                 return repr(self._s())


TURN_STATE: dict[str, Any] = _TurnStateProxy(_default_turn_state())

# v6.1: Task-aware budgets — generous for exhaustive, tight for targeted
_COST_BUDGETS = {
    "targeted":   {"warn": 120_000, "hard": 200_000},
    "behavioral": {"warn": 150_000, "hard": 250_000},
    "exhaustive": {"warn": 300_000, "hard": 500_000},   # 2.5x targeted — room for full coverage
    "unknown":    {"warn": 120_000, "hard": 200_000},
}
_TURN_LIMITS = {
    "targeted":   {"warn": 8,  "hard": 14},
    "behavioral": {"warn": 10, "hard": 16},
    "exhaustive": {"warn": 15, "hard": 25},  # nearly 2x targeted
    "unknown":    {"warn": 8,  "hard": 14},
}


def _classify_task_type(query: str) -> str:
    """Classify query as targeted, exhaustive, or behavioral for budget tuning."""
    q = query.lower()
    # v7.2: Honour explicit [task_type:X] prefix injected by benchmark/caller — no guessing.
    import re as _re
    _explicit = _re.match(r"\[task_type:(exhaustive|targeted|behavioral)\]", q.lstrip())
    if _explicit:
        return _explicit.group(1)
    # Exhaustive: audit, scan, review, find all, list all, check every
    exhaustive_signals = [
        "audit", "scan", "review all", "find all", "list all", "check every",
        "check all", "identify all", "across the codebase", "entire codebase",
        "every file", "all files", "comprehensive", "dead code", "unused",
        "naming", "convention", "consistency",
        # v7.1: broader exhaustive detection — security audit patterns
        "find places", "find code", "find functions", "search the",
        "search for", "look for", "all places", "all instances",
        "buffer overflow", "sql injection", "vulnerability", "vulnerabilities",
        "error handling", "anti-pattern", "insecure", "hardcoded",
        "enumerate", "cover all", "all major", "all modules",
        "all directories", "all packages", "across all",
        # v7.1.1: catch "find X" and "list X" patterns missed by earlier signals
        "find global", "find classes", "find the call", "find where",
        "list the class", "list the worst", "list them",
        "how bad is", "how many", "which files",
        "call sites", "double-free", "resource leak", "copy constructor",
        "rule of three", "rule of five", "sensitive data",
        "log sensitive", "global variable", "mutable variable",
        "command injection", "use-after-free", "dangling pointer",
        "integer overflow", "arithmetic error",
    ]
    if any(s in q for s in exhaustive_signals):
        return "exhaustive"
    # Behavioral: runtime, race condition, concurrency, deadlock, memory leak, performance
    behavioral_signals = [
        "race condition", "deadlock", "memory leak", "goroutine leak",
        "concurrency", "thread safe", "runtime", "panic", "crash",
        "timeout", "slow", "performance issue", "bottleneck",
        "data race", "mutex", "channel block",
    ]
    if any(s in q for s in behavioral_signals):
        return "behavioral"
    # Targeted: everything else (fix bug, add feature, refactor X)
    return "targeted"


def _detect_query_intent(query: str) -> str:
    """Detect specific query intent for gap-aware routing.

    Returns one of: content_search, control_flow, absence_query, dynamic_trace, general.
    This runs BEFORE task_type classification and influences confidence + strategy hints.
    """
    q = query.lower()

    content_signals = [
        "hardcoded", "as any", "type assertion", "magic number", "magic string",
        "string literal", "regex pattern", "console.log", "debugger statement",
        "todo", "fixme", "hack", "xxx", "deprecated", "eslint-disable",
        "ts-ignore", "ts-expect-error", "noqa", "nolint",
        "hardcoded url", "hardcoded ip", "hardcoded secret", "hardcoded password",
        "commented out", "dead comment",
    ]
    if any(s in q for s in content_signals):
        return "content_search"

    flow_signals = [
        "empty catch", "swallow error", "silent catch", "catch block",
        "inside loop", "in a loop", "db call in loop", "query in loop",
        "await in loop", "n+1", "nested callback", "callback hell",
        "deep nesting", "try without",
    ]
    if any(s in q for s in flow_signals):
        return "control_flow"

    absence_signals = [
        "without auth", "without validation", "without middleware",
        "missing auth", "missing validation", "missing test",
        "no test", "no auth", "no middleware", "no validation",
        "unprotected", "unchecked", "unvalidated", "unauthenticated",
        "routes without", "endpoints without", "exports without",
        "lacking", "not covered", "not tested",
    ]
    if any(s in q for s in absence_signals):
        return "absence_query"

    dynamic_signals = [
        "container.resolve", "container.register", "dependency injection",
        "emit event", "event emit", "event handler", "event listener",
        "subscriber", "on event", ".on(", ".emit(",
        "dynamic import", "lazy load", "register service",
    ]
    if any(s in q for s in dynamic_signals):
        return "dynamic_trace"

    return "general"


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"}
    if DG_API_TOKEN:
        headers["authorization"] = f"Bearer {DG_API_TOKEN}"
    req = urllib.request.Request(
        url=f"{DG_BASE}{path}",
        data=raw,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str) -> dict[str, Any]:
    req = urllib.request.Request(url=f"{DG_BASE}{path}", method="GET")
    if DG_API_TOKEN:
        req.add_header("authorization", f"Bearer {DG_API_TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Module-level cache for info graph and symbol index, keyed by file mtime.
# Avoids re-parsing MB-sized JSON on every tool call.
_INFO_GRAPH_CACHE: dict[str, Any] | None = None
_INFO_GRAPH_MTIME: int = -1
_SYMBOL_INDEX_CACHE: dict[str, Any] | None = None
_SYMBOL_INDEX_MTIME: int = -1
_CONTEXT_STORE_CACHE: list[dict[str, Any]] | None = None
_CONTEXT_STORE_MTIME: int = -1


def _local_info_graph() -> dict[str, Any] | None:
    """Read the info-graph from local DG_DATA_DIR without HTTP. Returns None if unavailable.
    Results are cached in memory and invalidated when the file mtime changes."""
    global _INFO_GRAPH_CACHE, _INFO_GRAPH_MTIME  # noqa: PLW0603
    graph_json = DG_DATA_DIR / "info_graph.json"
    if not graph_json.exists():
        return None
    try:
        current_mtime = int(graph_json.stat().st_mtime_ns)
        if _INFO_GRAPH_CACHE is not None and current_mtime == _INFO_GRAPH_MTIME:
            return _INFO_GRAPH_CACHE
        data = json.loads(graph_json.read_text(encoding="utf-8"))
        # Strip content from file nodes — retrieval doesn't use it, saves RAM.
        # Railway-mode graph_read reads info_graph.json directly (separate path).
        for node in data.get("nodes", []):
            node.pop("content", None)
        _INFO_GRAPH_CACHE = data
        _INFO_GRAPH_MTIME = current_mtime
        return data
    except Exception:
        return None


def _build_symbol_index(graph: dict[str, Any]) -> dict[str, Any]:
    """Build a flat {symbol_id: metadata} dict for O(1) graph_read lookup."""
    index: dict[str, Any] = {}
    for node in graph.get("nodes", []):
        if node.get("kind") == "symbol":
            node_id = str(node.get("id", ""))
            if node_id:
                index[node_id] = {
                    "line_start": node.get("line_start", 0),
                    "line_end": node.get("line_end", 0),
                    "body_hash": node.get("body_hash", ""),
                    "confidence": node.get("confidence", ""),
                    "path": node.get("path", ""),
                }
    return index


def _load_symbol_index() -> dict[str, Any]:
    """Load symbol index with in-memory caching keyed by file mtime."""
    global _SYMBOL_INDEX_CACHE, _SYMBOL_INDEX_MTIME  # noqa: PLW0603
    if not SYMBOL_INDEX_FILE.exists():
        return {}
    try:
        current_mtime = int(SYMBOL_INDEX_FILE.stat().st_mtime_ns)
        if _SYMBOL_INDEX_CACHE is not None and current_mtime == _SYMBOL_INDEX_MTIME:
            return _SYMBOL_INDEX_CACHE
        data = json.loads(SYMBOL_INDEX_FILE.read_text(encoding="utf-8"))
        _SYMBOL_INDEX_CACHE = data
        _SYMBOL_INDEX_MTIME = current_mtime
        return data
    except Exception:
        return {}


def _resolve_to_symbols(file_paths: list[str], query: str, max_per_file: int = 1) -> list[str]:
    """v7.3: Upgrade bare file paths to file::symbol entries where possible.

    For each file path, score all symbols in that file against the query terms
    and return the best-matching symbol ID instead of the bare path.  Falls back
    to the bare path when no symbol matches or the symbol index is unavailable.

    This reduces graph_read token cost by 10-15x — Claude reads only the relevant
    function body (~30 lines) instead of the entire file (~300-500 lines).
    """
    sym_idx = _load_symbol_index()
    if not sym_idx:
        return file_paths  # index not built yet, fall back gracefully

    query_terms = set(re.split(r"[\s_\-/.:]+", query.lower()))
    query_terms.discard("")
    query_terms = {t for t in query_terms if len(t) >= 3}

    result: list[str] = []
    for fp in file_paths:
        # Collect all symbols that live in this file.
        candidates: list[tuple[int, str]] = []  # (score, symbol_id)
        for sym_id, meta in sym_idx.items():
            if meta.get("path", "") != fp:
                continue
            # Score: count query term hits in the symbol name (part after ::)
            sym_name = sym_id.split("::")[-1].lower() if "::" in sym_id else ""
            score = sum(1 for t in query_terms if t in sym_name)
            # Bonus: symbol has meaningful line range (not a tiny stub)
            line_span = int(meta.get("line_end", 0)) - int(meta.get("line_start", 0))
            if line_span >= 5:
                score += 1
            if score > 0:
                candidates.append((score, sym_id))

        if candidates:
            candidates.sort(key=lambda x: -x[0])
            # Return top symbol(s) for this file
            for _, sym_id in candidates[:max_per_file]:
                result.append(sym_id)
        else:
            result.append(fp)  # no symbol match — keep bare file path

    return result


def _local_chat_fix(query: str, top_files: int, top_edges: int) -> dict[str, Any] | None:
    """Run retrieval locally from the graph file, bypassing /api/chat-fix HTTP call.
    Returns None if local retrieval is unavailable (falls back to HTTP)."""
    if _dg_retrieve is None:
        return None
    g = _local_info_graph()
    if g is None:
        return None
    try:
        rel = _dg_retrieve(g, query, top_files, top_edges)
        return {
            "ok": True,
            "query": query,
            "graph_files": rel.files,
            "graph_edges": rel.edges,
            "grep_hits": [],
        }
    except Exception:
        return None


def _is_local_file_ref(value: str) -> bool:
    if not value:
        return False
    if value.startswith("@"):
        return False
    # Strip symbol suffix (e.g. "src/auth.ts::handleLogin" → "src/auth.ts")
    file_part = value.split("::")[0] if "::" in value else value
    if ":" in file_part:  # URL-like (http:, C:\)
        return False
    if "/" not in file_part:
        return False
    return "." in file_part.split("/")[-1]


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _query_terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_]+", query.lower())
    stop = {
        "a", "an", "the", "and", "or", "to", "for", "with", "in", "on", "by", "of",
        "please", "can", "could", "would", "should", "will", "use", "update", "fix",
        "make", "show", "this", "that", "it",
    }
    out: list[str] = []
    seen = set()
    for w in words:
        if len(w) < 3 or w in stop or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:8]


def _query_key(query: str) -> str:
    return " ".join(_query_terms(query))


def _excerpt_by_terms(text: str, terms: list[str], max_chars: int) -> str:
    if not terms:
        return text[:max_chars]
    lines = text.splitlines()
    if not lines:
        return text[:max_chars]
    picks: list[str] = []
    seen_blocks: set[int] = set()
    for idx, line in enumerate(lines):
        blob = line.lower()
        if not any(t in blob for t in terms):
            continue
        start = max(0, idx - 12)
        end = min(len(lines), idx + 13)
        if start in seen_blocks:
            continue
        seen_blocks.add(start)
        block = "\n".join(lines[start:end])
        picks.append(block)
        if sum(len(x) for x in picks) >= max_chars:
            break
    if not picks:
        return text[:max_chars]
    out = "\n\n/* --- excerpt --- */\n\n".join(picks)
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


_LOG_LOCK = threading.Lock()


def _log_tool(name: str, payload: dict[str, Any], turn_override: int | None = None) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": name,
        "turn": turn_override if turn_override is not None else int(TURN_STATE.get("turn_count", 0)),
        "payload": payload,
    }
    with _LOG_LOCK:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


# ── Shadow savings measurement ────────────────────────────────────────────
# Run real measurements (grep, stat) in background threads. Results are logged
# but never sent to Claude — zero token cost, accurate data.
#
# Design principles:
# - No hardcoded caps. Use measured data to determine what vanilla would've done.
# - Account for the fact that vanilla Claude also has smart behaviors (partial reads,
#   skipping obvious files, conversation history).
# - Log raw measurements so /savings can apply corrections at read time.

# Read tool default limit: 2000 lines. Approximate as chars (avg 60 chars/line).
_READ_TOOL_MAX_CHARS = 2000 * 60  # 120000 chars — Read tool's effective ceiling

# Bash tool inline limit: empirically measured at ~21k chars (above this,
# output is saved to file and Claude only sees a 2KB preview).
_BASH_TOOL_MAX_CHARS = 21_000
# When output exceeds inline limit, Claude sees a 2KB preview instead.
_BASH_TOOL_PERSISTED_PREVIEW_CHARS = 2_048


def _query_has_file_path(query: str) -> bool:
    """Detect if query contains an obvious file path (vanilla Claude wouldn't grep)."""
    return bool(re.search(r'[\w/.-]+\.(ts|tsx|js|jsx|py|go|rs|cpp|h|java|rb|vue|svelte)\b', query))


def _shadow_grep_measure(query: str, recommended_files: list[str], turn: int) -> None:
    """Run rg and measure what vanilla Claude would have spent exploring.

    Skip if the query contains an obvious file path (vanilla Claude would just Read it).
    Use the most specific query term. Cap output at Bash tool's response limit.
    """
    try:
        # If query mentions a specific file, vanilla Claude wouldn't grep — it would Read directly
        if _query_has_file_path(query):
            return
        terms = _query_terms(query)
        if not terms:
            return
        # Pick the longest/most specific term (what a real Claude would grep for)
        best_term = max(terms, key=len)
        try:
            r = subprocess.run(
                ["rg", "-n", "-S", best_term, str(PROJECT_ROOT)],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (subprocess.TimeoutExpired, Exception):
            return
        raw_chars = len(r.stdout)
        if raw_chars == 0:
            return
        # If output fits inline (<21k), Claude sees it all.
        # If output exceeds inline limit, Claude only sees the 2KB persisted preview.
        if raw_chars <= _BASH_TOOL_MAX_CHARS:
            effective_chars = raw_chars
        else:
            effective_chars = _BASH_TOOL_PERSISTED_PREVIEW_CHARS
        hit_count = r.stdout[:effective_chars].count("\n")
        _log_tool("shadow_grep", {
            "query": query,
            "term_used": best_term,
            "query_had_file_path": False,
            "raw_output_chars": raw_chars,
            "effective_output_chars": effective_chars,
            "hit_count": hit_count,
            "tokens_would_have_cost": effective_chars // 4,
            "files_recommended": len(recommended_files),
        }, turn_override=turn)
    except Exception:
        pass


def _shadow_file_reads_measure(source: str, hit_files: list[str], inlined_chars: int, turn: int) -> None:
    """Measure full file sizes for files vanilla Claude would have Read after a grep.

    - Cap each file at Read tool's 2000-line limit (vanilla Claude also reads partially).
    - Log which files were measured so /savings can dedup against graph_read TAR.
    """
    try:
        total_full_chars = 0
        total_capped_chars = 0
        files_measured = 0
        measured_files: list[str] = []
        for fpath in hit_files:
            abs_path = PROJECT_ROOT / fpath
            try:
                size = abs_path.stat().st_size
            except (OSError, Exception):
                continue
            capped_size = min(size, _READ_TOOL_MAX_CHARS)
            total_full_chars += size
            total_capped_chars += capped_size
            files_measured += 1
            measured_files.append(fpath)
        if files_measured == 0:
            return
        # Savings = what vanilla would've read (capped) - what we already inlined
        tokens_avoided = max(0, total_capped_chars - inlined_chars) // 4
        _log_tool("shadow_file_reads", {
            "source": source,
            "total_hit_files": len(hit_files),
            "files_measured": files_measured,
            "files": measured_files,
            "full_file_chars": total_full_chars,
            "capped_file_chars": total_capped_chars,
            "inlined_chars": inlined_chars,
            "tokens_avoided": tokens_avoided,
        }, turn_override=turn)
    except Exception:
        pass


def _fire_shadow_grep(query: str, recommended_files: list[str]) -> None:
    turn = int(TURN_STATE.get("turn_count", 0))
    threading.Thread(
        target=_shadow_grep_measure,
        args=(query, recommended_files, turn),
        daemon=True,
    ).start()


def _fire_shadow_file_reads(source: str, hit_files: list[str], inlined_chars: int) -> None:
    turn = int(TURN_STATE.get("turn_count", 0))
    threading.Thread(
        target=_shadow_file_reads_measure,
        args=(source, hit_files, inlined_chars, turn),
        daemon=True,
    ).start()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Incremental graph update ─────────────────────────────────────────────
# Deploy: cp mcp_graph_server_v7.5.py ~/.graperoot-pro/mcp_graph_server_v7.5.py

def _detect_changed_files(root: Path, since_epoch: int) -> list[str]:
    """Return relative paths of files whose mtime > since_epoch.

    Uses `find -newer stamp_file` — works on any project, no git needed.
    Excludes .git, node_modules, .dual-graph*, __pycache__, and binary
    extensions that the graph builder ignores anyway.
    """
    stamp = DG_DATA_DIR / "last_scan_stamp"
    # Write/update stamp to match since_epoch so find -newer works correctly
    try:
        import os as _os
        stamp.write_text(str(since_epoch), encoding="utf-8")
        # Set the stamp file's mtime to since_epoch so find -newer is precise
        _os.utime(str(stamp), (since_epoch, since_epoch))
    except OSError:
        pass

    try:
        r = subprocess.run(
            [
                "find", str(root), "-newer", str(stamp), "-type", "f",
                "-not", "-path", "*/.git/*",
                "-not", "-path", "*/node_modules/*",
                "-not", "-path", "*/.dual-graph*",
                "-not", "-path", "*/.claude/*",
                "-not", "-path", "*/.cursor/*",
                "-not", "-path", "*/__pycache__/*",
                "-not", "-name", "*.pyc",
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        result = []
        for f in r.stdout.splitlines():
            f = f.strip()
            if not f:
                continue
            try:
                result.append(str(Path(f).relative_to(root)))
            except ValueError:
                pass
        return result
    except Exception:
        return []


def _incremental_update(root: Path, changed_files: list[str], graph: dict) -> dict:
    """Re-scan changed_files and patch their nodes/edges into the existing graph.

    Skips dead_exports recomputation — that requires the full graph.
    """
    if _gb_scan is None:
        return graph
    changed_set = set(changed_files)

    # Remove old nodes and edges for changed files
    graph["nodes"] = [
        n for n in graph.get("nodes", [])
        if n.get("path", n.get("id", "")).split("::")[0] not in changed_set
    ]
    graph["edges"] = [
        e for e in graph.get("edges", [])
        if e.get("from", "").split("::")[0] not in changed_set
    ]

    # Build existing_nodes from the pruned graph so builder skips unchanged files
    existing_nodes = {n["id"]: n for n in graph["nodes"] if n.get("kind") == "file"}

    # Re-scan the full project — unchanged files skip instantly via hash comparison
    try:
        new_graph = _gb_scan(root, existing_nodes=existing_nodes)
    except Exception:
        return graph

    # Merge only nodes/edges for the changed files back in
    for n in new_graph.get("nodes", []):
        if n.get("path", n.get("id", "")).split("::")[0] in changed_set:
            graph["nodes"].append(n)
    for e in new_graph.get("edges", []):
        if e.get("from", "").split("::")[0] in changed_set:
            graph["edges"].append(e)

    # Update counts and timestamp
    graph["node_count"] = len(graph["nodes"])
    graph["edge_count"] = len(graph["edges"])
    graph["file_count"] = len([n for n in graph["nodes"] if n.get("kind") == "file"])
    graph["symbol_count"] = len([n for n in graph["nodes"] if n.get("kind") == "symbol"])
    graph["built_at"] = int(time.time())
    return graph


def _load_action_graph() -> dict[str, Any]:
    if not ACTION_GRAPH_FILE.exists():
        return {"nodes": [], "edges": [], "files": {}, "actions": []}
    try:
        return json.loads(ACTION_GRAPH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"nodes": [], "edges": [], "files": {}, "actions": []}


def _save_action_graph(g: dict[str, Any]) -> None:
    ACTION_GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTION_GRAPH_FILE.write_text(json.dumps(g, indent=2), encoding="utf-8")


def _memory_id() -> str:
    return f"mem:{int(datetime.now(timezone.utc).timestamp() * 1000)}"


def _memory_anchor_for_ref(ref: str, symbol_index: dict[str, Any]) -> tuple[str, str, str]:
    if not ref:
        return "", "", ""
    if "::" in ref:
        sym = ref
        meta = symbol_index.get(sym, {})
        return sym, str(meta.get("path", ref.split("::")[0])), str(meta.get("body_hash", ""))
    return "", ref, ""


def _merge_unique_strings(values: list[Any], limit: int = 6) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_memory_entry(entry: Any, symbol_index: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    content = str(entry.get("content") or entry.get("summary") or "").strip()
    if not content:
        return None
    raw_files = entry.get("files", [])
    files = [str(f) for f in raw_files if isinstance(f, str) and str(f).strip()] if isinstance(raw_files, list) else []
    symbol_id = str(entry.get("symbol_id") or "").strip()
    file_path = str(entry.get("file_path") or "").strip()
    symbol_hash = str(entry.get("symbol_hash") or "").strip()
    if not symbol_id and files:
        symbol_id, anchored_file, anchored_hash = _memory_anchor_for_ref(files[0], symbol_index)
        if anchored_file and not file_path:
            file_path = anchored_file
        if anchored_hash and not symbol_hash:
            symbol_hash = anchored_hash
    if symbol_id and not file_path:
        file_path = str(symbol_index.get(symbol_id, {}).get("path", symbol_id.split("::")[0]))
    if symbol_id and not symbol_hash:
        symbol_hash = str(symbol_index.get(symbol_id, {}).get("body_hash", ""))
    if not files:
        if symbol_id:
            files = [symbol_id]
        elif file_path:
            files = [file_path]
    stale = bool(entry.get("stale", False))
    stale_reason = str(entry.get("stale_reason", "")).strip()
    created_at = str(entry.get("created_at") or entry.get("updated_at") or entry.get("date") or _now_iso())
    updated_at = str(entry.get("updated_at") or created_at)
    created_epoch = int(entry.get("created_epoch") or 0)
    if created_epoch <= 0:
        try:
            created_epoch = int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp())
        except Exception:
            created_epoch = int(datetime.now(timezone.utc).timestamp())
    tags = entry.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return {
        "id": str(entry.get("id") or _memory_id()),
        "kind": str(entry.get("kind") or entry.get("type") or "fact"),
        "content": content,
        "tags": [str(t) for t in tags if isinstance(t, str)],
        "files": files,
        "file_path": file_path,
        "symbol_id": symbol_id,
        "symbol_hash": symbol_hash,
        "created_at": created_at,
        "created_epoch": created_epoch,
        "updated_at": updated_at,
        "stale": stale,
        "stale_reason": stale_reason,
        "observed_queries": [str(q) for q in entry.get("observed_queries", []) if isinstance(q, str)] if isinstance(entry.get("observed_queries", []), list) else [],
        "evidence": entry.get("evidence", {}) if isinstance(entry.get("evidence", {}), dict) else {},
    }


def _memory_identity(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    kind = str(entry.get("kind", ""))
    symbol_id = str(entry.get("symbol_id", ""))
    file_path = str(entry.get("file_path", ""))
    if kind == "interaction_pattern":
        return (kind, symbol_id, file_path, "")
    if kind == "edit_observation":
        return (kind, symbol_id, file_path, "")
    if kind == "stale_marker":
        return (kind, symbol_id, file_path, str(entry.get("stale_reason", "")))
    return (
        kind,
        symbol_id,
        file_path,
        str(entry.get("content", "")),
    )


def _prune_context_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Drop auto-generated entries older than 14 days to prevent cross-session noise buildup.
    _QA_MAX_AGE_SECS = 14 * 86400
    _AUTO_KINDS = {"query_association", "interaction_pattern"}
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    entries = [
        row for row in entries
        if str(row.get("kind", "")) not in _AUTO_KINDS
        or (now_epoch - int(row.get("created_epoch", 0) or 0)) <= _QA_MAX_AGE_SECS
    ]
    caps = {
        "query_association": 4,
        "edit_observation": 2,
        "interaction_pattern": 1,
        "stale_marker": 1,
        "decision": 3,
    }
    ranked = sorted(entries, key=lambda row: int(row.get("created_epoch", 0) or 0), reverse=True)
    kept: list[dict[str, Any]] = []
    counts: dict[tuple[str, str, str], int] = {}
    for row in ranked:
        kind = str(row.get("kind", ""))
        symbol_id = str(row.get("symbol_id", ""))
        file_path = str(row.get("file_path", ""))
        bucket = (kind, symbol_id, file_path)
        cap = caps.get(kind, 6)
        if counts.get(bucket, 0) >= cap:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        kept.append(row)
        if len(kept) >= 200:
            break
    kept.reverse()
    return kept


def _merge_memory_entries(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["updated_at"] = str(incoming.get("updated_at") or _now_iso())
    merged["stale"] = bool(existing.get("stale") or incoming.get("stale"))
    merged["stale_reason"] = str(incoming.get("stale_reason") or existing.get("stale_reason") or "")
    merged["tags"] = _merge_unique_strings(list(existing.get("tags", [])) + list(incoming.get("tags", [])), limit=10)
    merged["files"] = _merge_unique_strings(list(existing.get("files", [])) + list(incoming.get("files", [])), limit=10)
    merged["observed_queries"] = _merge_unique_strings(
        list(existing.get("observed_queries", [])) + list(incoming.get("observed_queries", [])),
        limit=6,
    )
    evidence = dict(existing.get("evidence", {}))
    for key, value in incoming.get("evidence", {}).items():
        if isinstance(value, list):
            evidence[key] = _merge_unique_strings(list(evidence.get(key, [])) + value, limit=8)
        elif isinstance(value, bool):
            evidence[key] = bool(evidence.get(key, False) or value)
        elif isinstance(value, int):
            evidence[key] = max(int(evidence.get(key, 0) or 0), value)
        elif value not in (None, "", []):
            evidence[key] = value
    merged["evidence"] = evidence
    return merged


def _upsert_context_memory(candidate: dict[str, Any], symbol_index: dict[str, Any] | None = None) -> dict[str, Any] | None:
    idx = symbol_index if symbol_index is not None else _load_symbol_index()
    normalized = _normalize_memory_entry(candidate, idx)
    if normalized is None:
        return None
    entries = _load_context_store(idx)
    target = _memory_identity(normalized)
    for i, row in enumerate(entries):
        if _memory_identity(row) != target:
            continue
        entries[i] = _merge_memory_entries(row, normalized)
        _save_context_store(entries)
        return entries[i]
    entries.append(normalized)
    _save_context_store(entries)
    return normalized


def _load_context_store(symbol_index: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    global _CONTEXT_STORE_CACHE, _CONTEXT_STORE_MTIME  # noqa: PLW0603
    if not CONTEXT_STORE_FILE.exists():
        return []
    try:
        current_mtime = int(CONTEXT_STORE_FILE.stat().st_mtime_ns)
        if _CONTEXT_STORE_CACHE is not None and current_mtime == _CONTEXT_STORE_MTIME:
            return list(_CONTEXT_STORE_CACHE)
    except Exception:
        pass
    idx = symbol_index if symbol_index is not None else _load_symbol_index()
    try:
        raw = json.loads(CONTEXT_STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        # Prune-on-load: if file grew beyond cap, heal it immediately
        if len(raw) > 200:
            _save_context_store([r for r in raw if isinstance(r, dict)])
            raw = json.loads(CONTEXT_STORE_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
        out: list[dict[str, Any]] = []
        for row in raw:
            norm = _normalize_memory_entry(row, idx)
            if norm is not None:
                out.append(norm)
        try:
            _CONTEXT_STORE_MTIME = int(CONTEXT_STORE_FILE.stat().st_mtime_ns)
        except Exception:
            pass
        _CONTEXT_STORE_CACHE = out
        return list(out)
    except Exception:
        return []


def _save_context_store(entries: list[dict[str, Any]]) -> None:
    global _CONTEXT_STORE_CACHE, _CONTEXT_STORE_MTIME  # noqa: PLW0603
    _CONTEXT_STORE_CACHE = None  # invalidate cache on write
    _CONTEXT_STORE_MTIME = -1
    CONTEXT_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_STORE_FILE.write_text(json.dumps(_prune_context_entries(entries), indent=2), encoding="utf-8")


def _reconcile_context_store(symbol_index: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    idx = symbol_index if symbol_index is not None else _load_symbol_index()
    entries = _load_context_store(idx)
    changed = False
    stale_candidates: list[dict[str, Any]] = []
    for row in entries:
        sym = row.get("symbol_id", "")
        if not sym:
            continue
        meta = idx.get(sym)
        if not meta:
            if not row.get("stale") or row.get("stale_reason") != "symbol removed":
                row["stale"] = True
                row["stale_reason"] = "symbol removed"
                row["updated_at"] = _now_iso()
                changed = True
                stale_candidates.append(
                    {
                        "kind": "stale_marker",
                        "content": "Marked stale because symbol removed",
                        "files": list(row.get("files", [])),
                        "file_path": str(row.get("file_path", "")),
                        "symbol_id": sym,
                        "symbol_hash": str(row.get("symbol_hash", "")),
                        "created_at": _now_iso(),
                        "created_epoch": int(datetime.now(timezone.utc).timestamp()),
                        "updated_at": _now_iso(),
                        "stale": True,
                        "stale_reason": "symbol removed",
                        "observed_queries": list(row.get("observed_queries", [])),
                        "evidence": {"source_kind": str(row.get("kind", ""))},
                    }
                )
            continue
        current_hash = str(meta.get("body_hash", ""))
        if not row.get("symbol_hash") and current_hash:
            row["symbol_hash"] = current_hash
            changed = True
        if not row.get("file_path"):
            row["file_path"] = str(meta.get("path", sym.split("::")[0]))
            changed = True
        if row.get("symbol_hash") and current_hash and row.get("symbol_hash") != current_hash:
            if not row.get("stale") or row.get("stale_reason") != "symbol body changed":
                row["stale"] = True
                row["stale_reason"] = "symbol body changed"
                row["updated_at"] = _now_iso()
                changed = True
                stale_candidates.append(
                    {
                        "kind": "stale_marker",
                        "content": "Marked stale because symbol body changed",
                        "files": list(row.get("files", [])),
                        "file_path": str(row.get("file_path", "")),
                        "symbol_id": sym,
                        "symbol_hash": str(row.get("symbol_hash", "")),
                        "created_at": _now_iso(),
                        "created_epoch": int(datetime.now(timezone.utc).timestamp()),
                        "updated_at": _now_iso(),
                        "stale": True,
                        "stale_reason": "symbol body changed",
                        "observed_queries": list(row.get("observed_queries", [])),
                        "evidence": {"source_kind": str(row.get("kind", ""))},
                    }
                )
    if changed:
        _save_context_store(entries)
    for candidate in stale_candidates:
        _upsert_context_memory(candidate, idx)
    return entries


def _score_memory_entry(entry: dict[str, Any], qterms: set[str], related_files: set[str]) -> tuple[int, list[str]]:
    score = 0
    why: list[str] = []
    blob = " ".join(
        [
            str(entry.get("content", "")).lower(),
            " ".join(str(t).lower() for t in entry.get("tags", [])),
            str(entry.get("symbol_id", "")).lower(),
            str(entry.get("file_path", "")).lower(),
        ]
    )
    term_hits = sum(1 for t in qterms if t in blob)
    if term_hits:
        score += term_hits * 3
        why.append(f"{term_hits} query term match")
    symbol_id = str(entry.get("symbol_id", ""))
    file_path = str(entry.get("file_path", ""))
    if symbol_id and symbol_id in related_files:
        score += 5
        why.append("exact symbol match")
    elif file_path and file_path in related_files:
        score += 4
        why.append("same file as retrieved context")
    created_epoch = int(entry.get("created_epoch", 0) or 0)
    if created_epoch > 0:
        age_days = max(0.0, (int(datetime.now(timezone.utc).timestamp()) - created_epoch) / 86400.0)
        recency_score = 4.0 * math.pow(0.5, age_days / 7.0)
        if recency_score > 0.1:
            score += int(round(recency_score))
            why.append(f"recency {recency_score:.1f}")
    if entry.get("stale"):
        score -= 6
        why.append("stale penalty")
    else:
        score += 1
        why.append("fresh memory")
    # Cross-session penalty: auto-generated query_association entries from prior sessions
    # are less reliable than entries from the current session.
    if str(entry.get("kind", "")) == "query_association":
        entry_session = int((entry.get("evidence") or {}).get("session_epoch", 0) or 0)
        if entry_session > 0 and entry_session != _SESSION_START_EPOCH:
            score -= 2
            why.append("cross-session penalty")
    # Type-based score multiplier: explicit decisions outweigh auto-generated entries.
    kind = str(entry.get("kind", ""))
    multiplier = MEMORY_TYPE_MULTIPLIERS.get(kind, 1.0)
    if multiplier != 1.0:
        score = int(round(score * multiplier))
        why.append(f"type_weight×{multiplier:.1f}")
    return score, why


def _search_context_store(query: str, related_files: list[str] | None = None, limit: int = 3) -> list[dict[str, Any]]:
    qterms = set(_query_terms(query))
    related = set(related_files or [])
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in _reconcile_context_store():
        score, why = _score_memory_entry(row, qterms, related)
        if score <= 0:
            continue
        payload = dict(row)
        # Flag weak matches on auto-generated entries so callers can discount them.
        row_blob = " ".join([
            str(row.get("content", "")).lower(),
            " ".join(str(t).lower() for t in row.get("tags", [])),
            str(row.get("symbol_id", "")).lower(),
            str(row.get("file_path", "")).lower(),
        ])
        raw_term_hits = sum(1 for t in qterms if t in row_blob)
        if raw_term_hits < 2 and score < 10 and str(row.get("kind", "")) == "query_association":
            payload["weak_match"] = True
            payload["weak_match_reason"] = f"auto-generated, only {raw_term_hits} term overlap"
        payload["why"] = ", ".join(why[:3])
        scored.append((score, payload))
    scored.sort(
        key=lambda item: (
            -item[0],
            bool(item[1].get("stale")),
            -int(item[1].get("created_epoch", 0) or 0),
        )
    )
    return [row for _, row in scored[:limit]]


def _ensure_node(g: dict[str, Any], node_id: str, node_type: str, meta: dict[str, Any] | None = None) -> None:
    nodes = g.setdefault("nodes", [])
    if any(n.get("id") == node_id for n in nodes):
        return
    row = {"id": node_id, "type": node_type}
    if meta:
        row["meta"] = meta
    nodes.append(row)


def _add_edge(g: dict[str, Any], frm: str, to: str, rel: str, meta: dict[str, Any] | None = None) -> None:
    edges = g.setdefault("edges", [])
    # Use epoch seconds (not ISO string) to save space.
    row: dict[str, Any] = {"from": frm, "to": to, "rel": rel, "ts": int(datetime.now(timezone.utc).timestamp())}
    if meta:
        row["meta"] = meta
    edges.append(row)
    # Keep last 500 edges only.
    if len(edges) > 500:
        del edges[:-500]


def _slim_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Strip large fields from action payloads before storing."""
    keep: dict[str, Any] = {"kind": kind}
    for k in ("file", "query", "mode", "pattern", "response_chars", "overlap",
              "hit_count", "hit_files", "searched_dirs"):
        if k in payload:
            keep[k] = payload[k]
    return keep


# v7.4: priority weights for action eviction. Higher weight = more important to retain.
_ACTION_WEIGHT = {
    "register_edit": 100,
    "read": 50,
    "continue_retrieve": 25,
    "continue_memory_first": 25,
    "retrieve": 15,
    "fallback_rg": 10,
    "impact": 8,
    "cross_search": 8,
    "action_summary": 5,
    "read_cache_hit": 3,
    "retrieve_cache_hit": 3,
    "read_blocked_allowlist": 1,
    "read_blocked_reuse_gate": 1,
    "fallback_blocked_limit": 1,
}


def _action_importance(action: dict[str, Any], now_ts: int) -> float:
    """v7.4: score an action by kind-weight × time-decay (half-life 1 day)."""
    kind = str(action.get("kind", ""))
    base = _ACTION_WEIGHT.get(kind, 5)
    age_sec = max(0, now_ts - int(action.get("ts", 0) or 0))
    decay = 2.0 ** (-age_sec / 86400.0)
    return base * decay


def _record_action(kind: str, payload: dict[str, Any]) -> None:
    g = _load_action_graph()
    actions = g.setdefault("actions", [])
    # Store only slim metadata — never full file content.
    actions.append({"ts": int(datetime.now(timezone.utc).timestamp()), "kind": kind, "payload": _slim_payload(kind, payload)})
    # v7.4: priority-queue eviction. Keep the highest-importance actions, not just the most recent.
    # Preserves edits + meaningful reads even when the log is saturated with cache_hits.
    if len(actions) > 300:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        scored = [(a, _action_importance(a, now_ts)) for a in actions]
        scored.sort(key=lambda x: -x[1])
        kept = [a for a, _ in scored[:300]]
        kept.sort(key=lambda a: int(a.get("ts", 0) or 0))
        g["actions"] = kept
    _save_action_graph(g)


def _write_gc_active() -> None:
    """Write gate file so PreToolUse hook allows native exploration."""
    try:
        gate = DG_DATA_DIR / ".gc_active"
        gate.write_text(str(int(time.time())))
    except OSError:
        pass


def _recent_action_evidence(files: list[str], window_sec: int = 120) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    g = _load_action_graph()
    actions = g.get("actions", [])
    bases = {f.split("::")[0] if "::" in f else f for f in files}
    recent_kinds: list[str] = []
    recent_queries: list[str] = []
    for a in reversed(actions):
        ts = int(a.get("ts", 0) or 0)
        if ts <= 0 or now - ts > window_sec:
            continue
        payload = a.get("payload", {})
        action_file = str(payload.get("file", ""))
        action_query = str(payload.get("query", "")).strip()
        action_base = action_file.split("::")[0] if "::" in action_file else action_file
        if action_file and action_base not in bases and action_file not in files:
            continue
        kind = str(a.get("kind", ""))
        if kind and kind not in recent_kinds:
            recent_kinds.append(kind)
        if action_query and action_query not in recent_queries:
            recent_queries.append(action_query)
    return {
        "window_sec": window_sec,
        "recent_action_kinds": recent_kinds[:6],
        "recent_queries": recent_queries[:3],
    }


def _recent_read_context(ref: str, window_sec: int = 600) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    g = _load_action_graph()
    actions = g.get("actions", [])
    base_ref = ref.split("::")[0] if "::" in ref else ref
    symbol_ref = ref if "::" in ref else ""
    read_count = 0
    co_visited: list[str] = []
    queries: list[str] = []
    for a in reversed(actions):
        ts = int(a.get("ts", 0) or 0)
        if ts <= 0 or now - ts > window_sec:
            continue
        if str(a.get("kind", "")) not in {"read", "read_cache_hit"}:
            continue
        payload = a.get("payload", {})
        action_file = str(payload.get("file", ""))
        action_base = action_file.split("::")[0] if "::" in action_file else action_file
        exact_match = bool(symbol_ref and action_file == symbol_ref)
        file_match = bool(not symbol_ref and action_base == base_ref)
        if exact_match or file_match:
            read_count += 1
            query = str(payload.get("query", "")).strip()
            if query:
                queries.append(query)
            continue
        if action_file and (not symbol_ref or action_base == base_ref):
            co_visited.append(action_file)
    return {
        "read_count": read_count,
        "co_visited": _merge_unique_strings(co_visited, limit=3),
        "queries": _merge_unique_strings(queries, limit=3),
    }


def _capture_read_memory(file: str, file_base: str, query: str, qterms: list[str], symbol_index: dict[str, Any] | None = None) -> None:
    idx = symbol_index if symbol_index is not None else _load_symbol_index()
    anchor_ref = file if "::" in file else file_base
    symbol_id, file_path, symbol_hash = _memory_anchor_for_ref(anchor_ref, idx)
    if not file_path:
        file_path = file_base
    query_text = query.strip()
    if query_text:
        _upsert_context_memory(
            {
                "kind": "query_association",
                "content": f"Queried during: {query_text}",
                "tags": qterms,
                "files": [anchor_ref],
                "file_path": file_path,
                "symbol_id": symbol_id,
                "symbol_hash": symbol_hash,
                "created_at": _now_iso(),
                "created_epoch": int(datetime.now(timezone.utc).timestamp()),
                "updated_at": _now_iso(),
                "stale": False,
                "stale_reason": "",
                "observed_queries": [query_text],
                "evidence": {
                    "source": "graph_read",
                    "anchor_mode": "symbol" if symbol_id else "file",
                    "session_epoch": _SESSION_START_EPOCH,
                },
            },
            idx,
        )
    read_context = _recent_read_context(anchor_ref)
    if int(read_context.get("read_count", 0)) >= 2:
        co_visited = list(read_context.get("co_visited", []))
        content = f"Frequently read with: {', '.join(co_visited)}" if co_visited else "Repeatedly visited during recent work"
        _upsert_context_memory(
            {
                "kind": "interaction_pattern",
                "content": content,
                "tags": qterms,
                "files": [anchor_ref],
                "file_path": file_path,
                "symbol_id": symbol_id,
                "symbol_hash": symbol_hash,
                "created_at": _now_iso(),
                "created_epoch": int(datetime.now(timezone.utc).timestamp()),
                "updated_at": _now_iso(),
                "stale": False,
                "stale_reason": "",
                "observed_queries": list(read_context.get("queries", [])),
                "evidence": {
                    "source": "graph_read",
                    "read_count": int(read_context.get("read_count", 0)),
                    "co_visited": co_visited,
                },
            },
            idx,
        )


def _load_retrieval_cache() -> dict[str, Any]:
    if not RETRIEVAL_CACHE_FILE.exists():
        return {"entries": {}}
    try:
        data = json.loads(RETRIEVAL_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"entries": {}}
        if "entries" not in data or not isinstance(data["entries"], dict):
            data["entries"] = {}
        # Eagerly evict expired entries on load so file never grows stale.
        now = int(datetime.now(timezone.utc).timestamp())
        data["entries"] = {
            k: v for k, v in data["entries"].items()
            if isinstance(v, dict) and now - int(v.get("created_epoch", 0)) <= RETRIEVE_CACHE_TTL_SEC
        }
        return data
    except Exception:
        return {"entries": {}}


def _save_retrieval_cache(cache: dict[str, Any]) -> None:
    RETRIEVAL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RETRIEVAL_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _cache_key(query: str, top_files: int, top_edges: int) -> str:
    qk = _query_key(query)
    return f"{qk}|tf={top_files}|te={top_edges}"


def _file_mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except Exception:
        return -1


def _retrieval_entry_valid(entry: dict[str, Any]) -> bool:
    if not isinstance(entry, dict):
        return False
    created = int(entry.get("created_epoch", 0) or 0)
    if created <= 0:
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    if now - created > RETRIEVE_CACHE_TTL_SEC:
        return False
    files = entry.get("files", [])
    mtimes = entry.get("mtimes_ns", {})
    if not isinstance(files, list) or not isinstance(mtimes, dict):
        return False
    for rel in files:
        if not isinstance(rel, str) or not rel:
            return False
        # Symbol IDs (file::symbol) → check mtime of the base file on disk.
        file_path = rel.split("::")[0] if "::" in rel else rel
        p = (PROJECT_ROOT / file_path).resolve()
        current = _file_mtime_ns(p)
        if current < 0:
            return False
        if int(mtimes.get(rel, -2)) != current:
            return False
    return True


def _invalidate_retrieval_cache_for_files(changed_files: list[str]) -> int:
    cache = _load_retrieval_cache()
    entries = cache.get("entries", {})
    if not isinstance(entries, dict):
        return 0
    changed = set(changed_files)
    # Also match symbol IDs whose base file is in the changed set.
    changed_bases = {f.split("::")[0] if "::" in f else f for f in changed_files}
    kill_keys = []
    for key, ent in entries.items():
        files = ent.get("files", [])
        if not isinstance(files, list):
            continue
        if any(f in changed or (f.split("::")[0] if "::" in f else f) in changed_bases for f in files):
            kill_keys.append(key)
    for k in kill_keys:
        entries.pop(k, None)
    cache["entries"] = entries
    _save_retrieval_cache(cache)
    return len(kill_keys)


def _search_action_history(query: str, limit: int = 10) -> dict[str, Any]:
    g = _load_action_graph()
    qterms = set(_query_terms(query))
    actions = g.get("actions", [])
    files_meta = g.get("files", {})

    # Recent relevant actions — return only slim metadata, never full payload.
    action_hits: list[dict[str, Any]] = []
    for a in reversed(actions):
        payload = a.get("payload", {})
        # Score against slim payload only (already stripped of large content).
        blob = " ".join(str(v) for v in payload.values()).lower()
        overlap = sum(1 for t in qterms if t in blob)
        if overlap <= 0 and qterms:
            continue
        action_hits.append({"kind": a.get("kind"), "file": payload.get("file", ""), "overlap": overlap})
        if len(action_hits) >= limit:
            break

    # Relevant cached files — slim response, no cached_content.
    file_hits: list[dict[str, Any]] = []
    for file, meta in files_meta.items():
        cached_terms = set(meta.get("query_terms", []))
        overlap = len(qterms & cached_terms) if qterms else 0
        if qterms and overlap <= 0:
            continue
        file_hits.append({
            "file": file,
            "overlap": overlap,
            "edited_count": int(meta.get("edited_count", 0)),
        })
    file_hits.sort(key=lambda x: (-x["overlap"], -x["edited_count"], x["file"]))
    return {"action_hits": action_hits, "file_hits": file_hits[:limit]}


def build_server(host: str = "127.0.0.1", port: int = 8080) -> Any:
    if FastMCP is None:
        raise RuntimeError(
            "Missing dependency: 'mcp'. Install with: python3 -m pip install mcp"
        )

    _POLICY = (
        "You are working with the GrapeRoot dual-graph context engine. "
        "HARD RULES — follow these exactly every turn:\n"
        "1. Call graph_continue FIRST — before any Bash, Read, grep, rg, find, or file exploration. No exceptions.\n"
        "2. Read only the files returned in graph_continue's recommended_files, using graph_read (not Read or Bash).\n"
        "3. Use fallback_rg instead of Bash grep or rg for any pattern search.\n"
        "4. Obey the confidence cap: confidence=high means stop exploring entirely. "
        "confidence=medium allows at most max_supplementary_greps greps and max_supplementary_files reads. "
        "confidence=low allows the same caps.\n"
        "5. Never use Bash/Read/grep to explore the codebase. graph_continue, graph_read, and fallback_rg "
        "are the only permitted exploration tools. If you find yourself about to run cat, find, ls, or rg "
        "to understand code — stop and call graph_continue instead.\n"
        "6. After editing files, call graph_register_edit with the changed file paths.\n"
        "These rules exist to save tokens and give you better context than raw file reads would."
    )

    mcp = FastMCP(
        "dual-graph-mcp",
        instructions=_POLICY,
        host=host, port=port,
        stateless_http=True, json_response=True,
    )

    @mcp.tool()
    def graph_retrieve(query: str, top_files: int = 5, top_edges: int = 12) -> dict[str, Any]:
        """Internal retrieval — prefer graph_continue."""
        qk = _query_key(query)
        # New query starts a new adaptive turn budget.
        if qk and qk != TURN_STATE.get("query_key", ""):
            TURN_STATE["query_key"] = qk
            TURN_STATE["used_chars"] = 0
            TURN_STATE["seen_reads"] = {}
            TURN_STATE["reuse_gate_candidates"] = []
            TURN_STATE["reuse_gate_satisfied"] = False
            TURN_STATE["retrieved_files"] = []
            TURN_STATE["retrieve_count"] = 0
            TURN_STATE["last_retrieve_out"] = None
            TURN_STATE["fallback_calls"] = 0
            TURN_STATE["grep_all_calls"] = 0
            # Note: graph_continue_called is NOT reset here — only graph_continue sets it.
            # graph_retrieve is an internal function; resetting gc_called here would break
            # the gate when graph_continue calls graph_retrieve as part of its own flow.
        # Avoid repeated retrieval cycles for the same turn unless query changed.
        if ENFORCE_SINGLE_RETRIEVE and qk and qk == TURN_STATE.get("query_key", "") and int(TURN_STATE.get("retrieve_count", 0)) >= 1:
            cached = dict(TURN_STATE.get("last_retrieve_out") or {})
            if cached:
                cached["single_retrieve_reused"] = True
                return cached
        # Persistent retrieval cache (cross-turn) with safe invalidation.
        ck = _cache_key(query, top_files, top_edges)
        rcache = _load_retrieval_cache()
        cent = rcache.get("entries", {}).get(ck)
        if cent and _retrieval_entry_valid(cent):
            out = dict(cent.get("result", {}))
            out["retrieval_cache_hit"] = True
            out["retrieval_cache_key"] = ck
            TURN_STATE["retrieved_files"] = [str(f.get("id", "")) for f in out.get("graph_files", []) if str(f.get("id", ""))]
            TURN_STATE["last_retrieve_out"] = dict(out)
            TURN_STATE["retrieve_count"] = int(TURN_STATE.get("retrieve_count", 0)) + 1
            _log_tool("graph_retrieve", {"query": query, "top_files": top_files, "top_edges": top_edges, "mode": "retrieval_cache_hit"})
            _record_action("retrieve_cache_hit", {"query": query, "cache_key": ck})
            return out
        _log_tool("graph_retrieve", {"query": query, "top_files": top_files, "top_edges": top_edges})
        _record_action("retrieve", {"query": query, "top_files": top_files, "top_edges": top_edges})
        out = _local_chat_fix(query, top_files, top_edges) or _post(
            "/api/chat-fix",
            {"query": query, "top_files": top_files, "top_edges": top_edges, "max_grep_hits": 0},
        )
        # Action graph update + cache hints
        g = _load_action_graph()
        qid = f"query:{_query_key(query)}"
        _ensure_node(g, qid, "query", {"text": query})
        for f in out.get("graph_files", [])[: max(1, top_files)]:
            fid = str(f.get("id", ""))
            if not fid:
                continue
            _ensure_node(g, fid, "file")
            _add_edge(g, qid, fid, "retrieved")
        TURN_STATE["retrieved_files"] = [str(f.get("id", "")) for f in out.get("graph_files", [])[: max(1, top_files)] if str(f.get("id", ""))]
        _save_action_graph(g)

        files_meta = g.get("files", {})
        qterms = set(_query_terms(query))
        reuse: list[dict[str, Any]] = []
        for file, meta in files_meta.items():
            cached_terms = set(meta.get("query_terms", []))
            overlap = len(qterms & cached_terms)
            if overlap <= 0:
                continue
            reuse.append(
                {
                    "file": file,
                    "overlap": overlap,
                    "cached_chars": int(meta.get("cached_chars", 0)),
                    "cached_tokens_est": int(meta.get("cached_tokens_est", 0)),
                    "last_action": meta.get("last_action", ""),
                }
            )
        reuse.sort(key=lambda x: (-x["overlap"], -x["cached_tokens_est"], x["file"]))
        out["reuse_candidates"] = reuse[:3]
        TURN_STATE["reuse_gate_candidates"] = [r["file"] for r in reuse[:3]]
        TURN_STATE["reuse_gate_satisfied"] = False
        out["read_budget"] = {
            "remaining_chars": max(0, TURN_READ_BUDGET_CHARS - int(TURN_STATE.get("used_chars", 0))),
            "reuse_gate_candidates": TURN_STATE.get("reuse_gate_candidates", []),
        }
        TURN_STATE["retrieve_count"] = int(TURN_STATE.get("retrieve_count", 0)) + 1
        TURN_STATE["last_retrieve_out"] = dict(out)
        # Save retrieval cache with mtime stamps of returned files.
        rel_files = [str(f.get("id", "")) for f in out.get("graph_files", []) if str(f.get("id", ""))]
        mtimes: dict[str, int] = {}
        for rel in rel_files:
            # Symbol IDs (file::symbol) → stat the base file on disk.
            file_path = rel.split("::")[0] if "::" in rel else rel
            mtimes[rel] = _file_mtime_ns((PROJECT_ROOT / file_path).resolve())
        entries = rcache.get("entries", {})
        if not isinstance(entries, dict):
            entries = {}
        entries[ck] = {
            "created_epoch": int(datetime.now(timezone.utc).timestamp()),
            "files": rel_files,        # only file IDs, not full objects
            "mtimes_ns": mtimes,
            "result": out,
        }
        # Bound cache size to 50 entries (down from 200).
        if len(entries) > 50:
            items = sorted(entries.items(), key=lambda kv: int(kv[1].get("created_epoch", 0)))
            for old_key, _ in items[:-50]:
                entries.pop(old_key, None)
        rcache["entries"] = entries
        _save_retrieval_cache(rcache)
        return out

    @mcp.tool()
    def graph_read(file: str, max_chars: int = 20000, query: str = "", anchor: str = "") -> dict[str, Any]:
        """Read a file recommended by graph_continue. Use instead of Bash/Read.
        Accepts file::symbol notation (e.g. src/auth.ts::handleLogin) to read only that symbol.
        Only call this for files returned in graph_continue recommended_files."""
        if not TURN_STATE.get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first to get recommended files, then call graph_read.",
                "action_required": "graph_continue",
            }
        requested = max(256, int(max_chars or 0))
        effective_hard_max = int(TURN_STATE.get("explore_hard_max_chars", HARD_MAX_READ_CHARS))
        max_chars = min(requested, effective_hard_max)
        qterms = _query_terms(query)
        # v7.3: If caller passed a bare path but graph_continue recommended it as file::symbol,
        # automatically upgrade to the symbol version BEFORE any cache/gate checks.
        if "::" not in file:
            _sym_map = TURN_STATE.get("recommended_sym_map", {})
            if file in _sym_map:
                file = _sym_map[file]
        # For file::symbol notation, also accept the base file in the allowlist.
        file_base = file.split("::")[0] if "::" in file else file
        retrieved_files = set(TURN_STATE.get("retrieved_files", []))
        if ENFORCE_READ_ALLOWLIST and retrieved_files and file not in retrieved_files and file_base not in retrieved_files:
            payload = {
                "file": file,
                "requested_chars": requested,
                "granted_chars": 0,
                "query": query,
                "anchor": anchor,
                "mode": "allowlist_blocked",
                "retrieved_files": sorted(retrieved_files),
            }
            _log_tool("graph_read", payload)
            _record_action("read_blocked_allowlist", payload)
            return {
                "ok": False,
                "error": "file not in retrieved allowlist; call graph_retrieve with broader top_files first",
                "retrieved_files": sorted(retrieved_files),
            }
        g = _load_action_graph()
        files_meta = g.setdefault("files", {})
        meta = files_meta.get(file, {})
        gate_candidates = list(TURN_STATE.get("reuse_gate_candidates", []))
        gate_satisfied = bool(TURN_STATE.get("reuse_gate_satisfied", False))
        if ENFORCE_REUSE_GATE and gate_candidates and not gate_satisfied and file not in gate_candidates and file_base not in gate_candidates:
            payload = {
                "file": file,
                "requested_chars": requested,
                "granted_chars": 0,
                "query": query,
                "anchor": anchor,
                "mode": "reuse_gate_blocked",
                "reuse_gate_candidates": gate_candidates,
            }
            _log_tool("graph_read", payload)
            _record_action("read_blocked_reuse_gate", payload)
            return {
                "ok": False,
                "error": "reuse gate active: read one reuse candidate first",
                "reuse_gate_candidates": gate_candidates,
            }
        # v7.4: Cross-turn dedup — if this file was read in a prior turn, return a POINTER
        # instead of the full cached content. The caller already has the content in their
        # session history; re-returning it just re-bills cache tokens every turn.
        if meta and set(meta.get("query_terms", [])) & set(qterms) and meta.get("cached_content"):
            cached_len = int(meta.get("cached_chars", 0) or len(str(meta.get("cached_content", ""))))
            prior_turn = int(meta.get("first_read_turn", 0) or 0)
            payload = {
                "file": file,
                "requested_chars": requested,
                "granted_chars": 0,
                "query": query,
                "anchor": anchor,
                "mode": "cross_turn_pointer",
                "response_chars": 0,
                "full_file_chars": cached_len,
                "full_file_tokens_est": cached_len // 4,
                "tokens_avoided": cached_len // 4,
                "cached_len": cached_len,
                "prior_turn": prior_turn,
            }
            _log_tool("graph_read", payload)
            _record_action("read_cache_hit", payload)
            if file in gate_candidates:
                TURN_STATE["reuse_gate_satisfied"] = True
            _capture_read_memory(file, file_base, query, qterms)
            return {
                "ok": True,
                "file": file,
                "mode": "cross_turn_pointer",
                "note": f"you already read this file in an earlier turn of this session ({cached_len} chars). Refer to that excerpt instead of re-reading.",
                "from_action_graph": True,
            }

        used = int(TURN_STATE.get("used_chars", 0))
        effective_budget = int(TURN_STATE.get("explore_budget_chars", TURN_READ_BUDGET_CHARS))
        remaining = max(0, effective_budget - used)
        if remaining <= 0:
            payload = {
                "file": file,
                "requested_chars": requested,
                "granted_chars": 0,
                "query": query,
                "anchor": anchor,
                "mode": "budget_exhausted",
                "response_chars": 0,
                "response_tokens_est": 0,
                "full_file_chars": requested,
                "full_file_tokens_est": requested // 4,
                "tokens_avoided": requested // 4,
            }
            _log_tool("graph_read", payload)
            return {
                "ok": False,
                "error": "turn read budget exhausted; call graph_retrieve again or use fallback_rg",
                "budget": {
                    "used_chars": used,
                    "remaining_chars": remaining,
                    "turn_read_budget_chars": effective_budget,
                },
            }

        granted = min(max_chars, remaining)
        # Dedupe: prevent re-reading the exact same content in the same turn.
        # Key includes max_chars so requesting MORE data bypasses the dedupe.
        # Bare file reads bypass dedupe if previous read was a symbol excerpt.
        dedupe_key = f"{file}|{_query_key(query)}|{anchor.lower()}"
        seen = TURN_STATE.get("seen_reads", {})
        prev_entry = seen.get(dedupe_key)
        if prev_entry:
            prev_content = str(prev_entry.get("content", ""))
            prev_mode = str(prev_entry.get("mode", ""))
            prev_chars = int(prev_entry.get("chars", 0))
            # Allow re-read if: requesting more chars, or previous was a symbol excerpt
            # and now asking for full file (larger max_chars)
            is_upgrade = max_chars > prev_chars or (
                prev_mode in ("symbol_excerpt", "auto_symbol_excerpt") and "::" not in file
            )
            if not is_upgrade:
                preview = prev_content[:500]
                full_chars_est = prev_chars or len(prev_content)
                payload = {
                    "file": file,
                    "requested_chars": requested,
                    "granted_chars": min(500, granted),
                    "query": query,
                    "anchor": anchor,
                    "mode": "dedupe_preview",
                    "response_chars": len(preview),
                    "response_tokens_est": _est_tokens(preview),
                    "full_file_chars": full_chars_est,
                    "full_file_tokens_est": full_chars_est // 4,
                    "tokens_avoided": max(0, full_chars_est - len(preview)) // 4,
                }
                _log_tool("graph_read", payload)
                return {"ok": True, "file": file, "content": preview, "mode": "dedupe_preview", "already_returned": True}

        # Handle file::symbol notation — O(1) lookup via symbol index.
        sym_meta = None
        file_for_fs = file
        if "::" in file:
            file_for_fs, _ = file.split("::", 1)
            sym_meta = _load_symbol_index().get(file)

        tgt = (PROJECT_ROOT / file_for_fs).resolve()
        if PROJECT_ROOT not in tgt.parents and tgt != PROJECT_ROOT:
            return {"ok": False, "error": "outside project root"}
        if not tgt.exists() or not tgt.is_file():
            # Remote/Railway mode: file is not on this server's disk.
            # Fall back to content uploaded with the graph.
            graph_json = DG_DATA_DIR / "info_graph.json"
            text = None
            if graph_json.exists():
                try:
                    gdata = json.loads(graph_json.read_text(encoding="utf-8"))
                    for node in gdata.get("nodes", []):
                        if node.get("path") == file_for_fs and node.get("content"):
                            text = node["content"]
                            break
                except Exception:
                    pass
            if not text:
                return {"ok": False, "error": "file not found", "file": file}
        else:
            text = tgt.read_text(encoding="utf-8", errors="ignore")
        full_file_chars = len(text)  # capture BEFORE any truncation/excerpting
        mode = "full"
        sym_stale = False
        # v7.3: auto-promote bare file reads to symbol_excerpt using current turn query.
        # If the caller passed a bare path (no ::) but we have a query in TURN_STATE,
        # find the best-scoring symbol in this file and extract only those lines.
        # v7.5.1: Skip auto-promote if this file was already read as a symbol excerpt —
        # Claude is explicitly asking for the full file because the excerpt was insufficient.
        _already_symbol_read = any(
            e.get("mode") in ("symbol_excerpt", "auto_symbol_excerpt")
            for k, e in seen.items()
            if isinstance(e, dict) and k.startswith(file.split("::")[0])
        )
        if not sym_meta and not anchor and "::" not in file and not _already_symbol_read:
            _cur_query = str(TURN_STATE.get("current_query", ""))
            if _cur_query:
                _promoted = _resolve_to_symbols([file], _cur_query)
                if _promoted and "::" in _promoted[0]:
                    _promoted_id = _promoted[0]
                    sym_meta = _load_symbol_index().get(_promoted_id)
                    if sym_meta:
                        mode = "auto_symbol_excerpt"
        # If symbol notation matched (explicit or auto-promoted), extract only the symbol's lines.
        if sym_meta:
            _lines = text.splitlines()
            _start = int(sym_meta.get("line_start", 0))
            _end = min(int(sym_meta.get("line_end", len(_lines) - 1)), len(_lines) - 1)
            # v7.5.1: If index range is too small (< 5 lines), expand to capture full body.
            # This handles cases where the index only stored the signature.
            if _end - _start < 5 and _start < len(_lines):
                # Find the end of the function/block by tracking indentation or braces
                base_indent = len(_lines[_start]) - len(_lines[_start].lstrip()) if _lines[_start].strip() else 0
                expanded_end = _start
                brace_depth = 0
                for li in range(_start, min(len(_lines), _start + 200)):
                    line = _lines[li]
                    brace_depth += line.count("{") - line.count("}")
                    # For Python: stop at next line with same or less indent (not empty)
                    if li > _start + 1 and line.strip() and not line.strip().startswith(("#", "//", "/*")):
                        cur_indent = len(line) - len(line.lstrip())
                        if cur_indent <= base_indent and brace_depth <= 0:
                            break
                    expanded_end = li
                if expanded_end > _end:
                    _end = expanded_end
            text = "\n".join(_lines[_start:_end + 1])
            mode = "symbol_excerpt"
            # Staleness check: compare body hash against current file content.
            stored_hash = sym_meta.get("body_hash", "")
            if stored_hash:
                current_hash = hashlib.md5(text.encode()).hexdigest()[:8]
                sym_stale = current_hash != stored_hash
        if anchor:
            full_text = tgt.read_text(encoding="utf-8", errors="ignore") if tgt.exists() else text
            full_file_chars = len(full_text)
            i = full_text.lower().find(anchor.lower())
            if i >= 0:
                start = max(0, i - granted // 2)
                end = min(len(full_text), i + granted // 2)
                text = full_text[start:end]
                mode = "anchor_excerpt"
            else:
                text = text[:granted]
                mode = "anchor_fallback_head"
        elif len(text) > granted:
            terms = _query_terms(query)
            text = _excerpt_by_terms(text, terms, granted)
            mode = "query_excerpt" if query else "head"
        TURN_STATE["used_chars"] = used + len(text)
        # v6: accumulate total chars read across all turns for cost budget
        TURN_STATE["total_read_chars"] = int(TURN_STATE.get("total_read_chars", 0)) + len(text)
        # Store dedupe metadata so upgraded re-reads can bypass.
        seen[dedupe_key] = {"content": text[:200], "mode": mode, "chars": len(text)}
        if len(seen) > 20:
            oldest = next(iter(seen))
            del seen[oldest]
        TURN_STATE["seen_reads"] = seen
        payload = {
            "file": file,
            "requested_chars": requested,
            "granted_chars": granted,
            "query": query,
            "anchor": anchor,
            "mode": mode,
            "response_chars": len(text),
            "response_tokens_est": _est_tokens(text),
            "full_file_chars": full_file_chars,
            "full_file_tokens_est": _est_tokens("x" * full_file_chars),
            "tokens_avoided": max(0, full_file_chars - len(text)) // 4,
            "budget_used_chars": int(TURN_STATE.get("used_chars", 0)),
            "budget_remaining_chars": max(0, TURN_READ_BUDGET_CHARS - int(TURN_STATE.get("used_chars", 0))),
        }
        _log_tool("graph_read", payload)
        TURN_STATE["turn_tokens_served"] = int(TURN_STATE.get("turn_tokens_served", 0)) + _est_tokens(text)
        TURN_STATE["turn_tokens_avoided"] = int(TURN_STATE.get("turn_tokens_avoided", 0)) + max(0, full_file_chars - len(text)) // 4
        TURN_STATE["turn_full_file_tokens"] = int(TURN_STATE.get("turn_full_file_tokens", 0)) + full_file_chars // 4
        hits = TURN_STATE.get("turn_tool_hits", {})
        hits[mode] = hits.get(mode, 0) + 1
        TURN_STATE["turn_tool_hits"] = hits
        # Inline action append — do NOT call _record_action() here because `g` was loaded
        # at the top of this function and gets saved at the end. _record_action does its own
        # load/save cycle which would be overwritten by our _save_action_graph(g) below.
        actions = g.setdefault("actions", [])
        actions.append({"ts": int(datetime.now(timezone.utc).timestamp()), "kind": "read", "payload": _slim_payload("read", payload)})
        if len(actions) > 300:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            scored = [(a, _action_importance(a, now_ts)) for a in actions]
            scored.sort(key=lambda x: -x[1])
            kept = [a for a, _ in scored[:300]]
            kept.sort(key=lambda a: int(a.get("ts", 0) or 0))
            g["actions"] = kept
        # Persist file cache + graph edges.
        qid = f"query:{_query_key(query)}" if query else "query:(empty)"
        _ensure_node(g, qid, "query", {"text": query})
        _ensure_node(g, file, "file")
        _add_edge(g, qid, file, "read", {"mode": mode})
        # v7.4: track the turn on which this file was first read, so cross-turn dedup can
        # tell Claude "you already read this in turn N" rather than re-sending the content.
        prev_first_turn = int(files_meta.get(file, {}).get("first_read_turn", 0) or 0)
        files_meta[file] = {
            "query_terms": qterms,
            "cached_content": text[:300],          # cap to 300 chars — just enough for context hints
            "cached_chars": len(text),
            "cached_tokens_est": _est_tokens(text),
            "last_action": "read",
            "last_ts": int(datetime.now(timezone.utc).timestamp()),
            "first_read_turn": prev_first_turn or int(TURN_STATE.get("turn_count", 0)),
        }
        _save_action_graph(g)
        _capture_read_memory(file, file_base, query, qterms)
        resp: dict[str, Any] = {"ok": True, "file": file, "content": text, "mode": mode}
        if sym_stale:
            resp["stale"] = True
            resp["stale_reason"] = "symbol body changed since last graph scan — consider running graph_scan again"
        return resp

    @mcp.tool()
    def graph_neighbors(file: str, limit: int = 30) -> dict[str, Any]:
        """Return graph edges (imports/importers) for a file. Call after graph_continue."""
        if not TURN_STATE.get("graph_continue_called"):
            return {"ok": False, "error": "Call graph_continue first.", "action_required": "graph_continue"}
        _log_tool("graph_neighbors", {"file": file, "limit": limit})
        g = _local_info_graph() or _get("/api/info-graph?full=1")
        edges = g.get("edges", [])
        out = []
        for edge in edges:
            if edge.get("from") == file or edge.get("to") == file:
                out.append(edge)
            if len(out) >= limit:
                break
        return {"ok": True, "file": file, "neighbors": out}

    @mcp.tool()
    def graph_impact(changed_files: list[str]) -> dict[str, Any]:
        """Return files impacted by edits to changed_files. Call after graph_continue."""
        if not TURN_STATE.get("graph_continue_called"):
            return {"ok": False, "error": "Call graph_continue first.", "action_required": "graph_continue"}
        _log_tool("graph_impact", {"changed_files": changed_files})
        _record_action("impact", {"changed_files": changed_files})
        g = _local_info_graph() or _get("/api/info-graph?full=1")
        edges = g.get("edges", [])
        changed = set(changed_files)
        connected: set[str] = set()
        for edge in edges:
            frm = str(edge.get("from", ""))
            to = str(edge.get("to", ""))
            if frm in changed and _is_local_file_ref(to):
                connected.add(to)
            if to in changed and _is_local_file_ref(frm):
                connected.add(frm)
        return {
            "ok": True,
            "changed_files": sorted(changed),
            "connected_files": sorted(connected),
            "untouched_connected_files": sorted(x for x in connected if x not in changed),
        }

    @mcp.tool()
    def graph_register_edit(files: Union[str, list[str]], summary: str = "") -> dict[str, Any]:
        """Register edited files into in-chat action graph memory."""
        if isinstance(files, str):
            files = [files]
        # Global decision window: max verbatim entries before compression.
        # Stored ONCE per edit call (not per-file) to avoid N-file duplication.
        _DECISION_WINDOW = 20
        _DECISION_ARCHIVE_CAP = 300  # chars for compressed older decisions

        g = _load_action_graph()
        aid = f"action:edit:{int(datetime.now(timezone.utc).timestamp())}"
        _ensure_node(g, aid, "action", {"summary": summary})
        files_meta = g.setdefault("files", {})
        for f in files:
            _ensure_node(g, f, "file")
            _add_edge(g, aid, f, "edited")
            meta = files_meta.get(f, {})
            meta["last_action"] = "edited"
            meta["last_ts"] = _now_iso()
            meta["edited_count"] = int(meta.get("edited_count", 0)) + 1
            files_meta[f] = meta

        # ── Global decisions log (rolling window, stored once per edit) ───────
        # One entry per graph_register_edit call regardless of how many files.
        # This prevents N-file duplication and keeps the budget fixed.
        if summary:
            decisions: list[dict[str, Any]] = g.setdefault("decisions", [])
            decisions.append({
                "ts": int(datetime.now(timezone.utc).timestamp()),
                "summary": summary,
                "files": files,                     # which files this decision touched
                "scope": "global" if len(files) > 1 else "file",
            })
            if len(decisions) > _DECISION_WINDOW:
                overflow = decisions[:-_DECISION_WINDOW]
                g["decisions"] = decisions[-_DECISION_WINDOW:]
                # Compress overflow into archive string (rolling summary of old decisions).
                old_archive = g.get("decisions_archive", "")
                combined = "; ".join(d["summary"] for d in overflow)
                if old_archive:
                    combined = old_archive.rstrip(".") + "; " + combined
                g["decisions_archive"] = combined[:_DECISION_ARCHIVE_CAP]
            symbol_index = _load_symbol_index()
            symbol_id = ""
            file_path = ""
            symbol_hash = ""
            if files:
                symbol_id, file_path, symbol_hash = _memory_anchor_for_ref(str(files[0]), symbol_index)
                if not file_path:
                    file_path = str(files[0]).split("::")[0]
            evidence = _recent_action_evidence([str(f) for f in files])
            _upsert_context_memory(
                {
                    "id": _memory_id(),
                    "kind": "decision",
                    "content": summary,
                    "tags": _query_terms(summary),
                    "files": [str(f) for f in files],
                    "file_path": file_path,
                    "symbol_id": symbol_id,
                    "symbol_hash": symbol_hash,
                    "created_at": _now_iso(),
                    "created_epoch": int(datetime.now(timezone.utc).timestamp()),
                    "updated_at": _now_iso(),
                    "stale": False,
                    "stale_reason": "",
                    "observed_queries": evidence.get("recent_queries", []),
                    "evidence": evidence,
                },
                symbol_index,
            )
        symbol_index = _load_symbol_index()
        for f in files:
            symbol_id, file_path, symbol_hash = _memory_anchor_for_ref(str(f), symbol_index)
            if not file_path:
                file_path = str(f).split("::")[0]
            evidence = _recent_action_evidence([str(f)])
            recent_queries = list(evidence.get("recent_queries", []))
            content = f"Edited after query: {recent_queries[0]}" if recent_queries else "Edited during active session"
            _upsert_context_memory(
                {
                    "kind": "edit_observation",
                    "content": content,
                    "tags": _query_terms(summary or content),
                    "files": [str(f)],
                    "file_path": file_path,
                    "symbol_id": symbol_id,
                    "symbol_hash": symbol_hash,
                    "created_at": _now_iso(),
                    "created_epoch": int(datetime.now(timezone.utc).timestamp()),
                    "updated_at": _now_iso(),
                    "stale": False,
                    "stale_reason": "",
                    "observed_queries": recent_queries,
                    "evidence": {
                        "source": "graph_register_edit",
                        "recent_action_kinds": evidence.get("recent_action_kinds", []),
                        "recent_queries": recent_queries,
                        "edited": True,
                    },
                },
                symbol_index,
            )
        # ─────────────────────────────────────────────────────────────────────

        # Detect first-ever edit to surface "graph primed" signal
        prior_edit_count = sum(
            int(g.get("files", {}).get(f, {}).get("edited_count", 0)) for f in g.get("files", {})
        )
        _save_action_graph(g)
        payload = {"files": files, "summary": summary}
        invalidated = _invalidate_retrieval_cache_for_files(files)
        _log_tool("graph_register_edit", payload)
        _record_action("register_edit", {"files": files, "summary": summary, "retrieval_cache_invalidated": invalidated})
        result: dict[str, Any] = {"ok": True, "edited_files": files, "count": len(files), "retrieval_cache_invalidated": invalidated}
        if prior_edit_count == 0:
            result["graph_state"] = "primed"
            result["graph_state_reason"] = "Structural signal established. Future turns will use memory_first routing."
        return result

    @mcp.tool()
    def graph_action_summary(query: str = "", limit: int = 12) -> dict[str, Any]:
        """Return recent action graph summary + query-relevant touched files."""
        g = _load_action_graph()
        actions = g.get("actions", [])
        files_meta = g.get("files", {})
        recent = actions[-limit:]
        qterms = set(_query_terms(query))
        relevant = []
        for file, meta in files_meta.items():
            overlap = len(qterms & set(meta.get("query_terms", [])))
            if query and overlap <= 0:
                continue
            relevant.append({
                "file": file,
                "overlap": overlap,
                "last_action": meta.get("last_action", ""),
                "edited_count": int(meta.get("edited_count", 0)),
                "cached_tokens_est": int(meta.get("cached_tokens_est", 0)),
                "last_ts": meta.get("last_ts", ""),
            })
        relevant.sort(key=lambda x: (-x["overlap"], -x["edited_count"], -x["cached_tokens_est"], x["file"]))

        # ── Global decisions log ──────────────────────────────────────────────
        # Return only decisions relevant to the current query (or last 5 if no query).
        all_decisions: list[dict[str, Any]] = g.get("decisions", [])
        if query and qterms:
            file_set = {f["file"] for f in relevant}
            scored: list[tuple[int, dict[str, Any]]] = []
            for d in all_decisions:
                # Score by term overlap in summary + file intersection with relevant set.
                term_hits = sum(1 for t in qterms if t in d.get("summary", "").lower())
                file_hits = len(set(d.get("files", [])) & file_set)
                score = term_hits * 2 + file_hits
                if score > 0:
                    scored.append((score, d))
            scored.sort(key=lambda x: (-x[0], -x[1]["ts"]))
            shown_decisions = [d for _, d in scored[:5]]
        else:
            shown_decisions = all_decisions[-5:]  # most recent 5 when no query

        payload = {"query": query, "limit": limit}
        _log_tool("graph_action_summary", payload)
        _record_action("action_summary", payload)
        out: dict[str, Any] = {
            "ok": True,
            "recent_actions": recent,
            "relevant_files": relevant[:limit],
            "decisions": shown_decisions,
            "memories": _search_context_store(query, [f["file"] for f in relevant[:limit]], limit=5),
        }
        if g.get("decisions_archive"):
            out["decisions_archive"] = g["decisions_archive"]
        return out

    @mcp.tool()
    def graph_continue(query: str, top_files: int = 5, top_edges: int = 12, limit: int = 8) -> dict[str, Any]:
        """CALL THIS FIRST — before Read, Bash, grep, or any file exploration.
        Returns recommended_files to read via graph_read, and a confidence level.
        Do NOT use Bash/grep/Read before calling this. If needs_project=True, call graph_scan next."""
        # ── Project setup gate ────────────────────────────────────────────────
        # Only check the graph file — NOT PROJECT_ROOT.is_dir().
        # In the Railway upload model the project directory never exists on the
        # server; the graph arrives via POST /ingest-graph, so the graph file
        # is the only reliable signal that the project has been scanned.
        graph_json = DG_DATA_DIR / "info_graph.json"
        graph_missing = not graph_json.exists()
        graph_empty = False
        graph_stale_root = False
        gdata: dict[str, Any] = {}
        if not graph_missing:
            try:
                gdata = json.loads(graph_json.read_text(encoding="utf-8"))
                graph_empty = gdata.get("node_count", 0) == 0
                stored_root = gdata.get("root", "")
                if stored_root and str(PROJECT_ROOT) != stored_root:
                    graph_stale_root = True
                # Back-fill built_at for graphs scanned before this field was added.
                # Use the graph file's own mtime as a conservative lower bound.
                if not graph_empty and not graph_stale_root and not gdata.get("built_at"):
                    _mtime = int(graph_json.stat().st_mtime)
                    gdata["built_at"] = _mtime
                    try:
                        graph_json.write_text(json.dumps(gdata, indent=2), encoding="utf-8")
                    except OSError:
                        pass
            except Exception:
                graph_empty = True
        if graph_missing or graph_empty or graph_stale_root:
            # If scan already timed out this session, don't loop — tell Claude to skip
            if TURN_STATE.get("scan_timed_out"):
                _write_gc_active()
                TURN_STATE["graph_continue_called"] = True
                return {
                    "ok": True,
                    "skip": True,
                    "cold_start_reason": "scan_timeout",
                    "hint": "Graph scan timed out for this project. Use native tools (Read, Bash) — the gate allows them.",
                }
            gb_script = str(Path(__file__).resolve().parent / "graph_builder.py")
            ingest_url = f"{DG_BASE}/ingest-graph"
            resp: dict[str, Any] = {
                "ok": False,
                "needs_project": True,
                "query": query,
                "graph_builder": gb_script,
                "ingest_url": ingest_url,
            }
            if graph_stale_root:
                resp["reason"] = f"Graph was built for {stored_root} but current project is {PROJECT_ROOT}. Re-scan needed."
            return resp

        # ── Incremental graph update (first turn of session only) ─────────────
        _incremental_refreshed: list[str] = []
        _built_at = int(gdata.get("built_at") or 0)
        if _built_at > 0 and not TURN_STATE.get("incremental_done") and _gb_scan is not None:
            _inc_error: str = ""
            _inc_changed: list[str] = []
            try:
                _inc_changed = _detect_changed_files(PROJECT_ROOT, _built_at)
                if _inc_changed:
                    _updated = _incremental_update(PROJECT_ROOT, _inc_changed, gdata)
                    graph_json.write_text(json.dumps(_updated, indent=2), encoding="utf-8")
                    # Rebuild symbol index for the updated graph
                    sym_index = _build_symbol_index(_updated)
                    SYMBOL_INDEX_FILE.write_text(json.dumps(sym_index), encoding="utf-8")
                    gdata = _updated
                    _incremental_refreshed = _inc_changed
            except Exception as _exc:
                _inc_error = str(_exc)
            _log_tool("incremental_update", {
                "built_at": _built_at,
                "changed_files": _inc_changed,
                "refreshed": len(_incremental_refreshed),
                "error": _inc_error,
            })
            TURN_STATE["incremental_done"] = True

        # Phase 0: Handle truly empty/greenfield projects (< 5 files).
        # Projects with 5+ files get full graph treatment regardless of prior edits.
        file_count = gdata.get("file_count", gdata.get("node_count", 0))
        if file_count < 5:
            _DESIGN_TERMS = {
                "design", "create", "build", "plan", "architecture", "structure",
                "scaffold", "implement", "setup", "initialize", "how should", "what should",
                "start", "begin", "new", "project", "app", "service",
            }
            q_lower = query.lower()
            is_design_query = any(t in q_lower for t in _DESIGN_TERMS)
            if is_design_query:
                retrieved = graph_retrieve(query=query, top_files=top_files, top_edges=top_edges)
                graph_files = retrieved.get("graph_files", [])
                rec_files = [str(f.get("id", "")) for f in graph_files if str(f.get("id", ""))]
                return {
                    "ok": True,
                    "mode": "bootstrap_explore",
                    "note": "Greenfield project — allowing exploratory read to seed graph.",
                    "recommended_files": rec_files[: min(3, len(rec_files))],
                    "graph_edges": retrieved.get("graph_edges", []),
                }
            _write_gc_active()
            return {
                "ok": True,
                "skip": True,
                "cold_start_reason": "empty_project",
                "file_count": file_count,
                "hint": "Project has fewer than 5 files. Use targeted reads only — do NOT explore broadly.",
                "next_best_action": "Ask the user what they want to work on, or read a specific file if named.",
            }

        # v6: Task classification + turn tracking
        task_type = _classify_task_type(query)
        TURN_STATE["task_type"] = task_type
        TURN_STATE["turn_count"] = int(TURN_STATE.get("turn_count", 0)) + 1
        turn_count = TURN_STATE["turn_count"]
        TURN_STATE["graph_continue_called"] = True   # gate: allow graph_read/fallback_rg this turn
        _write_gc_active()
        # v7.2: Reset per-turn grep counter so exhaustive tasks get the full cap each turn.
        TURN_STATE["fallback_calls"] = 0

        # v6.1: Task-aware cost budget check
        total_chars = int(TURN_STATE.get("total_read_chars", 0))
        budget = _COST_BUDGETS.get(task_type, _COST_BUDGETS["unknown"])
        cost_guidance = None
        if total_chars >= budget["hard"]:
            cost_guidance = "COST_LIMIT_REACHED: You have read ~{:,} chars this session. Stop exploring and answer with what you have.".format(total_chars)
        elif total_chars >= budget["warn"]:
            cost_guidance = "COST_WARNING: {:,} chars read. Wrap up soon — avoid opening new files unless critical.".format(total_chars)

        # v6.1: Task-aware turn circuit breaker
        turns = _TURN_LIMITS.get(task_type, _TURN_LIMITS["unknown"])
        turn_guidance = None
        if turn_count >= turns["hard"]:
            turn_guidance = "TURN_LIMIT: {} turns used. Provide your answer now with available context.".format(turn_count)
        elif turn_count >= turns["warn"]:
            turn_guidance = "TURN_WARNING: {} turns used. Start wrapping up.".format(turn_count)

        # v6: Package hints — on first turn, extract package/module root from graph
        package_hints = None
        if turn_count == 1:
            try:
                nodes = gdata.get("nodes", [])
                pkg_dirs = set()
                for n in nodes[:200]:  # sample first 200 nodes
                    p = str(n.get("id", ""))
                    if "/" in p:
                        top = p.split("/")[0]
                        if top not in {".", "..", "node_modules", "vendor", "__pycache__", ".git"}:
                            pkg_dirs.add(top)
                if pkg_dirs:
                    package_hints = sorted(pkg_dirs)[:15]
            except Exception:
                pass

        hist = _search_action_history(query, limit=limit)
        file_hits = hist.get("file_hits", [])
        action_hits = hist.get("action_hits", [])

        # v7.2: For exhaustive tasks, pre-grep the application source dirs to build a
        # search_map BEFORE deciding memory_first vs retrieve_then_read. This runs on
        # every path so session history can't cause the map to be skipped.
        _pre_hit_dirs: dict[str, int] = {}
        _pre_hit_samples: dict[str, list[str]] = {}
        _pre_tried: list[str] = []
        if task_type == "exhaustive":
            import re as _re2
            _clean_q = _re2.sub(r'^\[task_type:\w+\]\s*', '', query, count=1)
            _stopwords_pre = {
                "find", "code", "where", "that", "with", "from", "this", "which",
                "have", "been", "look", "into", "also", "when", "what", "check",
                "make", "does", "will", "some", "just", "used", "over", "such",
                "even", "than", "more", "they", "their", "there", "function",
                "functions", "codebase", "source", "files", "call", "calls",
                "using", "every", "across", "after", "before", "without", "places",
                "patterns", "should", "could", "would", "exhaustive", "targeted",
                "task", "type", "openp", "papyrus",
            }
            _terms_pre = [
                w for w in _re2.findall(r'\b[A-Za-z_][A-Za-z_0-9]{2,}\b', _clean_q)
                if w.lower() not in _stopwords_pre and not w.lower().startswith("task_")
            ][:10]
            # Search only application source dirs — exclude vendored/generated code.
            _app_src_dirs = ["PPLib", "SLib", "PPEquip", "PPMain", "Tools"]
            _src_root = PROJECT_ROOT / "Src"
            for _term in _terms_pre:
                _all_files: list[str] = []
                for _app_dir in _app_src_dirs:
                    _search_path = _src_root / _app_dir
                    if not _search_path.exists():
                        continue
                    try:
                        _rg = subprocess.run(
                            ["rg", "--files-with-matches", "-i", _term, str(_search_path),
                             "--glob", "*.cpp", "--glob", "*.h", "--glob", "*.c"],
                            capture_output=True, text=True, timeout=6,
                        )
                        _all_files.extend(_rg.stdout.splitlines())
                    except Exception:
                        continue
                if _all_files:
                    _pre_tried.append(_term)
                    for _line in _all_files:
                        try:
                            _rel = str(Path(_line.strip()).relative_to(PROJECT_ROOT))
                        except ValueError:
                            continue
                        _parts = _rel.replace("\\", "/").split("/")
                        if len(_parts) >= 2:
                            _dk = f"{_parts[0]}/{_parts[1]}"
                            _pre_hit_dirs[_dk] = _pre_hit_dirs.get(_dk, 0) + 1
                            if _dk not in _pre_hit_samples:
                                _pre_hit_samples[_dk] = []
                            if len(_pre_hit_samples[_dk]) < 3:
                                _pre_hit_samples[_dk].append(_rel)
                if len(_pre_hit_dirs) >= 4:
                    break  # enough dirs mapped

        # If we already have relevant cached files from prior turns, prefer those first.
        def _access_type(file: str, files_meta: dict) -> str:
            """Classify a file as 'new', 'read', or 'write' based on action graph history."""
            meta = files_meta.get(file, {})
            if int(meta.get("edited_count", 0)) > 0:
                return "write"
            if meta.get("last_action"):
                return "read"
            return "new"

        files_meta = _load_action_graph().get("files", {})

        # Require min overlap to avoid false memory-first routing on single-word matches.
        strong_file_hits = [f for f in file_hits if int(f.get("overlap", 0)) >= MEMORY_FIRST_MIN_OVERLAP]
        if strong_file_hits:
            file_hits = strong_file_hits
            rec = [f["file"] for f in file_hits[: min(3, len(file_hits))]]
            # Apply time-decay: files only recommended if touched in recent 5 actions.
            recent_files = {a.get("file", "") for a in action_hits}
            rec = [f for f in rec if f in recent_files] or rec
            # v7.3: upgrade bare file paths → file::symbol where possible
            rec = _resolve_to_symbols(rec, query)
            rec_with_type = [{"file": f, "access_type": _access_type(f.split("::")[0] if "::" in f else f, files_meta)} for f in rec]
            # Intent-aware cap: explore queries must not be locked out of supplementary search.
            mem_query_intent = _dg_classify_intent(query) if _dg_classify_intent else "general"
            mem_confidence = "high"
            mem_max_greps = 0
            mem_max_files = 0
            if mem_query_intent == "explore":
                mem_confidence = "medium"
                mem_max_greps = max(mem_max_greps, 1)
            # v7.2: exhaustive tasks need exploration even in memory-first path.
            # Caps match retrieve_then_read path and explore_budget_chars is set so
            # graph_read uses the full exhaustive budget rather than the tight 60k default.
            if task_type == "exhaustive":
                mem_confidence = "medium"
                mem_max_greps = max(mem_max_greps, _EXHAUSTIVE_FALLBACK_CAP)
                mem_max_files = max(mem_max_files, 5)
                TURN_STATE["explore_budget_chars"] = _COST_BUDGETS["exhaustive"]["hard"]
                TURN_STATE["explore_hard_max_chars"] = EXPLORE_MAX_READ_CHARS
            # v6: behavioral tasks get more budget even in memory-first
            if task_type == "behavioral":
                mem_confidence = "medium"
                mem_max_greps = max(mem_max_greps, 2)
                mem_max_files = max(mem_max_files, 2)
            # v7.4 LEAN RESPONSE + v7.5 task_type restoration (1 word scaffolding).
            out: dict[str, Any] = {
                "ok": True,
                "mode": "memory_first",
                "confidence": mem_confidence,
                "max_supplementary_greps": mem_max_greps,
                "max_supplementary_files": mem_max_files,
                "recommended_files": [rc["file"] for rc in rec_with_type],
                "task_type": task_type,  # v7.5: restored — single word, anchors Claude's approach
            }
            # v7.4: emit expensive hints ONLY on the first graph_continue of the session.
            # Repeating them every turn just re-bills cache for text Claude has already seen.
            _first_turn = (int(TURN_STATE.get("turn_count", 0)) == 1)
            if task_type == "exhaustive":
                _natural_floor = max(1, min(len(_pre_hit_dirs), 3)) if _pre_hit_dirs else 2
                out["min_greps_required"] = _natural_floor
                if _pre_hit_dirs:
                    _sorted = sorted(_pre_hit_dirs.items(), key=lambda x: -x[1])
                    out["search_map"] = {
                        d: {"file_count": n, "sample_files": _pre_hit_samples.get(d, [])[:3]}
                        for d, n in _sorted[:6]
                    }
                if _first_turn:
                    out["exhaustive_hint"] = (
                        f"EXHAUSTIVE AUDIT: call fallback_rg/graph_grep_all at least "
                        f"{_natural_floor}× with distinct patterns; grep across all listed dirs."
                    )
            if task_type == "behavioral" and _first_turn:
                out["behavioral_hint"] = "Behavioral: concurrency, error propagation, resource lifecycle, cancellation."
            # Budget signals (only when actually triggered)
            if cost_guidance:
                out["cost_guidance"] = cost_guidance
            if turn_guidance:
                out["turn_guidance"] = turn_guidance
            if package_hints and _first_turn:
                out["package_hints"] = package_hints[:8]
            # v7.3: store current query + sym map so graph_read can auto-promote bare paths to symbols
            TURN_STATE["current_query"] = query
            TURN_STATE["recommended_sym_map"] = {
                f.split("::")[0]: f for f in rec if "::" in f
            }
            _log_tool("graph_continue", {"query": query, "mode": "memory_first", "recommended_files": rec})
            _record_action("continue_memory_first", {"query": query, "recommended_files": rec})
            _fire_shadow_grep(query, rec)
            if _incremental_refreshed:
                out["graph_refreshed"] = len(_incremental_refreshed)
                out["graph_refreshed_files"] = _incremental_refreshed[:5]
            return out

        # No relevant memory: do single retrieval and return compact suggestions.
        retrieved = graph_retrieve(query=query, top_files=top_files, top_edges=top_edges)
        TURN_STATE["graph_continue_called"] = True  # re-affirm after graph_retrieve (which resets turn state)
        graph_files = retrieved.get("graph_files", [])

        # v7.1: Language-aware reranking for mixed-language codebases.
        # Detects dominant language and demotes minority-language files so
        # e.g. vendored Python doesn't drown out C++ results.
        dominant_lang = _detect_dominant_language(gdata)
        if dominant_lang:
            graph_files = _rerank_by_language(graph_files, dominant_lang)

        rec_files = [str(f.get("id", "")) for f in graph_files if str(f.get("id", ""))]

        # ── Confidence tier based on top file score ───────────────────────────
        # v7.5: revert threshold to ≥10 for `high` (v7.4's ≥15 was too strict, only shifted
        # 31% of queries to medium and didn't target P13/P17/P18 as hoped). Keep v7.4's
        # 1-grep safety net at `high` — it's the actual improvement that helps.
        top_score = int(graph_files[0].get("_score", 0)) if graph_files else 0
        if top_score >= 10:
            confidence = "high"
            max_supp_greps = 1   # v7.4 safety net — always allow 1 supplementary grep
            max_supp_files = 1
        elif top_score >= 4:
            confidence = "medium"
            max_supp_greps = 2
            max_supp_files = 2
        else:
            confidence = "low"
            max_supp_greps = 3
            max_supp_files = 3

        # Exploration cap: broad architecture/trace queries must not be locked out of
        # supplementary search even when keyword scores are high.
        query_intent = _dg_classify_intent(query) if _dg_classify_intent else "general"
        if query_intent == "explore":
            if confidence == "high":
                confidence = "medium"
            max_supp_greps = max(max_supp_greps, 2)
            max_supp_files = max(max_supp_files, 2)
            TURN_STATE["explore_budget_chars"] = EXPLORE_READ_BUDGET_CHARS
            TURN_STATE["explore_hard_max_chars"] = EXPLORE_MAX_READ_CHARS

        # v7.6: Gap-aware query intent routing — detects content/control-flow/absence/
        # dynamic queries and adjusts confidence + strategy so the right tool is used.
        gap_intent = _detect_query_intent(query)
        TURN_STATE["gap_intent"] = gap_intent
        if gap_intent == "content_search":
            if confidence == "high":
                confidence = "low"
            elif confidence == "medium":
                confidence = "low"
            max_supp_greps = max(max_supp_greps, 5)
        elif gap_intent == "control_flow":
            if confidence == "high":
                confidence = "medium"
            max_supp_greps = max(max_supp_greps, 4)
        elif gap_intent == "absence_query":
            if confidence == "high":
                confidence = "medium"
            max_supp_greps = max(max_supp_greps, 3)
            TURN_STATE["absence_hint"] = True
        elif gap_intent == "dynamic_trace":
            if confidence == "high":
                confidence = "medium"
            max_supp_greps = max(max_supp_greps, 3)

        # v7.2: exhaustive tasks MUST have exploration budget — graph retrieval alone
        # cannot cover "find all X" style audits, especially on unfamiliar codebases.
        # explore_budget_chars is set so graph_read uses the full 500k budget, not the
        # tight 60k default that exhausts after 3-4 file reads.
        if task_type == "exhaustive":
            if confidence == "high":
                confidence = "medium"
            max_supp_greps = max(max_supp_greps, _EXHAUSTIVE_FALLBACK_CAP)
            max_supp_files = max(max_supp_files, 5)
            TURN_STATE["explore_budget_chars"] = _COST_BUDGETS["exhaustive"]["hard"]
            TURN_STATE["explore_hard_max_chars"] = EXPLORE_MAX_READ_CHARS
            # search_map already computed above in _pre_hit_dirs / _pre_hit_samples

        # v6: behavioral tasks need wider search — can't rely on keyword matching alone
        if task_type == "behavioral":
            if confidence == "high":
                confidence = "medium"
            max_supp_greps = max(max_supp_greps, 3)
            max_supp_files = max(max_supp_files, 3)

        # v7.2: For exhaustive tasks, diversify recommended_files across directories.
        # Top-K by score often clusters in one dir — model reads it, finds real bugs, stops.
        # Showing files from 3+ dirs makes the model naturally produce broader coverage.
        if task_type == "exhaustive" and len(rec_files) > 1:
            seen_dirs: set[str] = set()
            diverse: list[str] = []
            remaining: list[str] = []
            for f in rec_files:
                parts = f.replace("\\", "/").split("/")
                dir_key = "/".join(parts[:2]) if len(parts) >= 2 else f
                if dir_key not in seen_dirs:
                    diverse.append(f)
                    seen_dirs.add(dir_key)
                else:
                    remaining.append(f)
                if len(diverse) >= 5:
                    break
            # Pad with same-dir files if fewer than 3 diverse ones found
            if len(diverse) < 3:
                for f in remaining:
                    diverse.append(f)
                    if len(diverse) >= 3:
                        break
            rec_slice = diverse[:5]
        else:
            rec_slice = rec_files[: min(3, len(rec_files))]
        # v7.3: upgrade bare file paths → file::symbol where query resolves to a specific symbol
        rec_slice = _resolve_to_symbols(rec_slice, query)
        # v7.5.1: confidence-aware response strategy
        # Low: don't recommend files (likely wrong), tell Claude to fallback_rg first
        # Medium: recommend files + tell Claude to verify with fallback_rg if insufficient
        # High: recommend files, no extra exploration needed
        if confidence == "low":
            # Extract search hints from retrieved files (directories, not files)
            search_hints = []
            if rec_files:
                seen_dirs: set[str] = set()
                for f in rec_files[:6]:
                    d = str(Path(f).parent)
                    if d and d != "." and d not in seen_dirs:
                        seen_dirs.add(d)
                        search_hints.append(d)
            _low_strategy = (
                "Low confidence — graph is unsure where this lives. "
                "DO NOT read files blindly. Instead: "
                "1) Call fallback_rg with specific search terms. "
                "   fallback_rg returns symbol-contextualized results with code bodies inline — "
                "   you often won't need a follow-up graph_read. "
                "2) If you need more detail on a specific symbol, call graph_read. "
                "Never use native grep/cat/Read."
            )
            if gap_intent == "content_search":
                _low_strategy = (
                    "Content pattern search detected. "
                    "Call fallback_rg with the pattern — it returns hits with enclosing "
                    "symbol bodies inline. The results are self-sufficient; no graph_read needed "
                    "unless you need surrounding context. Never use native grep/cat/Read."
                )
            out: dict[str, Any] = {
                "ok": True,
                "mode": "search_first",
                "confidence": confidence,
                "recommended_files": [],
                "task_type": task_type,
                "query_intent": gap_intent,
                "search_hints": search_hints[:4],
                "strategy": _low_strategy,
            }
        elif confidence == "medium":
            _med_strategy = (
                "Medium confidence — these files are likely relevant but may not be complete. "
                "1) Read recommended_files with graph_read first. "
                "2) If they don't fully answer the question, call fallback_rg to find more. "
                "3) Then graph_read the files fallback_rg found. "
                "Never use native grep/cat/Read."
            )
            if TURN_STATE.get("absence_hint"):
                _med_strategy = (
                    "Absence query detected. Use graph_find_missing(have=..., lack=...) "
                    "to find symbols that have a property but lack another. "
                    "Example: graph_find_missing(have='api_route', lack='auth') for unprotected routes. "
                    "Then graph_read the results for details. Never use native grep/cat/Read."
                )
            out: dict[str, Any] = {
                "ok": True,
                "mode": "retrieve_then_read",
                "confidence": confidence,
                "recommended_files": rec_slice,
                "task_type": task_type,
                "query_intent": gap_intent,
                "strategy": _med_strategy,
            }
        else:
            out: dict[str, Any] = {
                "ok": True,
                "mode": "retrieve_then_read",
                "confidence": confidence,
                "recommended_files": rec_slice,
                "task_type": task_type,
                "fallback_rg_hint": "Use fallback_rg only if recommended_files don't fully answer the question. Never use native grep/cat/Read.",
            }
        _first_turn = (int(TURN_STATE.get("turn_count", 0)) == 1)
        # Exhaustive tasks benefit from search_map — keep it, but leaner.
        if task_type == "exhaustive" and _pre_hit_dirs:
            _sorted_dirs = sorted(_pre_hit_dirs.items(), key=lambda x: -x[1])
            out["search_map"] = {
                d: {"file_count": n, "sample_files": _pre_hit_samples.get(d, [])[:3]}
                for d, n in _sorted_dirs[:6]
            }
            _natural_floor = max(1, min(len(_pre_hit_dirs), 3))
            out["min_greps_required"] = _natural_floor
            if _first_turn:
                out["exhaustive_hint"] = f"EXHAUSTIVE AUDIT: ≥{_natural_floor} greps across listed dirs before synthesizing."
        if task_type == "behavioral" and _first_turn:
            out["behavioral_hint"] = "Behavioral: concurrency, error propagation, resource lifecycle, cancellation."
        if cost_guidance:
            out["cost_guidance"] = cost_guidance
        if turn_guidance:
            out["turn_guidance"] = turn_guidance
        if package_hints and _first_turn:
            out["package_hints"] = package_hints[:8]

        # v7.3: store current query + sym map so graph_read can auto-promote bare paths to symbols
        TURN_STATE["current_query"] = query
        TURN_STATE["recommended_sym_map"] = {
            f.split("::")[0]: f for f in rec_slice if "::" in f
        }
        _log_tool("graph_continue", {"query": query, "mode": "retrieve_then_read", "confidence": confidence, "recommended_files": rec_slice})
        _record_action("continue_retrieve", {"query": query, "recommended_files": rec_slice})
        if confidence == "high":
            _fire_shadow_grep(query, rec_slice)
        if _incremental_refreshed:
            out["graph_refreshed"] = len(_incremental_refreshed)
            out["graph_refreshed_files"] = _incremental_refreshed[:5]
        return out

    @mcp.tool()
    def fallback_rg(pattern: str, max_hits: int = 30, include_symbol_bodies: bool = True) -> dict[str, Any]:
        """Use instead of Bash grep/rg when graph_continue confidence is medium or low.
        Returns contextualized hits with enclosing symbol info and bodies.
        Do NOT use Bash grep directly — always use this tool for pattern search."""
        if not TURN_STATE.get("graph_continue_called"):
            return {
                "ok": False,
                "error": "Call graph_continue first, then use fallback_rg if confidence is medium or low.",
                "action_required": "graph_continue",
            }
        calls = int(TURN_STATE.get("fallback_calls", 0))
        retrieved = list(TURN_STATE.get("retrieved_files", []))
        graph_dirs: list[str] = []
        if retrieved:
            import os as _os
            seen: set[str] = set()
            for fp in retrieved:
                bare = fp.split("::")[0] if "::" in fp else fp
                rel_dir = str(_os.path.dirname(bare))
                if not rel_dir or rel_dir == ".":
                    continue
                abs_dir = PROJECT_ROOT / rel_dir
                if abs_dir.exists() and rel_dir not in seen:
                    seen.add(rel_dir)
                    graph_dirs.append(rel_dir)

        def _run_rg(search_paths: list[str]) -> subprocess.CompletedProcess:
            base = ["rg", "-n", "-S", "--engine", "pcre2",
                    "-B2", "-A2",
                    "--max-count", str(max_hits), pattern]
            r = subprocess.run(base + search_paths, cwd=str(PROJECT_ROOT),
                               capture_output=True, text=True, timeout=30, check=False)
            if r.returncode == 2 and "PCRE2" in r.stderr:
                base2 = ["rg", "-n", "-S", "-B2", "-A2",
                          "--max-count", str(max_hits), pattern]
                r = subprocess.run(base2 + search_paths, cwd=str(PROJECT_ROOT),
                                   capture_output=True, text=True, timeout=30, check=False)
            return r

        _log_tool("fallback_rg", {"pattern": pattern, "max_hits": max_hits,
                                   "graph_dirs": graph_dirs})

        # Phase 1: search graph-relevant directories
        proc = _run_rg(graph_dirs) if graph_dirs else None

        hits: list[dict[str, Any]] = []
        if proc and proc.stdout.strip():
            for line in proc.stdout.splitlines():
                if line.startswith("--"):
                    continue
                parts = line.split(":", 2)
                if len(parts) == 3:
                    hits.append({"file": parts[0], "line": parts[1], "text": parts[2]})
                if len(hits) >= max_hits:
                    break

        # Phase 2: full project if phase 1 found nothing
        if not hits:
            proc = _run_rg(["."])
            for line in proc.stdout.splitlines():
                if line.startswith("--"):
                    continue
                parts = line.split(":", 2)
                if len(parts) == 3:
                    hits.append({"file": parts[0], "line": parts[1], "text": parts[2]})
                if len(hits) >= max_hits:
                    break

        # ── Symbol contextualization ──────────────────────────────────────
        # Map each hit to its enclosing symbol and optionally include bodies
        sym_idx = _load_symbol_index()
        if sym_idx and hits:
            # Build per-file symbol ranges for fast lookup
            file_symbols: dict[str, list[tuple[int, int, str]]] = {}
            for sym_id, meta in sym_idx.items():
                path = meta.get("path", sym_id.split("::")[0])
                ls, le = int(meta.get("line_start", 0)), int(meta.get("line_end", 0))
                if ls and le:
                    file_symbols.setdefault(path, []).append((ls, le, sym_id))
            for syms in file_symbols.values():
                syms.sort(key=lambda x: x[0])

            # Annotate hits with enclosing symbol
            seen_symbols: dict[str, int] = {}
            for hit in hits:
                fpath = hit["file"]
                try:
                    hit_line = int(hit["line"])
                except (ValueError, TypeError):
                    continue
                syms = file_symbols.get(fpath, [])
                for ls, le, sym_id in syms:
                    if ls <= hit_line <= le:
                        hit["symbol"] = sym_id
                        hit["symbol_range"] = f"L{ls}-L{le}"
                        seen_symbols[sym_id] = seen_symbols.get(sym_id, 0) + 1
                        break

            # Include full bodies for top hit symbols (budget-tracked)
            # Cap: skip symbols > 80 lines (too large, agent should graph_read those)
            MAX_BODY_LINES = 80
            symbol_bodies: list[dict[str, Any]] = []
            skipped_large: list[str] = []
            if include_symbol_bodies and seen_symbols:
                budget_used = int(TURN_STATE.get("used_chars", 0))
                budget_max = int(TURN_STATE.get("char_budget", 60000))
                remaining = budget_max - budget_used
                top_syms = sorted(seen_symbols.items(), key=lambda x: -x[1])[:5]
                for sym_id, hit_count in top_syms:
                    if remaining <= 2000:
                        break
                    meta = sym_idx.get(sym_id, {})
                    path = meta.get("path", sym_id.split("::")[0])
                    ls = int(meta.get("line_start", 0))
                    le = int(meta.get("line_end", 0))
                    if not ls or not le:
                        continue
                    sym_lines = le - ls + 1
                    if sym_lines > MAX_BODY_LINES:
                        skipped_large.append(f"{sym_id} ({sym_lines} lines)")
                        continue
                    abs_path = PROJECT_ROOT / path
                    if not abs_path.exists():
                        continue
                    try:
                        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        body = "\n".join(lines[ls - 1:le])
                        if len(body) > remaining:
                            body = body[:remaining] + "\n... [truncated]"
                        symbol_bodies.append({
                            "symbol": sym_id,
                            "file": path,
                            "lines": f"{ls}-{le}",
                            "hit_count": hit_count,
                            "body": body,
                        })
                        remaining -= len(body)
                        TURN_STATE["used_chars"] = budget_used + (budget_max - remaining)
                    except Exception:
                        continue

            # For hits NOT inside any symbol, read surrounding context (±5 lines)
            # so the agent has enough context without needing graph_read
            uncontextualized_expanded: list[dict[str, Any]] = []
            if hits:
                unctx_files: dict[str, list[dict]] = {}
                for hit in hits:
                    if "symbol" not in hit:
                        unctx_files.setdefault(hit["file"], []).append(hit)
                for fpath, file_hits in list(unctx_files.items())[:5]:
                    abs_path = PROJECT_ROOT / fpath
                    if not abs_path.exists():
                        continue
                    try:
                        all_lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    except Exception:
                        continue
                    for hit in file_hits[:3]:
                        try:
                            hl = int(hit["line"]) - 1
                        except (ValueError, TypeError):
                            continue
                        ctx_start = max(0, hl - 5)
                        ctx_end = min(len(all_lines), hl + 6)
                        snippet = "\n".join(
                            f"{'>' if i == hl else ' '} {i+1}: {all_lines[i]}"
                            for i in range(ctx_start, ctx_end)
                        )
                        uncontextualized_expanded.append({
                            "file": fpath,
                            "line": hit["line"],
                            "context": snippet,
                        })

        # Shadow measurement: what vanilla Claude would have Read fully
        _shadow_hit_files = list({h["file"] for h in hits})
        _shadow_inlined = sum(len(sb.get("body", "")) for sb in symbol_bodies)
        _shadow_inlined += sum(len(ue.get("context", "")) for ue in uncontextualized_expanded)
        if _shadow_hit_files:
            _fire_shadow_file_reads("fallback_rg", _shadow_hit_files, _shadow_inlined)

        TURN_STATE["fallback_calls"] = calls + 1
        TURN_STATE["total_grep_calls"] = int(TURN_STATE.get("total_grep_calls", 0)) + 1
        _record_action("fallback_rg", {
            "pattern": pattern,
            "hit_count": len(hits),
            "searched_dirs": graph_dirs if hits and graph_dirs else ["(full project)"],
            "hit_files": list({h["file"] for h in hits[:10]}),
        })

        result: dict[str, Any] = {
            "ok": True,
            "pattern": pattern,
            "hits": hits,
            "searched_dirs": graph_dirs if hits and graph_dirs else ["(full project)"],
        }
        if symbol_bodies:
            result["symbol_bodies"] = symbol_bodies
            result["note"] = "Symbol bodies included inline — no need to call graph_read for these."
        if skipped_large:
            result["large_symbols_skipped"] = skipped_large
            result["large_symbol_hint"] = "These symbols are too large to inline. Use graph_read(file::symbol) for details."
        if uncontextualized_expanded:
            result["expanded_context"] = uncontextualized_expanded
        if not symbol_bodies and not uncontextualized_expanded:
            hit_files_unique = list(dict.fromkeys(h["file"] for h in hits))[:5]
            if hit_files_unique:
                result["next_step"] = f"Call graph_read on: {hit_files_unique}"
            else:
                result["next_step"] = "No hits found. Try a different search term."
        return result

    @mcp.tool()
    def graph_find_missing(have: str, lack: str, scope: str = "") -> dict[str, Any]:
        """Find symbols that HAVE a property but LACK another (absence queries).

        Examples:
          graph_find_missing(have="api_route", lack="auth")
            → routes without auth middleware
          graph_find_missing(have="exported", lack="test")
            → exported symbols without test coverage
          graph_find_missing(have="api_route", lack="validate")
            → endpoints without input validation

        Args:
            have: What symbols must have. Values: "api_route", "hook", "model",
                  "use_case", "exported", or any keyword/annotation substring.
            lack: What symbols must NOT have. Checked against annotations and
                  importers. Values: "auth", "validate", "test", or any substring.
            scope: Optional file path prefix to scope the search (e.g. "src/api/").
        """
        graph = _local_info_graph()
        if not graph:
            return {"ok": False, "error": "Graph not available. Run graph_scan first."}

        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])

        # Build importer index: who imports each file?
        # Use resolved_to (absolute file ID) when available, fall back to raw 'to'.
        importers: dict[str, set[str]] = {}
        for e in edges:
            if e.get("rel") in ("imports", "requires"):
                target = e.get("resolved_to") or e.get("to", "")
                if target:
                    importers.setdefault(target, set()).add(e["from"])

        # Collect candidate symbols matching `have`
        have_lower = have.lower()
        candidates: list[dict] = []
        for node in nodes:
            if node.get("kind") != "symbol":
                continue
            if scope and not node.get("path", "").startswith(scope):
                continue
            match = False
            if have_lower == "exported" and node.get("exported"):
                match = True
            elif have_lower == node.get("symbol_type", ""):
                match = True
            elif have_lower in (node.get("symbol_type", "") or ""):
                match = True
            elif any(have_lower in kw for kw in node.get("keywords", [])):
                match = True
            elif any(have_lower in (a or "").lower() for a in (node.get("annotations") or [])):
                match = True
            if match:
                candidates.append(node)

        # Filter to those that LACK the specified property
        lack_lower = lack.lower()
        missing: list[dict] = []
        for node in candidates:
            has_it = False
            # Check annotations
            for ann in (node.get("annotations") or []):
                if lack_lower in ann.lower():
                    has_it = True
                    break
            # Check keywords
            if not has_it:
                for kw in node.get("keywords", []):
                    if lack_lower in kw:
                        has_it = True
                        break
            # For "test": check if any importer is a test file
            if not has_it and lack_lower == "test":
                file_path = node.get("path", "")
                file_importers = importers.get(file_path, set())
                for imp in file_importers:
                    if "test" in imp.lower() or "spec" in imp.lower() or "__tests__" in imp:
                        has_it = True
                        break
            if not has_it:
                missing.append({
                    "symbol": node.get("id", ""),
                    "file": node.get("path", ""),
                    "name": node.get("name", ""),
                    "symbol_type": node.get("symbol_type", ""),
                    "line": node.get("line_start", 0),
                    "annotations": node.get("annotations") or [],
                })

        _log_tool("graph_find_missing", {"have": have, "lack": lack, "scope": scope,
                                          "candidates": len(candidates), "missing": len(missing)})
        return {
            "ok": True,
            "have": have,
            "lack": lack,
            "scope": scope or "(all)",
            "total_matching_have": len(candidates),
            "missing_count": len(missing),
            "missing": missing[:50],
            "note": f"Found {len(missing)} symbols that have '{have}' but lack '{lack}'."
                    + (" (showing first 50)" if len(missing) > 50 else ""),
        }

    @mcp.tool()
    def graph_dynamic_trace(key: str, kind: str = "auto") -> dict[str, Any]:
        """Trace runtime/dynamic patterns: DI container resolution or event flow.

        Examples:
          graph_dynamic_trace(key="orderService", kind="di")
            → shows who registers and who resolves "orderService"
          graph_dynamic_trace(key="order.created", kind="event")
            → shows who emits and who subscribes to "order.created"
          graph_dynamic_trace(key="order")
            → auto-detects: searches both DI keys and event names

        Args:
            key: The DI service key or event name to trace.
            kind: "di", "event", or "auto" (searches both).
        """
        graph = _local_info_graph()
        if not graph:
            return {"ok": False, "error": "Graph not available. Run graph_scan first."}

        dyn = graph.get("dynamic_registry", {})
        if not dyn:
            return {"ok": False, "error": "Dynamic registry not built. Re-run graph_scan.",
                    "hint": "The graph was built before this feature. Rebuild with latest graph_builder."}

        di_reg = dyn.get("di", {})
        events_reg = dyn.get("events", {})
        results: dict[str, Any] = {"ok": True, "key": key, "kind": kind}

        if kind in ("di", "auto"):
            # Search DI registry — exact match and partial match
            di_matches: dict[str, dict] = {}
            for dk, data in di_reg.items():
                if dk == key or key.lower() in dk.lower():
                    di_matches[dk] = data
            if di_matches:
                results["di"] = di_matches

        if kind in ("event", "auto"):
            # Search event registry — exact match and partial match
            event_matches: dict[str, dict] = {}
            for ek, data in events_reg.items():
                if ek == key or key.lower() in ek.lower():
                    event_matches[ek] = data
            if event_matches:
                results["events"] = event_matches

        found_di = bool(results.get("di"))
        found_events = bool(results.get("events"))
        if not found_di and not found_events:
            # Try broader search
            all_di_keys = list(di_reg.keys())
            all_event_keys = list(events_reg.keys())
            results["found"] = False
            results["note"] = f"No matches for '{key}'. Available DI keys: {len(all_di_keys)}, event names: {len(all_event_keys)}."
            results["similar_di"] = [k for k in all_di_keys if any(w in k.lower() for w in key.lower().split("."))][:10]
            results["similar_events"] = [k for k in all_event_keys if any(w in k.lower() for w in key.lower().split("."))][:10]
        else:
            results["found"] = True
            parts = []
            if found_di:
                for dk, data in results["di"].items():
                    parts.append(f"DI '{dk}': {len(data.get('registers',[]))} registers, {len(data.get('resolves',[]))} resolves")
            if found_events:
                for ek, data in results["events"].items():
                    parts.append(f"Event '{ek}': {len(data.get('emitters',[]))} emitters, {len(data.get('subscribers',[]))} subscribers")
            results["note"] = "; ".join(parts)

        _log_tool("graph_dynamic_trace", {"key": key, "kind": kind, "found": results.get("found", False)})
        return results

    @mcp.tool()
    def graph_external_usage(package: str, symbol: str = "") -> dict[str, Any]:
        """Find which files import a specific symbol from an external package.

        Examples:
          graph_external_usage(package="@medusajs/framework", symbol="container")
          graph_external_usage(package="zod")  # all imports from zod
          graph_external_usage(package="react", symbol="useState")

        Args:
            package: The package name (e.g., "@medusajs/framework", "zod", "express").
            symbol: Optional specific export to look up. If empty, returns all symbols
                    imported from that package.
        """
        graph = _local_info_graph()
        if not graph:
            return {"ok": False, "error": "Graph not available. Run graph_scan first."}

        ext_idx = graph.get("external_usage_index", {})
        if not ext_idx:
            return {"ok": False, "error": "External usage index not built. Re-run graph_scan to generate it.",
                    "hint": "The graph was built before this feature existed. Rebuild with the latest graph_builder."}

        # Find matching packages (support partial match for scoped packages)
        matched_pkgs: dict[str, dict[str, list[str]]] = {}
        for pkg_name, symbols in ext_idx.items():
            if pkg_name == package or pkg_name.startswith(package + "/") or package in pkg_name:
                matched_pkgs[pkg_name] = symbols

        if not matched_pkgs:
            return {"ok": True, "package": package, "symbol": symbol,
                    "found": False, "note": f"No imports from '{package}' found in this codebase."}

        if symbol:
            # Look up specific symbol across matched packages
            files: list[str] = []
            for pkg_name, symbols_map in matched_pkgs.items():
                for sym_name, file_list in symbols_map.items():
                    if sym_name == symbol or symbol.lower() in sym_name.lower():
                        files.extend(file_list)
            # If not found in matched packages, search ALL packages for this symbol
            # (handles re-exports: same symbol exported from different subpaths)
            if not files:
                for pkg_name, symbols_map in ext_idx.items():
                    if pkg_name in matched_pkgs:
                        continue
                    for sym_name, file_list in symbols_map.items():
                        if sym_name == symbol:
                            files.extend(file_list)
            files = list(dict.fromkeys(files))  # dedupe
            return {
                "ok": True,
                "package": package,
                "symbol": symbol,
                "found": bool(files),
                "files": files[:30],
                "count": len(files),
                "note": f"'{symbol}' from '{package}' is imported in {len(files)} files."
                        + (" (showing first 30)" if len(files) > 30 else "")
                        + (" (includes re-exports from related packages)" if len(files) > 0 and not matched_pkgs else ""),
            }
        else:
            # Return all symbols imported from this package
            all_symbols: dict[str, int] = {}
            for pkg_name, symbols_map in matched_pkgs.items():
                for sym_name, file_list in symbols_map.items():
                    all_symbols[sym_name] = all_symbols.get(sym_name, 0) + len(file_list)
            sorted_syms = sorted(all_symbols.items(), key=lambda x: -x[1])
            return {
                "ok": True,
                "package": package,
                "found": bool(all_symbols),
                "symbols": [{"name": s, "import_count": c} for s, c in sorted_syms[:40]],
                "total_symbols": len(all_symbols),
                "matched_packages": list(matched_pkgs.keys()),
                "note": f"{len(all_symbols)} symbols imported from '{package}' across the codebase.",
            }

    @mcp.tool()
    def cross_search(pattern: str, path: str, max_hits: int = 50) -> dict[str, Any]:
        """Search for a pattern in a directory OUTSIDE the current project.
        Registers found files so the gate allows subsequent cat/grep/find.
        For in-project searches use fallback_rg instead.
        """
        search_path = Path(path).expanduser().resolve()

        # Redirect internal paths to fallback_rg
        try:
            search_path.relative_to(PROJECT_ROOT)
            return {
                "ok": False,
                "error": "path is inside the current project root",
                "hint": "Use fallback_rg(pattern=...) for in-project searches.",
                "project_root": str(PROJECT_ROOT),
            }
        except ValueError:
            pass

        if not search_path.exists():
            return {"ok": False, "error": f"path does not exist: {search_path}"}

        max_hits = min(int(max_hits or 50), 50)

        def _run(target: str) -> subprocess.CompletedProcess:
            base = ["rg", "-n", "-S", "--engine", "pcre2",
                    "--max-count", str(max_hits), pattern, target]
            r = subprocess.run(base, capture_output=True, text=True, timeout=30, check=False)
            if r.returncode == 2 and "PCRE2" in r.stderr:
                base2 = ["rg", "-n", "-S", "--max-count", str(max_hits), pattern, target]
                r = subprocess.run(base2, capture_output=True, text=True, timeout=30, check=False)
            return r

        try:
            proc = _run(str(search_path))
        except FileNotFoundError:
            return {"ok": False, "error": "rg not found — install ripgrep"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "search timed out after 30s",
                    "hint": "Use a more specific path or pattern"}

        hits: list[dict] = []
        hit_files: set[str] = set()
        hit_dirs: set[str] = set()

        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3:
                abs_file = parts[0]
                hits.append({"file": abs_file, "line": parts[1], "text": parts[2]})
                hit_files.add(abs_file)
                hit_dirs.add(str(Path(abs_file).parent) + "/")
                if len(hits) >= max_hits:
                    break

        # Register in external_refs
        g = _load_action_graph()
        ext_refs: dict = g.setdefault("external_refs", {})
        now_ts = int(time.time())

        # Evict expired entries (>24h) on each write
        for k in [k for k, v in ext_refs.items()
                  if now_ts - int(v.get("ts", 0)) >= 86400]:
            del ext_refs[k]

        # Always register searched root (even with 0 hits)
        root_key = str(search_path).rstrip("/") + ("/" if search_path.is_dir() else "")
        ext_refs[root_key] = {"ts": now_ts, "found_via": "cross_search",
                              "pattern": pattern,
                              "kind": "dir" if search_path.is_dir() else "file"}

        # Register each hit file + parent dir
        for f in hit_files:
            ext_refs[f] = {"ts": now_ts, "found_via": "cross_search",
                           "pattern": pattern, "kind": "file"}
        for d in hit_dirs:
            if d not in ext_refs:
                ext_refs[d] = {"ts": now_ts, "found_via": "cross_search",
                               "pattern": pattern, "kind": "dir"}

        _save_action_graph(g)
        _log_tool("cross_search", {"pattern": pattern, "path": str(search_path),
                                    "hit_count": len(hits)})
        _record_action("cross_search", {
            "pattern": pattern, "path": str(search_path),
            "hit_count": len(hits), "hit_files": sorted(hit_files)[:10],
        })

        return {
            "ok": True,
            "pattern": pattern,
            "path": str(search_path),
            "hits": hits,
            "next_step": (
                f"Registered. You can now cat/grep/find paths under {search_path} — gate allows silently."
                if hits else
                f"No hits. {search_path} registered — ls/find on this dir now allowed."
            ),
        }

    @mcp.tool()
    def graph_add_memory(
        type: str,
        content: str,
        tags: list[str] | None = None,
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a memory entry to the context store with automatic pruning.

        Use this instead of writing context-store.json directly. It ensures
        the store stays healthy and never bloats beyond its cap.

        Args:
            type: Entry kind — decision | task | next | fact | blocker
            content: One sentence, max 15 words.
            tags: Optional list of topic tags.
            files: Optional list of relevant file paths.
        """
        import datetime as _dt

        entry: dict[str, Any] = {
            "type": type,
            "content": content,
            "tags": tags or [],
            "files": files or [],
            "date": _dt.date.today().isoformat(),
        }
        existing = _load_context_store()
        existing.append(entry)
        _save_context_store(existing)
        total = len(_load_context_store())
        return {"ok": True, "total": total}

    # ── v7: Doc Enrichment (Extractors 2+3) ─────────────────────────────────
    # Runs after _gb_scan. Patches file node keywords with terms extracted from
    # .md files — zero token cost at query time, purely additive at build time.

    _DOC_PATH_BACKTICK = re.compile(r'`((?:[\w.-]+/){1,5}[\w.-]*)`')
    _DOC_FILE_MENTION  = re.compile(r'`([\w/.-]+\.(java|cpp|go|py|rs|h|cc|ts|tsx|js))`')
    _DOC_HEADING       = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)
    _DOC_COMPONENT     = re.compile(
        r'\b(frontend|backend|fe|be|storage|scheduler|executor|planner|compiler|'
        r'engine|server|client|proxy|gateway|broker|worker|coordinator|manager|'
        r'controller|handler|tablet|replica|partition|segment|chunk|block|'
        r'query|transaction|catalog|metadata|optimizer|parser|analyzer|rewriter|'
        r'pipeline|operator|compaction|ingestion|cache|buffer|raft|consensus|'
        r'replication|snapshot|checkpoint|recovery|election|leader|follower)\b',
        re.IGNORECASE,
    )

    def _doc_extract_enrichment(root: Path) -> dict[str, set[str]]:
        """Scan all .md files under root.
        Returns: {dir_prefix -> set of enrichment terms}
        e.g. {"fe/": {"fe", "frontend", "query", "planner", "nereids"}}
        """
        dir_terms: dict[str, set[str]] = {}

        def _add(prefix: str, terms: set[str]) -> None:
            prefix = prefix.rstrip('/') + '/'
            if prefix not in dir_terms:
                dir_terms[prefix] = set()
            dir_terms[prefix].update(t.lower() for t in terms if len(t) >= 2)

        for md_file in root.rglob('*.md'):
            if '.git' in md_file.parts:
                continue
            try:
                text = md_file.read_text(errors='ignore')
            except Exception:
                continue

            # Split into sections by heading
            headings = list(_DOC_HEADING.finditer(text))
            for i, hm in enumerate(headings):
                heading_text = hm.group(2).strip()
                section_start = hm.end()
                section_end = len(text)
                for j in range(i + 1, len(headings)):
                    if len(headings[j].group(1)) <= len(hm.group(1)):
                        section_end = headings[j].start()
                        break
                section = text[section_start:section_end][:600]

                # Component keywords from heading + section start
                combined = heading_text + ' ' + section[:200]
                components = set(m.lower() for m in _DOC_COMPONENT.findall(combined))

                # Paths from backtick mentions in this section
                paths_found = []
                for pat in (_DOC_PATH_BACKTICK, _DOC_FILE_MENTION):
                    for pm in pat.finditer(section):
                        p = pm.group(1)
                        if not p.startswith(('http', 'www', 'v1.', 'v2.')):
                            paths_found.append(p)

                # For each path found, associate it with component terms + heading words
                heading_words = set(
                    w.lower() for w in re.split(r'\W+', heading_text)
                    if len(w) >= 3 and w.lower() not in {
                        'the', 'and', 'for', 'with', 'from', 'this', 'that',
                        'are', 'was', 'has', 'not', 'but', 'all', 'can',
                    }
                )
                terms = components | heading_words

                for p in paths_found:
                    # Determine the directory prefix for this path
                    # e.g. "fe/fe-core/AGENTS.md" → "fe/" and "fe/fe-core/"
                    parts = p.replace('\\', '/').split('/')
                    # Add terms to each level of the path hierarchy
                    cumulative = ''
                    for part in parts:
                        if part:
                            cumulative += part + '/'
                            candidate = root / cumulative.rstrip('/')
                            if candidate.exists():
                                _add(cumulative, terms)

                # Also: if heading alone has strong component signal and no paths,
                # try to match against top-level dirs by component name
                if components and not paths_found:
                    for comp in components:
                        candidate_dir = root / comp
                        if candidate_dir.is_dir():
                            _add(comp + '/', heading_words | components)

        return dir_terms

    def _apply_doc_enrichment(graph: dict, dir_terms: dict[str, set[str]]) -> int:
        """Patch file nodes in graph with doc enrichment keywords.
        Returns count of nodes enriched."""
        enriched = 0
        for node in graph.get('nodes', []):
            if node.get('kind') != 'file':
                continue
            file_path = str(node.get('path', node.get('id', '')))
            added_terms: set[str] = set()
            for prefix, terms in dir_terms.items():
                if file_path.startswith(prefix) or file_path == prefix.rstrip('/'):
                    added_terms.update(terms)
            if added_terms:
                existing_kw = set(node.get('keywords', []))
                new_kw = list(existing_kw | added_terms)
                node['keywords'] = new_kw
                enriched += 1
        return enriched

    @mcp.tool()
    def graph_scan(project_root: str) -> dict[str, Any]:
        """Scan a local project directory and build/refresh its information graph.

        Call this once at the start of a session to point the dual-graph at your
        project folder. After scanning, graph_retrieve / graph_read / graph_continue
        will work against that project.

        Args:
            project_root: Absolute path to the project directory to scan.
        """
        global PROJECT_ROOT  # noqa: PLW0603

        if _gb_scan is None:
            return {"ok": False, "error": "graph_builder not available"}

        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            return {"ok": False, "error": f"Not a directory: {root}"}

        # Load existing nodes for incremental re-scan (preserve summaries for unchanged files).
        graph_json = DG_DATA_DIR / "info_graph.json"
        existing_nodes: dict = {}
        if graph_json.exists():
            try:
                old_graph = json.loads(graph_json.read_text(encoding="utf-8"))
                existing_nodes = {n["id"]: n for n in old_graph.get("nodes", []) if n.get("kind") == "file"}
            except Exception:
                pass

        # Run scan with timeout to prevent MCP tool call from hanging on large repos
        import concurrent.futures
        _SCAN_TIMEOUT_SEC = 90
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_gb_scan, root, existing_nodes=existing_nodes)
                graph = future.result(timeout=_SCAN_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            # Scan too slow (large repo or slow I/O like WSL /mnt/c/)
            # Mark in session state so graph_continue won't loop asking for re-scan
            PROJECT_ROOT = root
            TURN_STATE["scan_timed_out"] = True
            _write_gc_active()
            TURN_STATE["graph_continue_called"] = True
            return {
                "ok": False,
                "scan_timeout": True,
                "skip": True,
                "project_root": str(root),
                "error": f"Scan exceeded {_SCAN_TIMEOUT_SEC}s timeout (project too large or slow I/O). "
                         "Use native tools (Read, Bash grep) for this session — the graph gate will allow them.",
                "hint": "Proceed with your task using standard file exploration. "
                        "The graph is not available for this project.",
            }

        # v7: Doc enrichment — extract path+heading mappings from .md files,
        # patch file node keywords so BM25 retrieval uses them automatically.
        try:
            dir_terms = _doc_extract_enrichment(root)
            enriched_count = _apply_doc_enrichment(graph, dir_terms)
        except Exception:
            dir_terms = {}
            enriched_count = 0

        # Ensure graph always stores the project root and build timestamp.
        graph["root"] = str(root)
        graph["built_at"] = int(time.time())
        # Stamp file for find-based staleness detection (fallback when no git)
        try:
            (DG_DATA_DIR / "last_scan_stamp").write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass

        # Write to the same JSON the dashboard serves.
        graph_json = DG_DATA_DIR / "info_graph.json"
        graph_json.parent.mkdir(parents=True, exist_ok=True)
        graph_json.write_text(json.dumps(graph, indent=2), encoding="utf-8")

        # Build and persist symbol index for O(1) graph_read lookups.
        sym_index = _build_symbol_index(graph)
        SYMBOL_INDEX_FILE.write_text(json.dumps(sym_index), encoding="utf-8")
        _reconcile_context_store(sym_index)

        # Update module-level root so graph_read / fallback_rg resolve correctly.
        PROJECT_ROOT = root

        # ── Reset all project-specific state ─────────────────────────────────
        # Retrieval cache: scores are stale from the old project.
        # Symbol index was already written above — do NOT delete it here.
        RETRIEVAL_CACHE_FILE.unlink(missing_ok=True)

        # Action graph: file reads/edits belong to the old project.
        _save_action_graph({"nodes": [], "edges": [], "files": {}, "actions": []})

        # Turn state: in-memory budgets, seen reads, retrieved file list.
        TURN_STATE.update({
            "query_key": "",
            "used_chars": 0,
            "seen_reads": {},
            "reuse_gate_candidates": [],
            "reuse_gate_satisfied": False,
            "retrieved_files": [],
            "retrieve_count": 0,
            "last_retrieve_out": None,
            "fallback_calls": 0,
            "grep_all_calls": 0,
            "graph_continue_called": True,  # graph_scan is the setup step; reads allowed after
        })
        _write_gc_active()

        file_count = graph.get("file_count", graph["node_count"])
        symbol_count = graph.get("symbol_count", 0)
        _log_tool("graph_scan", {"project_root": str(root), "files": file_count, "symbols": symbol_count, "edges": graph["edge_count"], "doc_enriched": enriched_count})
        return {
            "ok": True,
            "project_root": str(root),
            "file_count": file_count,
            "symbol_count": symbol_count,
            "edge_count": graph["edge_count"],
            "doc_enriched_nodes": enriched_count,
            "doc_dir_prefixes": len(dir_terms),
            "message": f"Graph built. {enriched_count} file nodes enriched from docs. Use graph_continue or graph_retrieve to query.",
        }

    @mcp.tool()
    def graph_dead_exports(file_prefix: str = "", min_line_span: int = 0, symbol_type: str = "", limit: int = 60) -> dict[str, Any]:
        """Return pre-computed dead exports. Call after graph_continue.

        Pre-computed at scan time — instant, no grep needed. Scope per package
        using file_prefix (e.g. "packages/admin") so you can iterate per-package.

        Args:
            file_prefix: Only return dead exports in files starting with this prefix.
            min_line_span: Only include symbols with (line_end - line_start) >= this.
            symbol_type: Filter by symbol type (api_route, hook, model, use_case, utility).
            limit: Max results to return (default 60).
        """
        if not TURN_STATE.get("graph_continue_called"):
            return {"ok": False, "error": "Call graph_continue first.", "action_required": "graph_continue"}
        graph_json = DG_DATA_DIR / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph found. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        dead_exports = graph.get("dead_exports", [])
        if not dead_exports:
            return {"ok": True, "total": 0, "results": [], "note": "No dead exports found or graph predates v5 — re-run graph_scan."}

        results = dead_exports
        if file_prefix:
            results = [d for d in results if d.get("file", "").startswith(file_prefix)]
        if symbol_type:
            results = [d for d in results if d.get("symbol_type") == symbol_type]
        if min_line_span > 0:
            sym_idx = {}
            try:
                if SYMBOL_INDEX_FILE.exists():
                    sym_idx = json.loads(SYMBOL_INDEX_FILE.read_text())
            except Exception:
                pass
            def _span(d: dict) -> int:
                key = f"{d['file']}::{d['symbol']}"
                s = sym_idx.get(key, {})
                return s.get("line_end", 0) - s.get("line_start", 0)
            results = [d for d in results if _span(d) >= min_line_span]

        total = len(results)
        results = results[:limit]
        _log_tool("graph_dead_exports", {"file_prefix": file_prefix, "total": total, "returned": len(results)})
        return {"ok": True, "total": total, "returned": len(results), "results": results}

    @mcp.tool()
    def graph_find_cycles(file_prefix: str = "", limit: int = 20) -> dict[str, Any]:
        """Find circular import chains. Call after graph_continue.

        Resolves relative import paths properly so barrel-file cycles are detected.
        Scope per package with file_prefix (e.g. "packages/medusa").

        Args:
            file_prefix: Only look for cycles among files starting with this prefix.
            limit: Max number of cycles to return.
        """
        if not TURN_STATE.get("graph_continue_called"):
            return {"ok": False, "error": "Call graph_continue first.", "action_required": "graph_continue"}
        graph_json = DG_DATA_DIR / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph found. Call graph_scan first."}
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # Build file_id set and stem_to_id map for path resolution
        file_ids: set[str] = set()
        for node in graph.get("nodes", []):
            if node.get("kind") == "file":
                file_ids.add(node["id"])

        stem_to_id: dict[str, str] = {}
        for fid in file_ids:
            # Strip extension
            stem = re.sub(r'\.[^/]+$', '', fid)
            stem_to_id[stem] = fid
            # Also map /index stripping (barrel files)
            if stem.endswith("/index"):
                stem_to_id[stem[:-len("/index")]] = fid

        def resolve_import(src_file: str, dst_raw: str) -> str | None:
            if not dst_raw.startswith("."):
                return None
            src_dir = posixpath.dirname(src_file)
            joined = posixpath.normpath(posixpath.join(src_dir, dst_raw))
            if joined in file_ids:
                return joined
            for candidate in (joined, f"{joined}/index"):
                if candidate in stem_to_id:
                    return stem_to_id[candidate]
            return None

        # Build adjacency list from edges
        adj: dict[str, set[str]] = {}
        for edge in graph.get("edges", []):
            if edge.get("rel") != "imports":
                continue
            src = edge.get("from", "")
            dst_raw = edge.get("to", "")
            if file_prefix and not src.startswith(file_prefix):
                continue
            # Try resolve relative imports; skip absolute/external
            resolved = resolve_import(src, dst_raw)
            if resolved is None:
                continue
            if file_prefix and not resolved.startswith(file_prefix):
                continue
            if src not in adj:
                adj[src] = set()
            adj[src].add(resolved)

        # Tarjan's SCC
        index_counter = [0]
        stack: list[str] = []
        lowlink: dict[str, int] = {}
        index: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        sccs: list[list[str]] = []

        def strongconnect(v: str) -> None:
            index[v] = lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack[v] = True
            for w in adj.get(v, set()):
                if w not in index:
                    strongconnect(w)
                    lowlink[v] = min(lowlink[v], lowlink[w])
                elif on_stack.get(w):
                    lowlink[v] = min(lowlink[v], index[w])
            if lowlink[v] == index[v]:
                scc: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

        import sys
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 5000))
        try:
            for v in list(adj.keys()):
                if v not in index:
                    strongconnect(v)
        finally:
            sys.setrecursionlimit(old_limit)

        cycles = [{"size": len(s), "files": sorted(s)} for s in sorted(sccs, key=len, reverse=True)]
        cycles = cycles[:limit]
        _log_tool("graph_find_cycles", {"file_prefix": file_prefix, "total_cycles": len(sccs), "returned": len(cycles)})
        return {"ok": True, "total_cycles": len(sccs), "returned": len(cycles), "cycles": cycles}

    @mcp.tool()
    def graph_grep_all(pattern: str, file_glob: str = "", max_hits: int = 200) -> dict[str, Any]:
        """Exhaustive grep across the entire project.

        Use for full-codebase sweeps. Each call costs tokens — use BROAD patterns
        that return all findings in 1-2 calls. Do NOT call in a loop per-package.

        Soft limit: 5 calls per task (exhaustive) / 2 calls (targeted).
        After the limit, results are still returned but a warning is added.

        Args:
            pattern: Regex pattern to search for.
            file_glob: Optional glob to restrict files (e.g. "*.ts", "**/*.py").
            max_hits: Maximum number of matches to return (default 200).
        """
        if not TURN_STATE.get("graph_continue_called"):
            return {"ok": False, "error": "Call graph_continue first.", "action_required": "graph_continue"}
        # Track call count and enforce soft limit
        call_num = int(TURN_STATE.get("grep_all_calls", 0)) + 1
        TURN_STATE["grep_all_calls"] = call_num
        TURN_STATE["total_grep_calls"] = int(TURN_STATE.get("total_grep_calls", 0)) + 1
        task_type = TURN_STATE.get("task_type", "unknown")
        soft_limit = 5 if task_type == "exhaustive" else 2

        cmd = ["rg", "--no-heading", "-n", pattern, str(PROJECT_ROOT)]
        if file_glob:
            cmd += ["-g", file_glob]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            raw = result.stdout
        except Exception as e:
            return {"ok": False, "error": str(e)}

        lines = raw.splitlines()
        total = len(lines)
        truncated = total > max_hits
        lines = lines[:max_hits]

        # Make paths relative to project root
        prefix = str(PROJECT_ROOT) + "/"
        cleaned = []
        for line in lines:
            if line.startswith(prefix):
                line = line[len(prefix):]
            cleaned.append(line)

        _log_tool("graph_grep_all", {"pattern": pattern, "total": total, "returned": len(cleaned), "call_num": call_num})
        hit_files = list({line.split(":")[0] for line in cleaned if ":" in line})[:15]
        # Shadow measurement: vanilla Claude would Read all hit files fully
        if hit_files:
            _fire_shadow_file_reads("graph_grep_all", hit_files, sum(len(line) for line in cleaned))
        _record_action("grep_all", {
            "pattern": pattern,
            "file_glob": file_glob,
            "hit_count": total,
            "hit_files": hit_files,
        })

        out: dict[str, Any] = {"ok": True, "total": total, "truncated": truncated, "results": cleaned,
                               "grep_all_calls": call_num, "grep_all_soft_limit": soft_limit}
        if call_num >= soft_limit:
            out["warning"] = (
                f"GREP_LIMIT: {call_num}/{soft_limit} graph_grep_all calls used for this {task_type} task. "
                "Synthesize findings from what you have — do not call graph_grep_all again."
            )
        return out

    # ── Feature 1: Debugging Intelligence ────────────────────────────────────
    @mcp.tool()
    def graph_debug(
        error: str,
        language: str = "auto",
        max_radius: int = 5,
    ) -> dict[str, Any]:
        """Parse a stack trace or error message and find the root cause via graph traversal.

        Given an error, stack trace, or log snippet, this tool:
        1. Extracts file:line:function frames from the stack trace
        2. Looks up those functions in the symbol index
        3. Traverses import/call edges to find blast radius
        4. Scores files by graph distance + recent git changes
        5. Returns a structured root cause report with recommended files to read

        Args:
            error: The full error message, stack trace, or log snippet.
            language: Language hint — "python", "javascript", "go", or "auto".
            max_radius: Maximum graph hops to traverse from crash point (default 5).
        """
        graph_json = DG_DATA_DIR / "info_graph.json"
        if not graph_json.exists():
            return {"ok": False, "error": "No graph found. Call graph_scan first."}

        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        sym_index = _load_symbol_index()

        # ── Step 1: Parse stack frames ────────────────────────────────────────
        frames: list[dict[str, Any]] = []

        # Python: File "path/file.py", line N, in function_name
        for m in re.finditer(r'File "([^"]+\.py)", line (\d+), in (\S+)', error):
            path = m.group(1)
            # Make relative to project root
            try:
                path = str(Path(path).relative_to(PROJECT_ROOT))
            except Exception:
                pass
            frames.append({"file": path, "line": int(m.group(2)), "function": m.group(3), "lang": "python"})

        # JavaScript/TypeScript: at FunctionName (file.ts:N:M) or at file.ts:N:M
        for m in re.finditer(r'at (?:(\w[\w.$<>]*) )?\(?([^\s(]+\.[jt]sx?):(\d+):\d+\)?', error):
            func = m.group(1) or "<anonymous>"
            path = m.group(2)
            try:
                path = str(Path(path).relative_to(PROJECT_ROOT))
            except Exception:
                pass
            frames.append({"file": path, "line": int(m.group(3)), "function": func, "lang": "javascript"})

        # Go: goroutine N [running]: or file.go:N +0xNN
        for m in re.finditer(r'([^\s]+\.go):(\d+)', error):
            path = m.group(1)
            try:
                path = str(Path(path).relative_to(PROJECT_ROOT))
            except Exception:
                pass
            frames.append({"file": path, "line": int(m.group(2)), "function": "", "lang": "go"})

        # Generic: any file.ext:line pattern
        if not frames:
            for m in re.finditer(r'([\w./\-]+\.\w{1,6}):(\d+)', error):
                path = m.group(1)
                frames.append({"file": path, "line": int(m.group(2)), "function": "", "lang": "auto"})

        # Dedupe frames by file
        seen_files: set[str] = set()
        unique_frames: list[dict[str, Any]] = []
        for f in frames:
            if f["file"] not in seen_files:
                seen_files.add(f["file"])
                unique_frames.append(f)

        # ── Step 2: Build adjacency from graph edges ──────────────────────────
        # Graph stores imports as module names (sentry.integrations.pagerduty.client)
        # not file paths. Build a module→file mapping for resolution.
        file_ids: set[str] = set()
        module_to_file: dict[str, str] = {}
        for node in graph.get("nodes", []):
            if node.get("kind") != "file":
                continue
            fid = str(node["id"])
            file_ids.add(fid)
            # Build module name variants: src/sentry/foo/bar.py → sentry.foo.bar
            p = fid
            for prefix in ("src/", "lib/", "pkg/"):
                if p.startswith(prefix):
                    p = p[len(prefix):]
                    break
            if p.endswith(".py"):
                p = p[:-3]
            elif p.endswith("/__init__.py"):
                p = p[:-12]
            mod = p.replace("/", ".")
            module_to_file[mod] = fid
            # Also map the raw path without extension
            module_to_file[p] = fid

        def _resolve_to_file(target: str) -> str | None:
            """Resolve a module name or file path to a file ID."""
            if target in file_ids:
                return target
            # Try direct module lookup
            if target in module_to_file:
                return module_to_file[target]
            # Try stripping leading dots (relative imports)
            stripped = target.lstrip(".")
            if stripped in module_to_file:
                return module_to_file[stripped]
            return None

        # adj[file_id] = list of file_ids it imports
        # rev_adj[file_id] = list of file_ids that import it
        adj: dict[str, list[str]] = {}
        rev_adj: dict[str, list[str]] = {}
        for edge in graph.get("edges", []):
            rel = edge.get("rel", "")
            if rel not in ("imports", "calls", "uses"):
                continue
            src = str(edge.get("from", ""))
            dst_raw = str(edge.get("to", ""))
            if not src or not dst_raw:
                continue
            src_file = _resolve_to_file(src) or (src if src in file_ids else None)
            dst_file = _resolve_to_file(dst_raw)
            if src_file and dst_file:
                adj.setdefault(src_file, []).append(dst_file)
                rev_adj.setdefault(dst_file, []).append(src_file)

        # ── Step 3: BFS from crash files to find blast radius ─────────────────
        crash_files = [f["file"] for f in unique_frames if f["file"] in file_ids]
        # Also try fuzzy match if exact not found
        if not crash_files:
            for frame in unique_frames:
                fname = frame["file"]
                # Try suffix match
                for fid in file_ids:
                    if fid.endswith(fname) or fname.endswith(fid.split("/")[-1]):
                        crash_files.append(fid)
                        break

        blast_radius: dict[str, int] = {}  # file -> hop distance
        queue = [(f, 0) for f in crash_files]
        visited: set[str] = set(crash_files)
        while queue:
            cur, depth = queue.pop(0)
            blast_radius[cur] = depth
            if depth >= max_radius:
                continue
            # Traverse both directions: what this file calls AND what calls this file
            for neighbor in adj.get(cur, []) + rev_adj.get(cur, []):
                if neighbor not in visited and neighbor in file_ids:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))

        # ── Step 4: Score by git recency ──────────────────────────────────────
        git_recency: dict[str, int] = {}  # file -> days since last commit
        try:
            result = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "log", "--name-only", "--format=", "--since=30 days ago"],
                capture_output=True, text=True, timeout=5
            )
            changed_recently: set[str] = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    changed_recently.add(line)
            for f in blast_radius:
                git_recency[f] = 0 if f in changed_recently else 30
        except Exception:
            pass

        # ── Step 5: Score and rank impact files ──────────────────────────────
        # Score = (max_radius - hop_distance) * 10 + recency_bonus
        scored_files: list[dict[str, Any]] = []
        for fpath, hop in blast_radius.items():
            recency_bonus = 5 if git_recency.get(fpath, 30) == 0 else 0
            score = (max_radius - hop) * 10 + recency_bonus
            scored_files.append({"file": fpath, "hop": hop, "score": score, "recently_changed": git_recency.get(fpath, 30) == 0})
        scored_files.sort(key=lambda x: -x["score"])

        # ── Step 6: Look up likely root cause function in symbol index ────────
        root_cause_symbols: list[dict[str, Any]] = []
        for frame in unique_frames[:3]:
            func = frame.get("function", "")
            if not func or func in ("<module>", "<anonymous>", "main"):
                continue
            sym_id = f"{frame['file']}::{func}"
            if sym_id in sym_index:
                meta = sym_index[sym_id]
                root_cause_symbols.append({
                    "symbol": sym_id,
                    "line": meta.get("line_start", frame["line"]),
                    "file": frame["file"],
                })

        # ── Step 7: Find unresolved hops (missing from index) ────────────────
        unresolved: list[str] = []
        for fpath in list(blast_radius.keys())[:10]:
            for neighbor in adj.get(fpath, []):
                if neighbor not in file_ids:
                    unresolved.append(neighbor)
        unresolved = list(set(unresolved))[:5]

        # ── Step 8: Get last git modifier for crash files ─────────────────────
        last_modified_by: dict[str, str] = {}
        try:
            for cf in crash_files[:3]:
                result = subprocess.run(
                    ["git", "-C", str(PROJECT_ROOT), "log", "-1", "--format=%an %ar", "--", cf],
                    capture_output=True, text=True, timeout=3
                )
                if result.stdout.strip():
                    last_modified_by[cf] = result.stdout.strip()
        except Exception:
            pass

        # ── Output ────────────────────────────────────────────────────────────
        top_files = [f["file"] for f in scored_files[:5]]
        coverage_pct = round(len(crash_files) / max(len(unique_frames), 1) * 100)

        out: dict[str, Any] = {
            "ok": True,
            "crash_files": crash_files,
            "frames_parsed": len(unique_frames),
            "coverage": f"{coverage_pct}% of frames found in graph",
            "root_cause_symbols": root_cause_symbols,
            "blast_radius": len(blast_radius),
            "impact_files": scored_files[:max_radius * 2],
            "recommended_files": top_files,
            "last_modified": last_modified_by,
            "unresolved_deps": unresolved,
        }
        if not crash_files:
            out["warning"] = "No frames matched graph index — try graph_scan first or check file paths"
            # Fall back to graph_continue-style retrieval using error text as query
            fallback = _local_chat_fix(error[:200], 5, 8)
            if fallback:
                out["fallback_files"] = [str(f.get("id", "")) for f in fallback.get("graph_files", [])[:5]]

        _log_tool("graph_debug", {"frames": len(unique_frames), "crash_files": crash_files, "blast_radius": len(blast_radius)})
        return out

    # ── GrapeRoot Review ──────────────────────────────────────────────────────
    @mcp.tool()
    def review_pr(
        pr_url: str,
        dry_run: bool = False,
        post: bool = True,
    ) -> dict[str, Any]:
        """Run a full GrapeRoot code review on a GitHub PR.

        Uses the local AST graph (already built by dgc-pro) for graph-proven
        context — blast radius, orphaned methods, cross-file consistency.
        All static detectors run deterministically. LLM (arch + security) run
        with o1 for deep reasoning. Results posted to the PR as a GitHub review.

        Args:
            pr_url: GitHub PR URL, e.g. https://github.com/owner/repo/pull/123
            dry_run: If True, print findings but do not post to GitHub.
            post: If False, return findings without posting (same as dry_run=True).

        Returns:
            dict with 'findings', 'total', 'posted' keys.
        """
        import subprocess, sys, os, json
        from pathlib import Path

        # Use enterprise review script (clean local-first implementation)
        review_script = Path(__file__).parent / "review_enterprise.py"
        if not review_script.exists():
            review_script = Path(__file__).parent / "review.py"  # fallback
        if not review_script.exists():
            return {"error": f"review_enterprise.py not found at {review_script.parent}"}

        cmd = [sys.executable, str(review_script), pr_url]
        if dry_run or not post:
            cmd.append("--dry")

        json_out = f"/tmp/gr-review-mcp-{abs(hash(pr_url))}.json"
        cmd += ["--json-out", json_out]

        # Use MCP port so review.py picks up the running graph server
        env = {**os.environ, "GRAPEROOT_PORT": str(MCP_PORT)}
        _log_tool("review_pr", {"pr_url": pr_url, "dry_run": dry_run})

        try:
            result = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=300,
                cwd=str(review_script.parent),
            )
        except subprocess.TimeoutExpired:
            return {"error": "review timed out after 300s"}

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        findings, cost = [], 0.0
        try:
            out = json.loads(Path(json_out).read_text())
            report = out.get("report", {})
            findings = report.get("inline_comments") or out.get("findings", [])
            cost = out.get("cost_usd", 0.0)
            Path(json_out).unlink(missing_ok=True)
        except Exception:
            pass

        summary_lines = []
        for f in findings:
            sev = f.get("severity", "?")
            title = f.get("title", "")
            summary_lines.append(f"[{sev}] {title}")

        return {
            "total": len(findings),
            "posted": post and not dry_run and result.returncode == 0,
            "cost_usd": cost,
            "findings": summary_lines,
            "stdout_tail": stdout[-800:] if stdout else "",
            "stderr_tail": stderr[-400:] if stderr else "",
        }

    # ── Feature 2: Decision Memory ────────────────────────────────────────────
    @mcp.tool()
    def graph_add_decision(
        decision: str,
        files: list[str] | None = None,
        why: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record an architectural or debugging decision with its WHY context.

        This persists decisions to both the context store and the action graph,
        so they surface automatically in future graph_read and graph_continue calls.

        Args:
            decision: The decision made (max 30 words).
            files: Files this decision applies to.
            why: Why this decision was made (context, constraints, alternatives rejected).
            tags: Topic tags (e.g. ["auth", "performance"]).
        """
        import datetime as _dt

        entry: dict[str, Any] = {
            "type": "decision",
            "content": decision,
            "why": why,
            "tags": tags or [],
            "files": files or [],
            "date": _dt.date.today().isoformat(),
        }
        existing = _load_context_store()
        existing.append(entry)
        _save_context_store(existing)

        # Also store in action graph decisions list for graph_action_summary surfacing
        g = _load_action_graph()
        decisions_list: list[dict[str, Any]] = g.setdefault("decisions", [])
        decisions_list.append({
            "ts": int(time.time()),
            "summary": decision,
            "why": why,
            "files": files or [],
            "tags": tags or [],
        })
        # Cap decisions list at 50
        if len(decisions_list) > 50:
            g["decisions_archive"] = f"{len(decisions_list) - 40} older decisions archived"
            g["decisions"] = decisions_list[-40:]
        _save_action_graph(g)

        _log_tool("graph_add_decision", {"decision": decision[:50], "files": files or []})
        return {
            "ok": True,
            "stored": decision,
            "why": why,
            "files": files or [],
            "total_decisions": len(g.get("decisions", [])),
        }

    return mcp


_HEARTBEAT_INTERVAL_SEC = 15 * 60  # ping every 15 minutes


def _ping_license_server() -> None:
    """Startup ping + periodic heartbeat so last_seen stays current during active sessions.
    Auto-creates identity.json on first run. Runs as a daemon thread (dies with the process)
    and works identically on macOS, Linux, and Windows."""
    import platform as _platform
    import threading
    import uuid

    def _generate_random_id() -> str:
        """Generate a random installation ID (no hardware info collected)."""
        return uuid.uuid4().hex

    def _load_payload() -> bytes | None:
        """Load (or create) identity and return the encoded JSON payload."""
        import datetime
        try:
            identity_path = Path.home() / ".dual-graph" / "identity.json"
            if not identity_path.exists():
                identity = {
                    "machine_id": _generate_random_id(),
                    "platform": _platform.system().lower(),
                    "installed_date": datetime.date.today().isoformat(),
                    "tool": "mcp-auto",
                }
                identity_path.parent.mkdir(parents=True, exist_ok=True)
                identity_path.write_text(json.dumps(identity), encoding="utf-8")
            else:
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
                # Existing users: just stamp installed_date, keep their ID intact
                if "installed_date" not in identity:
                    identity["installed_date"] = datetime.date.today().isoformat()
                    identity_path.write_text(json.dumps(identity), encoding="utf-8")
            machine_id = identity.get("machine_id", "")
            if not machine_id:
                return None
            return json.dumps({
                "machine_id": machine_id,
                "platform": identity.get("platform", "unknown"),
                "tool": identity.get("tool", "unknown"),
            }).encode("utf-8")
        except Exception:
            return None

    def _send(payload: bytes) -> None:
        try:
            req = urllib.request.Request(
                "https://api.graperoot.dev/ping",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=4)
        except Exception:
            pass  # never crash the MCP server

    def _heartbeat_loop() -> None:
        payload = _load_payload()
        if payload is None:
            return
        _send(payload)  # immediate startup ping
        while True:
            time.sleep(_HEARTBEAT_INTERVAL_SEC)
            _send(payload)  # periodic heartbeat

    threading.Thread(target=_heartbeat_loop, daemon=True).start()


def main() -> int:
    import anyio
    import uvicorn
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    preferred = int(os.environ.get("PORT", 8080))
    port = preferred
    for _p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                _s.bind(("127.0.0.1", _p))
                port = _p
                break
            except OSError:
                continue
    else:
        raise RuntimeError(f"no free port in {preferred}-{preferred + 19}")
    DG_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Clear tool call log so /savings only reports THIS session's data
    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    if port != preferred:
        logging.getLogger(__name__).warning("Port %d in use, using %d", preferred, port)
        os.environ["PORT"] = str(port)
        try:
            (DG_DATA_DIR / "mcp_port").write_text(str(port), encoding="utf-8")
        except OSError:
            pass
    _ping_license_server()
    _bind_host = os.environ.get("HOST", "127.0.0.1")
    mcp = build_server(host=_bind_host, port=port)

    # Custom /ingest-graph route: accepts pre-built graph JSON from local machine
    # so users can run: graph_builder.py locally -> POST here -> chat via MCP
    async def ingest_graph(request: Request) -> JSONResponse:
        try:
            graph = await request.json()
            if "nodes" not in graph or "edges" not in graph:
                return JSONResponse({"ok": False, "error": "missing nodes/edges"}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        graph_json = DG_DATA_DIR / "info_graph.json"
        graph_json.parent.mkdir(parents=True, exist_ok=True)
        graph_json.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        # Build and persist symbol index for O(1) graph_read lookups.
        sym_index = _build_symbol_index(graph)
        SYMBOL_INDEX_FILE.write_text(json.dumps(sym_index), encoding="utf-8")
        _reconcile_context_store(sym_index)
        # Invalidate retrieval cache so new graph is used immediately.
        RETRIEVAL_CACHE_FILE.unlink(missing_ok=True)
        return JSONResponse({
            "ok": True,
            "node_count": graph.get("node_count", len(graph["nodes"])),
            "edge_count": graph.get("edge_count", len(graph["edges"])),
        })

    # ── Static file routes: serve dg + graph_builder so users can curl-install ──
    _HERE = Path(__file__).resolve().parent

    async def serve_graph_builder(request: Request) -> Response:
        gb = _HERE / "graph_builder.py"
        return Response(gb.read_text(encoding="utf-8"), media_type="text/plain")

    async def serve_dg(request: Request) -> Response:
        return Response((_HERE / "dg").read_text(encoding="utf-8"), media_type="text/plain")

    async def serve_dgc(request: Request) -> Response:
        return Response((_HERE / "dgc").read_text(encoding="utf-8"), media_type="text/plain")

    async def serve_mcp_server(request: Request) -> Response:
        return Response((_HERE / "mcp_graph_server.py").read_text(encoding="utf-8"), media_type="text/plain")

    async def serve_install(request: Request) -> Response:
        base = str(request.base_url).rstrip("/")
        script = f"""\
#!/usr/bin/env bash
set -euo pipefail
BASE_URL="{base}"
INSTALL_DIR="$HOME/.dual-graph"
mkdir -p "$INSTALL_DIR"

echo "[install] Downloading files..."
curl -sSL "$BASE_URL/graph_builder.py"    -o "$INSTALL_DIR/graph_builder.py"
curl -sSL "$BASE_URL/mcp_graph_server.py" -o "$INSTALL_DIR/mcp_graph_server.py"
curl -sSL "$BASE_URL/dg"  -o "$INSTALL_DIR/dg"  && chmod +x "$INSTALL_DIR/dg"
curl -sSL "$BASE_URL/dgc" -o "$INSTALL_DIR/dgc" && chmod +x "$INSTALL_DIR/dgc"

echo "[install] Installing Python dependencies..."
python3 -m pip install "mcp>=1.3.0" uvicorn anyio starlette --quiet

# Add to PATH if not already there
SHELL_RC="$HOME/.zshrc"
[[ "$SHELL" == */bash ]] && SHELL_RC="$HOME/.bashrc"
if ! grep -q '.dual-graph' "$SHELL_RC" 2>/dev/null; then
  echo 'export PATH="$PATH:$HOME/.dual-graph"' >> "$SHELL_RC"
  echo "[install] Added ~/.dual-graph to PATH in $SHELL_RC"
fi

echo ""
echo "[install] Done! Run these once:"
echo "  source $SHELL_RC"
echo ""
echo "  # Register for Codex CLI (uses Railway):"
echo "  codex mcp add dual-graph --url $BASE_URL/mcp"
echo ""
echo "  # Register for Claude Code (uses local server):"
echo "  claude mcp add --transport http dual-graph http://localhost:8080/mcp"
echo ""
echo "Then per project:"
echo "  dg /path/to/project    # Codex CLI  (Railway MCP)"
echo "  dgc /path/to/project   # Claude Code (local MCP, fully private)"
"""
        return Response(script, media_type="text/plain")

    # Get the MCP app with its lifespan (session manager) intact.
    # IMPORTANT: do NOT wrap mcp_app in a new Starlette app via Mount("/") —
    # that kills the inner lifespan so the session manager never starts,
    # causing every /mcp request to return HTTP 500.
    # Instead, prepend our custom routes directly into mcp_app's own router
    # so it remains the top-level ASGI app and its lifespan runs normally.
    async def prime(request: Request) -> Response:
        """Compact plain-text action graph summary for Claude Code hooks (SessionStart/PreCompact)."""
        g = _load_action_graph()
        files_meta = g.get("files", {})
        decisions = g.get("decisions", [])[-5:]
        decisions_archive = g.get("decisions_archive", "")
        memories = _search_context_store("", [], limit=5)

        edited = sorted(
            [(f, m) for f, m in files_meta.items() if int(m.get("edited_count", 0)) > 0],
            key=lambda x: -int(x[1].get("edited_count", 0)),
        )

        lines = ["# Dual-Graph Context", f"Project: {PROJECT_ROOT}"]
        graph_json = DG_DATA_DIR / "info_graph.json"
        if graph_json.exists():
            try:
                gdata = json.loads(graph_json.read_text(encoding="utf-8"))
                lines.append(f"Files scanned: {gdata.get('file_count', gdata.get('node_count', '?'))}")
            except Exception:
                pass

        if edited:
            lines.append("\n## Recently edited")
            for f, m in edited[:8]:
                lines.append(f"- {f} (edited {int(m.get('edited_count', 0))}x)")

        if decisions or decisions_archive:
            lines.append("\n## Decisions")
            if decisions_archive:
                lines.append(f"(older) {decisions_archive}")
            for d in decisions:
                lines.append(f"- {d.get('summary', '')}")

        if memories:
            lines.append("\n## Memories")
            for mem in memories:
                label = "[STALE] " if mem.get("stale") else ""
                anchor = mem.get("symbol_id") or mem.get("file_path") or ""
                suffix = f" ({anchor})" if anchor else ""
                lines.append(f"- {label}{mem.get('content', '')}{suffix}")

        if not edited and not decisions and not memories:
            lines.append("\n(No prior edits recorded — graph is fresh)")

        return Response("\n".join(lines), media_type="text/plain")

    # ── Token usage log endpoint ────────────────────────────────────────────
    # The Stop hook POSTs session usage here. Saved to .dual-graph/token_log.jsonl
    # so the dashboard can read it via /api/token-summary and /api/token-dataset.
    async def log_token_usage(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return Response('{"ok":false}', status_code=400, media_type="application/json")
        ts = datetime.now(timezone.utc).isoformat()
        # input_tokens = raw (non-cached) input only. Cache sent separately.
        raw = int(body.get("input_tokens", 0))
        output_tk = int(body.get("output_tokens", 0))
        cc = int(body.get("cache_creation_input_tokens", 0))
        cr = int(body.get("cache_read_input_tokens", 0))
        model = str(body.get("model", "unknown"))
        # Cost calculation
        is_opus = "opus" in model.lower()
        if is_opus:
            cost = (raw * 15.0 + cc * 18.75 + cr * 1.50 + output_tk * 75.0) / 1_000_000
        else:  # sonnet / default
            cost = (raw * 3.0 + cc * 3.75 + cr * 0.30 + output_tk * 15.0) / 1_000_000
        event = {
            "timestamp": ts,
            "model": model,
            "input_tokens": raw,
            "output_tokens": output_tk,
            "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr,
            "total_tokens": raw + cc + cr + output_tk,
            "cost_usd": round(cost, 6),
            "project": str(body.get("project", "")),
            "description": str(body.get("description", "")),
            "mode": "session",
        }
        log_path = DG_DATA_DIR / "token_log.jsonl"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass
        return Response(json.dumps({"ok": True, "cost_usd": event["cost_usd"]}), media_type="application/json")

    # ── Feature 4: System map endpoints ──────────────────────────────────────
    async def api_system_map(request: Request) -> Response:
        """Return a compact system map: core modules, hotspots, change zones."""
        graph_json = DG_DATA_DIR / "info_graph.json"
        if not graph_json.exists():
            return Response('{"ok":false,"error":"No graph"}', status_code=404, media_type="application/json")
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return Response(json.dumps({"ok": False, "error": str(e)}), status_code=500, media_type="application/json")

        # Build set of actual source file IDs
        source_file_ids: set[str] = set()
        for node in graph.get("nodes", []):
            if node.get("kind") == "file":
                source_file_ids.add(str(node.get("id", "")))

        # Count incoming/outgoing edges — only count edges between source files
        incoming: dict[str, int] = {}
        outgoing: dict[str, int] = {}
        for edge in graph.get("edges", []):
            if edge.get("rel") not in ("imports", "calls", "uses"):
                continue
            src = str(edge.get("from", ""))
            dst = str(edge.get("to", ""))
            if src in source_file_ids:
                outgoing[src] = outgoing.get(src, 0) + 1
            if dst in source_file_ids:
                incoming[dst] = incoming.get(dst, 0) + 1

        # File metadata
        files: list[dict[str, Any]] = []
        for node in graph.get("nodes", []):
            if node.get("kind") != "file":
                continue
            fid = str(node.get("id", ""))
            inc = incoming.get(fid, 0)
            out_deg = outgoing.get(fid, 0)
            files.append({
                "id": fid,
                "incoming": inc,
                "outgoing": out_deg,
                "symbols": int(node.get("symbol_count", 0)),
            })

        # Hotspots: most imported source files (high incoming among source files)
        # Fall back to high-outgoing + symbol count if incoming is sparse
        has_incoming = any(f["incoming"] > 0 for f in files)
        if has_incoming:
            hotspots = sorted(files, key=lambda x: -x["incoming"])[:20]
        else:
            # Graph stores imports as module names not file paths — rank by outgoing+symbols
            hotspots = sorted(files, key=lambda x: -(x["outgoing"] + x["symbols"] * 2))[:20]
        # Entry points: high outgoing (orchestrators — urls.py, factories, main files)
        entry_points = sorted(files, key=lambda x: -x["outgoing"])[:10]
        # Complex files: highest symbol count
        complex_files = sorted(files, key=lambda x: -x["symbols"])[:10]
        # Islands: no connections at all
        islands = [f for f in files if f["incoming"] == 0 and f["outgoing"] == 0][:10]

        # Recent changes from git
        recent_changed: list[str] = []
        try:
            result = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "log", "--name-only", "--format=", "--since=7 days ago"],
                capture_output=True, text=True, timeout=5
            )
            recent_changed = list({l.strip() for l in result.stdout.splitlines() if l.strip()})[:20]
        except Exception:
            pass

        out_data = {
            "ok": True,
            "total_files": len(files),
            "total_edges": len(graph.get("edges", [])),
            "hotspots": hotspots,
            "entry_points": entry_points,
            "complex_files": complex_files,
            "islands": islands,
            "recently_changed": recent_changed,
            "project_root": str(PROJECT_ROOT),
        }
        return Response(json.dumps(out_data), media_type="application/json")

    async def api_hotspots(request: Request) -> Response:
        """Return top files by incoming dependency count (highest blast radius)."""
        graph_json = DG_DATA_DIR / "info_graph.json"
        if not graph_json.exists():
            return Response('{"ok":false,"error":"No graph"}', status_code=404, media_type="application/json")
        try:
            graph = json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception as e:
            return Response(json.dumps({"ok": False, "error": str(e)}), status_code=500, media_type="application/json")

        incoming: dict[str, int] = {}
        for edge in graph.get("edges", []):
            if edge.get("rel") in ("imports", "calls", "uses"):
                dst = str(edge.get("to", ""))
                if dst:
                    incoming[dst] = incoming.get(dst, 0) + 1

        top_n = int(request.query_params.get("n", "30"))
        ranked = sorted(incoming.items(), key=lambda x: -x[1])[:top_n]
        return Response(json.dumps({
            "ok": True,
            "hotspots": [{"file": f, "incoming_deps": c} for f, c in ranked],
        }), media_type="application/json")

    async def api_savings(request: Request) -> Response:
        """Compute honest, measured savings from mcp_tool_calls.jsonl.

        Savings sources (all measured, not estimated):
        1. TAR: partial file reads vs full file — capped at Read tool's 2000-line limit
           (vanilla Claude also reads partially for big files)
        2. Cross-turn pointers: re-reads avoided — discounted because vanilla Claude
           also has conversation history and may not re-read
        3. Shadow grep: exploration output avoided (only counted when query was vague)
        4. Shadow file reads: files inlined that vanilla would've Read separately

        Subtract GrapeRoot's own overhead: every graph_continue and graph_read response
        has JSON metadata that vanilla Read doesn't have.
        """
        empty = {"ok": True, "total_turns": 0, "tool_hits": {},
            "tokens_served": 0, "tokens_avoided_tar": 0,
            "tokens_avoided_cross_turn": 0, "tar": 0.0,
            "shadow_savings": {"grep_avoided": {"measurements": 0, "tokens": 0},
                               "file_reads_avoided": {"measurements": 0, "tokens": 0},
                               "total_tokens": 0},
            "graperoot_overhead_tokens": 0,
            "cost": {"model": "opus", "price_per_m_input": 15.0,
                     "vanilla_cost_usd": 0.0, "graperoot_cost_usd": 0.0,
                     "saved_usd": 0.0, "savings_pct": 0.0}}
        if not LOG_FILE.exists():
            return Response(json.dumps(empty), media_type="application/json")

        tokens_served = 0
        tokens_avoided_tar = 0
        tokens_avoided_cross_turn = 0
        full_tokens = 0
        tool_hits: dict[str, int] = {}
        max_turn = 0
        graperoot_overhead_tokens = 0
        shadow_grep_tokens = 0
        shadow_grep_count = 0
        shadow_file_read_tokens = 0
        shadow_file_read_count = 0
        seen_files: set[str] = set()

        try:
            with LOG_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    turn = int(entry.get("turn", 0))
                    if turn > max_turn:
                        max_turn = turn
                    tool_name = entry.get("tool")

                    # Shadow measurements (background threads logged these)
                    if tool_name == "shadow_grep":
                        p = entry.get("payload", {})
                        shadow_grep_tokens += int(p.get("tokens_would_have_cost", 0))
                        shadow_grep_count += 1
                        continue
                    if tool_name == "shadow_file_reads":
                        p = entry.get("payload", {})
                        shadow_file_read_tokens += int(p.get("tokens_avoided", 0))
                        shadow_file_read_count += 1
                        continue

                    # GrapeRoot overhead: measure actual response payload size
                    if tool_name == "graph_continue":
                        p = entry.get("payload", {})
                        # Measure the actual logged payload as proxy for response size
                        payload_chars = len(json.dumps(p))
                        graperoot_overhead_tokens += payload_chars // 4
                        continue

                    if tool_name not in ("graph_read", "native_read"):
                        continue

                    p = entry.get("payload", {})
                    mode = p.get("mode", "unknown")
                    tool_hits[mode] = tool_hits.get(mode, 0) + 1
                    fc = int(p.get("full_file_chars", 0))
                    rc = int(p.get("response_chars", 0))
                    # Extract base file path (strip ::symbol suffix)
                    raw_file = p.get("file", "") or ""
                    base_file = raw_file.split("::")[0] if "::" in raw_file else raw_file

                    if mode == "cross_turn_pointer":
                        capped = min(fc, _READ_TOOL_MAX_CHARS) if fc > 0 else 0
                        tokens_avoided_cross_turn += capped // 4
                        graperoot_overhead_tokens += 20
                        continue

                    if mode == "dedupe_preview":
                        if fc > 0:
                            tokens_avoided_tar += max(0, fc - rc) // 4
                            tokens_served += rc // 4
                            full_tokens += fc // 4
                        graperoot_overhead_tokens += 15
                        continue

                    if fc > 0:
                        capped_fc = min(fc, _READ_TOOL_MAX_CHARS)
                        tokens_served += rc // 4
                        graperoot_overhead_tokens += 25
                        if base_file and base_file in seen_files:
                            # Same file read again (different symbol) — vanilla Claude
                            # would have Read this file only once. Don't double-count.
                            pass
                        else:
                            if base_file:
                                seen_files.add(base_file)
                            full_tokens += capped_fc // 4
                            avoided = max(0, capped_fc - rc) // 4
                            tokens_avoided_tar += avoided
                    else:
                        tokens_served += int(p.get("response_tokens_est", 0))

        except Exception as e:
            return Response(json.dumps({"ok": False, "error": str(e)}), status_code=500, media_type="application/json")

        tar = round(tokens_avoided_tar / full_tokens, 4) if full_tokens > 0 else 0.0

        # Dollar cost — honest formula
        # Vanilla = what we served + what we avoided (TAR + cross-turn + shadow)
        # GrapeRoot = what we served + our overhead
        price_per_m = 15.0
        total_shadow = shadow_grep_tokens + shadow_file_read_tokens
        vanilla_tokens = tokens_served + tokens_avoided_tar + tokens_avoided_cross_turn + total_shadow
        graperoot_tokens = tokens_served + graperoot_overhead_tokens
        vanilla_cost = (vanilla_tokens / 1_000_000) * price_per_m
        graperoot_cost = (graperoot_tokens / 1_000_000) * price_per_m
        saved_usd = max(0.0, vanilla_cost - graperoot_cost)
        savings_pct = round((saved_usd / vanilla_cost) * 100, 1) if vanilla_cost > 0 else 0.0

        total_avoided = tokens_avoided_tar + tokens_avoided_cross_turn + total_shadow
        total_reads = sum(v for k, v in tool_hits.items() if k != "cross_turn_pointer")
        return Response(json.dumps({
            "ok": True,
            "total_turns": max_turn,
            "tool_hits": tool_hits,
            "tar": tar,
            "tokens_served": tokens_served,
            "tokens_avoided": total_avoided,
            "tokens_avoided_tar": tokens_avoided_tar,
            "tokens_avoided_cross_turn": tokens_avoided_cross_turn,
            "graperoot_overhead_tokens": graperoot_overhead_tokens,
            "unique_files_read": len(seen_files),
            "total_reads": total_reads,
            "shadow_savings": {
                "grep_avoided": {
                    "measurements": shadow_grep_count,
                    "tokens": shadow_grep_tokens,
                },
                "file_reads_avoided": {
                    "measurements": shadow_file_read_count,
                    "tokens": shadow_file_read_tokens,
                },
                "total_tokens": total_shadow,
            },
            "cost": {
                "model": "opus",
                "price_per_m_input": price_per_m,
                "vanilla_tokens": vanilla_tokens,
                "graperoot_tokens": graperoot_tokens,
                "vanilla_cost_usd": round(vanilla_cost, 4),
                "graperoot_cost_usd": round(graperoot_cost, 4),
                "saved_usd": round(saved_usd, 4),
                "savings_pct": savings_pct,
            },
        }), media_type="application/json")

    mcp_app = mcp.streamable_http_app()

    # ── Session middleware (pure-ASGI, no BaseHTTPMiddleware) ──────────────
    # BaseHTTPMiddleware spawns a new task context, breaking ContextVar
    # propagation. Pure-ASGI middleware runs in the same task context as the
    # handler, so ContextVar set here IS visible inside tool functions.
    _starlette_app = mcp_app

    async def session_middleware(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            sid = (headers.get(b"mcp-session-id", b"")
                   or headers.get(b"x-mcp-session-id", b"")).decode() or "default"
            _set_session(sid)
        await _starlette_app(scope, receive, send)

    # Add custom routes to the Starlette app BEFORE wrapping with middleware
    _starlette_app.router.routes[0:0] = [
        Route("/prime", prime, methods=["GET"]),
        Route("/log", log_token_usage, methods=["POST"]),
        Route("/ingest-graph", ingest_graph, methods=["POST"]),
        Route("/install.sh", serve_install, methods=["GET"]),
        Route("/dgc", serve_dgc, methods=["GET"]),
        Route("/dg", serve_dg, methods=["GET"]),
        Route("/graph_builder.py", serve_graph_builder, methods=["GET"]),
        Route("/mcp_graph_server.py", serve_mcp_server, methods=["GET"]),
        Route("/api/system-map", api_system_map, methods=["GET"]),
        Route("/api/hotspots", api_hotspots, methods=["GET"]),
        Route("/savings", api_savings, methods=["GET"]),
    ]

    mcp_app = session_middleware  # type: ignore[assignment]
    # ──────────────────────────────────────────────────────────────────────

    async def serve() -> None:
        # workers=1 is critical for _CURRENT_SID session routing — multiple workers
        # would each have their own _SESSION_STATES and _CURRENT_SID globals.
        # For multi-worker deployments, replace _CURRENT_SID with a proper
        # per-request ContextVar solution or Redis session store.
        config = uvicorn.Config(mcp_app, host=_bind_host, port=port, log_level="error", workers=1)
        await uvicorn.Server(config).serve()

    anyio.run(serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
