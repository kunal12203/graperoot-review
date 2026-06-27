"""
resilience_checks.py — Detects resilience anti-patterns in Go, Python, and JS/TS source files.

Checks implemented:
  RES-001  HTTP calls without timeout
  RES-002  Retry loops without exponential backoff
  RES-003  Feature flags without fallback default
  RES-004  Missing circuit breakers in HTTP-heavy files
"""

from __future__ import annotations

import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv"}
_MAX_FILE_SIZE = 500 * 1024  # 500 KB

# ---------------------------------------------------------------------------
# Pre-compiled patterns — HTTP timeout (RES-001)
# ---------------------------------------------------------------------------

# Go
_GO_HTTP_NOCLIENT = re.compile(r"\bhttp\.(Get|Post|Head|PostForm)\(")
_GO_HTTP_DEFAULT_CLIENT = re.compile(r"\bhttp\.DefaultClient\.Do\(")
_GO_HTTP_NEW_CLIENT = re.compile(r"&http\.Client\{")
_GO_TIMEOUT_FIELD = re.compile(r"\bTimeout\s*:")
_GO_CTX_TIMEOUT = re.compile(r"\bcontext\.(WithTimeout|WithDeadline)\(")

# Python
_PY_HTTP_CALL = re.compile(
    r"\b(requests|httpx)\.(get|post|put|patch|delete|head|request|options)\("
)
_PY_TIMEOUT_KW = re.compile(r"\btimeout\s*=")

# JS/TS
_JS_FETCH = re.compile(r"\bfetch\(")
_JS_AXIOS = re.compile(
    r"\baxios\.(get|post|put|delete|patch|request|head|options)\("
)
_JS_TIMEOUT_SIGNAL = re.compile(r"\b(signal|timeout)\s*:", re.IGNORECASE)
_JS_ABORT_SIGNAL = re.compile(r"\bAbortSignal\b")
_JS_TIMEOUT_FN_NAME = re.compile(
    r"\bfunction\s+(timeout\w*|withTimeout\w*|fetchWithTimeout\w*)\b"
    r"|\b(timeout\w*|withTimeout\w*|fetchWithTimeout\w*)\s*[=:]\s*(async\s+)?(function|\()",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Pre-compiled patterns — Retry without backoff (RES-002)
# ---------------------------------------------------------------------------

_GO_RETRY_LOOP = re.compile(
    r"\bfor\b[^{]*\b(retries|attempts|maxRetry|maxAttempts|retry|attempt)\b"
)
_GO_BACKOFF_EXPR = re.compile(
    r"math\.Pow\s*\(\s*2|backoff|time\.Sleep\s*\(\s*time\.Duration\s*\([^)]*\*"
)
_GO_BACKOFF_IMPORT = re.compile(
    r'"(cenkalti/backoff|go-retry|hashicorp/go-retryablehttp|backoff)'
)

_PY_RETRY_LOOP = re.compile(
    r"^\s*(for\s+\w+\s+in\s+range\s*\(|while\s+\w*(retry|attempt|retries|attempts)\w*\s*[<>!=])",
    re.IGNORECASE,
)
_PY_FIXED_SLEEP = re.compile(r"\btime\.sleep\s*\(\s*[\d.]+\s*\)")
_PY_EXP_SLEEP = re.compile(
    r"\btime\.sleep\s*\(.*([\*\^]|\*\*|pow|backoff|delay\s*\*|sleep\s*\*)"
)
_PY_BACKOFF_IMPORT = re.compile(r"^\s*(import|from)\s+(tenacity|backoff|retry)\b")
_PY_RETRY_VAR_IN_LOOP = re.compile(r"\b(retry|attempt|retries|attempts)\b", re.IGNORECASE)

_JS_RETRY_LOOP = re.compile(
    r"\b(for|while)\b[^{;]*\b(retry|attempt|retries|attempts|maxRetry|maxAttempts)\b",
    re.IGNORECASE,
)
_JS_FIXED_SETTIMEOUT = re.compile(r"\bsetTimeout\s*\([^,]+,\s*\d+\s*\)")
_JS_EXP_SETTIMEOUT = re.compile(r"\bsetTimeout\s*\([^,]+,\s*Math\.pow\s*\(")
_JS_BACKOFF_IMPORT = re.compile(
    r"""(require|import).*["'](p-retry|async-retry|retry|exponential-backoff)["']"""
)

# ---------------------------------------------------------------------------
# Pre-compiled patterns — Feature flags without fallback (RES-003)
# ---------------------------------------------------------------------------

# LaunchDarkly Go: client.BoolVariation(key, user, default)
_LD_GO_VARIATION = re.compile(r"\bclient\.BoolVariation\s*\(([^)]*)\)")
# LaunchDarkly Python
_LD_PY_VARIATION = re.compile(
    r"\b(ldclient\.get\(\)|client)\.variation\s*\(([^)]*)\)"
)
# LaunchDarkly JS/TS
_LD_JS_VARIATION = re.compile(r"\b(client|ldclient)\.variation\s*\(([^)]*)\)")
# Unleash Python
_UNLEASH_PY_ENABLED = re.compile(r"\bunleash\.is_enabled\s*\(([^)]*)\)")
# Unleash JS
_UNLEASH_JS_ENABLED = re.compile(r"\bunleash\.isEnabled\s*\(([^)]*)\)")
# Statsig Python/JS
_STATSIG_CHECKGATE = re.compile(r"\bstatsig\.checkGate\s*\(")

# ---------------------------------------------------------------------------
# Pre-compiled patterns — Circuit breakers (RES-004)
# ---------------------------------------------------------------------------

_GO_HTTP_ANY = re.compile(r"\bhttp\.(Get|Post|Head|Do|DefaultClient)\b")
_GO_CB_IMPORT = re.compile(
    r'"(gobreaker|hystrix|sony/gobreaker|circuitbreaker|breaker|go-breaker)'
)

_PY_REQUESTS_CALL = re.compile(r"\brequests\.(get|post|put|patch|delete|head|request)\(")
_PY_CB_IMPORT = re.compile(r"^\s*(import|from)\s+(pybreaker|circuitbreaker|aiobreaker)\b")

_JS_HTTP_ANY = re.compile(r"\b(fetch\(|axios\.)")
_JS_CB_IMPORT = re.compile(
    r"""(require|import).*["'](opossum|cockatiel|circuit-breaker|brakes)["']"""
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_test_file(path: str) -> bool:
    """Return True if path looks like a test file."""
    norm = path.replace("\\", "/")
    return (
        "/test" in norm
        or "_test.go" in norm
        or ".test." in norm
        or ".spec." in norm
    )


def _walk_source_files(project_root: str, extensions: tuple[str, ...]):
    """Yield (abs_path, rel_path) for source files matching extensions."""
    for dirpath, dirnames, filenames in os.walk(project_root):
        # Prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(extensions):
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, project_root)
            if _is_test_file(rel_path):
                continue
            if os.path.getsize(abs_path) > _MAX_FILE_SIZE:
                continue
            yield abs_path, rel_path


def _read_lines(abs_path: str) -> list[str]:
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def _count_args(arg_str: str) -> int:
    """Count comma-separated top-level arguments in an argument string."""
    depth = 0
    count = 1 if arg_str.strip() else 0
    for ch in arg_str:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _surrounding_lines(lines: list[str], idx: int, before: int, after: int) -> str:
    """Return a single string of surrounding lines (0-based idx)."""
    start = max(0, idx - before)
    end = min(len(lines), idx + after + 1)
    return "".join(lines[start:end])


# ---------------------------------------------------------------------------
# RES-001: HTTP calls without timeout
# ---------------------------------------------------------------------------


def find_http_calls_without_timeout(graph: dict, project_root: str) -> list[dict]:
    """
    Scan Go, Python, and JS/TS files for HTTP calls that lack a timeout.
    Returns list of finding dicts with keys: file, line, message, severity, rule_id.
    """
    findings: list[dict] = []

    # --- Go ---
    for abs_path, rel_path in _walk_source_files(project_root, (".go",)):
        lines = _read_lines(abs_path)
        for idx, line in enumerate(lines):
            matched = False
            call_type = ""

            if _GO_HTTP_NOCLIENT.search(line):
                matched = True
                call_type = "http function call"
            elif _GO_HTTP_DEFAULT_CLIENT.search(line):
                matched = True
                call_type = "http.DefaultClient.Do"
            elif _GO_HTTP_NEW_CLIENT.search(line):
                matched = True
                call_type = "&http.Client{}"

            if not matched:
                continue

            # Check prev 5 lines for context timeout
            prev_ctx = _surrounding_lines(lines, idx, 5, 0)
            if _GO_CTX_TIMEOUT.search(prev_ctx):
                continue

            # Check next 15 lines for Timeout field
            next_ctx = _surrounding_lines(lines, idx, 0, 15)
            if _GO_TIMEOUT_FIELD.search(next_ctx):
                continue

            findings.append(
                {
                    "file": rel_path,
                    "line": idx + 1,
                    "message": f"Go {call_type} without Timeout field or context deadline",
                    "severity": "high",
                    "rule_id": "RES-001",
                }
            )

    # --- Python ---
    for abs_path, rel_path in _walk_source_files(project_root, (".py",)):
        lines = _read_lines(abs_path)
        for idx, line in enumerate(lines):
            if not _PY_HTTP_CALL.search(line):
                continue

            # Exclude if timeout= on same line
            if _PY_TIMEOUT_KW.search(line):
                continue

            # Exclude if session timeout set in prev 5 lines
            prev_ctx = _surrounding_lines(lines, idx, 5, 0)
            if _PY_TIMEOUT_KW.search(prev_ctx):
                continue

            call_match = _PY_HTTP_CALL.search(line)
            lib = call_match.group(1) if call_match else "requests"
            method = call_match.group(2) if call_match else "call"

            findings.append(
                {
                    "file": rel_path,
                    "line": idx + 1,
                    "message": f"Python {lib}.{method}() without timeout= argument",
                    "severity": "high",
                    "rule_id": "RES-001",
                }
            )

    # --- JS/TS ---
    js_exts = (".js", ".ts", ".jsx", ".tsx")
    for abs_path, rel_path in _walk_source_files(project_root, js_exts):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        # Check if this file defines timeout wrapper functions
        has_timeout_wrapper = bool(_JS_TIMEOUT_FN_NAME.search(file_text))

        for idx, line in enumerate(lines):
            is_fetch = bool(_JS_FETCH.search(line))
            is_axios = bool(_JS_AXIOS.search(line))

            if not (is_fetch or is_axios):
                continue

            # Exclude if AbortSignal / timeout: / signal: on same line
            if _JS_ABORT_SIGNAL.search(line) or _JS_TIMEOUT_SIGNAL.search(line):
                continue

            # Exclude if file defines a timeout-aware wrapper
            if has_timeout_wrapper:
                continue

            # Check next 3 lines for signal/timeout option
            next_ctx = _surrounding_lines(lines, idx, 0, 3)
            if _JS_TIMEOUT_SIGNAL.search(next_ctx) or _JS_ABORT_SIGNAL.search(next_ctx):
                continue

            call_name = "fetch()" if is_fetch else "axios call"
            findings.append(
                {
                    "file": rel_path,
                    "line": idx + 1,
                    "message": f"JS/TS {call_name} without signal/timeout option",
                    "severity": "high",
                    "rule_id": "RES-001",
                }
            )

    return findings


# ---------------------------------------------------------------------------
# RES-002: Retry loops without exponential backoff
# ---------------------------------------------------------------------------


def find_retries_without_backoff(graph: dict, project_root: str) -> list[dict]:
    """
    Scan for retry loops that use fixed sleep instead of exponential backoff.
    Returns list of finding dicts.
    """
    findings: list[dict] = []

    # --- Go ---
    for abs_path, rel_path in _walk_source_files(project_root, (".go",)):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        if _GO_BACKOFF_IMPORT.search(file_text):
            continue

        for idx, line in enumerate(lines):
            if not _GO_RETRY_LOOP.search(line):
                continue

            # Look at next 25 lines for backoff patterns
            body = _surrounding_lines(lines, idx, 0, 25)
            if _GO_BACKOFF_EXPR.search(body):
                continue

            # Also check for time.Sleep with a fixed literal in body
            if not re.search(r"\btime\.Sleep\b", body):
                continue

            findings.append(
                {
                    "file": rel_path,
                    "line": idx + 1,
                    "message": "Go retry loop uses fixed sleep without exponential backoff",
                    "severity": "medium",
                    "rule_id": "RES-002",
                }
            )

    # --- Python ---
    for abs_path, rel_path in _walk_source_files(project_root, (".py",)):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        if _PY_BACKOFF_IMPORT.search(file_text, re.MULTILINE if False else 0):
            continue

        # Re-check with multiline flag
        if re.search(
            r"^\s*(import|from)\s+(tenacity|backoff|retry)\b",
            file_text,
            re.MULTILINE,
        ):
            continue

        for idx, line in enumerate(lines):
            if not _PY_RETRY_LOOP.match(line):
                continue

            # Look for retry variable usage + fixed sleep in body (next 20 lines)
            body = _surrounding_lines(lines, idx, 0, 20)

            if not _PY_RETRY_VAR_IN_LOOP.search(body):
                continue

            fixed_sleep = _PY_FIXED_SLEEP.search(body)
            exp_sleep = _PY_EXP_SLEEP.search(body)

            if fixed_sleep and not exp_sleep:
                findings.append(
                    {
                        "file": rel_path,
                        "line": idx + 1,
                        "message": "Python retry loop uses fixed time.sleep() without exponential backoff",
                        "severity": "medium",
                        "rule_id": "RES-002",
                    }
                )

    # --- JS/TS ---
    js_exts = (".js", ".ts", ".jsx", ".tsx")
    for abs_path, rel_path in _walk_source_files(project_root, js_exts):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        if _JS_BACKOFF_IMPORT.search(file_text):
            continue

        for idx, line in enumerate(lines):
            if not _JS_RETRY_LOOP.search(line):
                continue

            body = _surrounding_lines(lines, idx, 0, 20)

            has_fixed_timeout = bool(_JS_FIXED_SETTIMEOUT.search(body))
            has_exp_timeout = bool(_JS_EXP_SETTIMEOUT.search(body))

            if has_fixed_timeout and not has_exp_timeout:
                findings.append(
                    {
                        "file": rel_path,
                        "line": idx + 1,
                        "message": "JS/TS retry loop uses fixed setTimeout() without exponential backoff",
                        "severity": "medium",
                        "rule_id": "RES-002",
                    }
                )

    return findings


# ---------------------------------------------------------------------------
# RES-003: Feature flags without fallback
# ---------------------------------------------------------------------------


def find_feature_flags_without_fallback(graph: dict, project_root: str) -> list[dict]:
    """
    Scan for feature flag calls missing a required fallback/default argument.
    Returns list of finding dicts.
    """
    findings: list[dict] = []

    # --- Go ---
    for abs_path, rel_path in _walk_source_files(project_root, (".go",)):
        lines = _read_lines(abs_path)
        for idx, line in enumerate(lines):
            m = _LD_GO_VARIATION.search(line)
            if not m:
                continue
            args_str = m.group(1)
            # BoolVariation needs 3 args: key, user, default
            if _count_args(args_str) < 3:
                findings.append(
                    {
                        "file": rel_path,
                        "line": idx + 1,
                        "message": "LaunchDarkly BoolVariation() called without fallback default (3rd arg required)",
                        "severity": "high",
                        "rule_id": "RES-003",
                    }
                )

    # --- Python ---
    for abs_path, rel_path in _walk_source_files(project_root, (".py",)):
        lines = _read_lines(abs_path)
        for idx, line in enumerate(lines):
            # LaunchDarkly
            m = _LD_PY_VARIATION.search(line)
            if m:
                args_str = m.group(2)
                if _count_args(args_str) < 3:
                    findings.append(
                        {
                            "file": rel_path,
                            "line": idx + 1,
                            "message": "LaunchDarkly variation() called without fallback default (3rd arg required)",
                            "severity": "high",
                            "rule_id": "RES-003",
                        }
                    )
                continue

            # Unleash
            m2 = _UNLEASH_PY_ENABLED.search(line)
            if m2:
                args_str = m2.group(1)
                arg_count = _count_args(args_str)
                # Only 1 arg and no fallback_function= keyword
                if arg_count < 2 and "fallback_function" not in args_str:
                    findings.append(
                        {
                            "file": rel_path,
                            "line": idx + 1,
                            "message": "Unleash is_enabled() called without fallback (2nd arg or fallback_function= required)",
                            "severity": "high",
                            "rule_id": "RES-003",
                        }
                    )
                continue

            # Statsig — must be wrapped in try/except
            if _STATSIG_CHECKGATE.search(line):
                # Look for try block in prev 5 lines
                prev_ctx = _surrounding_lines(lines, idx, 5, 0)
                if not re.search(r"\btry\s*:", prev_ctx):
                    findings.append(
                        {
                            "file": rel_path,
                            "line": idx + 1,
                            "message": "statsig.checkGate() not wrapped in try/except — no fallback on SDK failure",
                            "severity": "high",
                            "rule_id": "RES-003",
                        }
                    )

    # --- JS/TS ---
    js_exts = (".js", ".ts", ".jsx", ".tsx")
    for abs_path, rel_path in _walk_source_files(project_root, js_exts):
        lines = _read_lines(abs_path)
        for idx, line in enumerate(lines):
            # LaunchDarkly
            m = _LD_JS_VARIATION.search(line)
            if m:
                args_str = m.group(2)
                if _count_args(args_str) < 3:
                    findings.append(
                        {
                            "file": rel_path,
                            "line": idx + 1,
                            "message": "LaunchDarkly variation() called without fallback default (3rd arg required)",
                            "severity": "high",
                            "rule_id": "RES-003",
                        }
                    )
                continue

            # Unleash JS
            m2 = _UNLEASH_JS_ENABLED.search(line)
            if m2:
                args_str = m2.group(1)
                arg_count = _count_args(args_str)
                if arg_count < 2 and "fallback" not in args_str:
                    findings.append(
                        {
                            "file": rel_path,
                            "line": idx + 1,
                            "message": "Unleash isEnabled() called without second arg or fallback option",
                            "severity": "high",
                            "rule_id": "RES-003",
                        }
                    )
                continue

            # Statsig JS — no try/catch wrapper
            if _STATSIG_CHECKGATE.search(line):
                prev_ctx = _surrounding_lines(lines, idx, 5, 0)
                if not re.search(r"\btry\s*\{", prev_ctx):
                    findings.append(
                        {
                            "file": rel_path,
                            "line": idx + 1,
                            "message": "statsig.checkGate() not wrapped in try/catch — no fallback on SDK failure",
                            "severity": "high",
                            "rule_id": "RES-003",
                        }
                    )

    return findings


# ---------------------------------------------------------------------------
# RES-004: Missing circuit breakers
# ---------------------------------------------------------------------------


def find_missing_circuit_breakers(graph: dict, project_root: str) -> list[dict]:
    """
    Flag files with 3+ HTTP calls but no circuit-breaker import.
    Returns list of finding dicts (one per flagged file).
    """
    findings: list[dict] = []

    # --- Go ---
    for abs_path, rel_path in _walk_source_files(project_root, (".go",)):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        http_calls = len(_GO_HTTP_ANY.findall(file_text))
        if http_calls < 3:
            continue
        if _GO_CB_IMPORT.search(file_text):
            continue

        findings.append(
            {
                "file": rel_path,
                "line": 1,
                "message": f"Go file has {http_calls} HTTP calls but no circuit breaker (gobreaker/hystrix) imported",
                "severity": "medium",
                "rule_id": "RES-004",
            }
        )

    # --- Python ---
    for abs_path, rel_path in _walk_source_files(project_root, (".py",)):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        http_calls = len(_PY_REQUESTS_CALL.findall(file_text))
        if http_calls < 3:
            continue
        if re.search(
            r"^\s*(import|from)\s+(pybreaker|circuitbreaker|aiobreaker)\b",
            file_text,
            re.MULTILINE,
        ):
            continue

        findings.append(
            {
                "file": rel_path,
                "line": 1,
                "message": f"Python file has {http_calls} requests calls but no circuit breaker (pybreaker/circuitbreaker/aiobreaker) imported",
                "severity": "medium",
                "rule_id": "RES-004",
            }
        )

    # --- JS/TS ---
    js_exts = (".js", ".ts", ".jsx", ".tsx")
    for abs_path, rel_path in _walk_source_files(project_root, js_exts):
        lines = _read_lines(abs_path)
        file_text = "".join(lines)

        http_calls = len(_JS_HTTP_ANY.findall(file_text))
        if http_calls < 3:
            continue
        if _JS_CB_IMPORT.search(file_text):
            continue

        findings.append(
            {
                "file": rel_path,
                "line": 1,
                "message": f"JS/TS file has {http_calls} HTTP calls but no circuit breaker (opossum/cockatiel) imported",
                "severity": "medium",
                "rule_id": "RES-004",
            }
        )

    return findings


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def get_resilience_summary(project_root: str) -> dict:
    """
    Run all four checks and return an aggregated summary with resilience score.

    Penalties:
      -3 per HTTP-without-timeout finding
      -2 per retry-without-backoff finding
      -4 per feature-flag-without-fallback finding
      -5 per missing-circuit-breaker finding
    """
    graph: dict[str, Any] = {}  # placeholder; checks don't use graph yet

    timeout_findings = find_http_calls_without_timeout(graph, project_root)
    backoff_findings = find_retries_without_backoff(graph, project_root)
    flag_findings = find_feature_flags_without_fallback(graph, project_root)
    cb_findings = find_missing_circuit_breakers(graph, project_root)

    all_findings = timeout_findings + backoff_findings + flag_findings + cb_findings

    penalties = (
        len(timeout_findings) * 3
        + len(backoff_findings) * 2
        + len(flag_findings) * 4
        + len(cb_findings) * 5
    )
    score = max(0, 100 - penalties)

    return {
        "ok": len(all_findings) == 0,
        "http_without_timeout": len(timeout_findings),
        "retries_without_backoff": len(backoff_findings),
        "feature_flags_without_fallback": len(flag_findings),
        "missing_circuit_breakers": len(cb_findings),
        "total_findings": len(all_findings),
        "resilience_score": score,
        "findings": all_findings,
    }


# ---------------------------------------------------------------------------
# MCP tool exports (called by mcp_tools_integration.py)
# ---------------------------------------------------------------------------


def graph_http_timeout_tool(project_root: str) -> dict:
    """MCP tool: find HTTP calls without timeout in project_root."""
    findings = find_http_calls_without_timeout({}, project_root)
    return {
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


def graph_retry_backoff_tool(project_root: str) -> dict:
    """MCP tool: find retry loops without exponential backoff in project_root."""
    findings = find_retries_without_backoff({}, project_root)
    return {
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


def graph_feature_flag_tool(project_root: str) -> dict:
    """MCP tool: find feature flag calls without fallback in project_root."""
    findings = find_feature_flags_without_fallback({}, project_root)
    return {
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


def graph_circuit_breaker_tool(project_root: str) -> dict:
    """MCP tool: find HTTP-heavy files missing circuit breakers in project_root."""
    findings = find_missing_circuit_breakers({}, project_root)
    return {
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def _test_resilience() -> None:
    """
    Run smoke tests for each check using synthetic code snippets written to a
    temporary directory. Asserts >= 8 conditions total.
    """
    import tempfile
    import shutil

    tmpdir = tempfile.mkdtemp(prefix="resilience_test_")
    passed = 0
    failed = 0

    def ok(condition: bool, label: str) -> None:
        nonlocal passed, failed
        if condition:
            print(f"  PASS  {label}")
            passed += 1
        else:
            print(f"  FAIL  {label}")
            failed += 1

    def write(rel: str, content: str) -> str:
        path = os.path.join(tmpdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        return path

    # ------------------------------------------------------------------
    # Test 1 — HTTP timeout: Go without timeout → flagged
    # ------------------------------------------------------------------
    write(
        "src/client.go",
        'package main\nimport "net/http"\nfunc doReq() { http.Get("http://example.com") }\n',
    )
    r = find_http_calls_without_timeout({}, tmpdir)
    go_hits = [f for f in r if f["file"].endswith("client.go")]
    ok(len(go_hits) >= 1, "RES-001 Go http.Get without timeout → flagged")

    # ------------------------------------------------------------------
    # Test 2 — HTTP timeout: Go with context.WithTimeout → excluded
    # ------------------------------------------------------------------
    write(
        "src/client_ctx.go",
        'package main\nimport ("net/http"; "context")\n'
        'func doReqCtx() {\n'
        '  ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)\n'
        '  defer cancel()\n'
        '  http.Get("http://example.com")\n'
        '}\n',
    )
    r2 = find_http_calls_without_timeout({}, tmpdir)
    ctx_hits = [f for f in r2 if f["file"].endswith("client_ctx.go")]
    ok(len(ctx_hits) == 0, "RES-001 Go http.Get with context.WithTimeout → excluded")

    # ------------------------------------------------------------------
    # Test 3 — HTTP timeout: Python without timeout= → flagged
    # ------------------------------------------------------------------
    write(
        "src/api.py",
        "import requests\n\ndef call():\n    resp = requests.get('http://example.com')\n    return resp\n",
    )
    r3 = find_http_calls_without_timeout({}, tmpdir)
    py_hits = [f for f in r3 if f["file"].endswith("api.py")]
    ok(len(py_hits) >= 1, "RES-001 Python requests.get without timeout → flagged")

    # ------------------------------------------------------------------
    # Test 4 — HTTP timeout: Python with timeout= → excluded
    # ------------------------------------------------------------------
    write(
        "src/api_ok.py",
        "import requests\n\ndef call():\n    resp = requests.get('http://example.com', timeout=5)\n    return resp\n",
    )
    r4 = find_http_calls_without_timeout({}, tmpdir)
    py_ok_hits = [f for f in r4 if f["file"].endswith("api_ok.py")]
    ok(len(py_ok_hits) == 0, "RES-001 Python requests.get with timeout= → excluded")

    # ------------------------------------------------------------------
    # Test 5 — Retry backoff: Python fixed sleep → flagged
    # ------------------------------------------------------------------
    write(
        "src/retry_bad.py",
        "import time\n\ndef fetch_with_retry():\n"
        "    for attempt in range(5):\n"
        "        try:\n"
        "            return do_request()\n"
        "        except Exception:\n"
        "            time.sleep(1)\n",
    )
    r5 = find_retries_without_backoff({}, tmpdir)
    rb_hits = [f for f in r5 if f["file"].endswith("retry_bad.py")]
    ok(len(rb_hits) >= 1, "RES-002 Python retry with fixed sleep → flagged")

    # ------------------------------------------------------------------
    # Test 6 — Retry backoff: Python exponential sleep → excluded
    # ------------------------------------------------------------------
    write(
        "src/retry_good.py",
        "import time\n\ndef fetch_with_retry():\n"
        "    for attempt in range(5):\n"
        "        try:\n"
        "            return do_request()\n"
        "        except Exception:\n"
        "            time.sleep(2 ** attempt)\n",
    )
    r6 = find_retries_without_backoff({}, tmpdir)
    rb_ok_hits = [f for f in r6 if f["file"].endswith("retry_good.py")]
    ok(len(rb_ok_hits) == 0, "RES-002 Python retry with exponential sleep → excluded")

    # ------------------------------------------------------------------
    # Test 7 — Feature flags: LaunchDarkly without fallback → flagged
    # ------------------------------------------------------------------
    write(
        "src/flags.py",
        "import ldclient\n\ndef check():\n"
        "    result = ldclient.get().variation('my-flag', user)\n"
        "    return result\n",
    )
    r7 = find_feature_flags_without_fallback({}, tmpdir)
    ff_hits = [f for f in r7 if f["file"].endswith("flags.py")]
    ok(len(ff_hits) >= 1, "RES-003 LaunchDarkly variation() without 3rd arg → flagged")

    # ------------------------------------------------------------------
    # Test 8 — Feature flags: LaunchDarkly with fallback → excluded
    # ------------------------------------------------------------------
    write(
        "src/flags_ok.py",
        "import ldclient\n\ndef check():\n"
        "    result = ldclient.get().variation('my-flag', user, False)\n"
        "    return result\n",
    )
    r8 = find_feature_flags_without_fallback({}, tmpdir)
    ff_ok_hits = [f for f in r8 if f["file"].endswith("flags_ok.py")]
    ok(len(ff_ok_hits) == 0, "RES-003 LaunchDarkly variation() with fallback → excluded")

    # ------------------------------------------------------------------
    # Test 9 — Circuit breaker: Python 3+ requests.get, no CB import → flagged
    # ------------------------------------------------------------------
    write(
        "src/multi_http.py",
        "import requests\n\n"
        "def a(): return requests.get('/a')\n"
        "def b(): return requests.post('/b')\n"
        "def c(): return requests.get('/c')\n",
    )
    r9 = find_missing_circuit_breakers({}, tmpdir)
    cb_hits = [f for f in r9 if f["file"].endswith("multi_http.py")]
    ok(len(cb_hits) >= 1, "RES-004 Python 3 HTTP calls without CB → flagged")

    # ------------------------------------------------------------------
    # Test 10 — Circuit breaker: Python with pybreaker import → excluded
    # ------------------------------------------------------------------
    write(
        "src/multi_http_cb.py",
        "import requests\nimport pybreaker\n\n"
        "breaker = pybreaker.CircuitBreaker()\n"
        "def a(): return requests.get('/a')\n"
        "def b(): return requests.post('/b')\n"
        "def c(): return requests.get('/c')\n",
    )
    r10 = find_missing_circuit_breakers({}, tmpdir)
    cb_ok_hits = [f for f in r10 if f["file"].endswith("multi_http_cb.py")]
    ok(len(cb_ok_hits) == 0, "RES-004 Python 3 HTTP calls with pybreaker import → excluded")

    # Cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\nResults: {passed} passed, {failed} failed out of {passed + failed} assertions")
    if failed:
        raise AssertionError(f"{failed} smoke test(s) failed")
    print("All resilience smoke tests passed.")


if __name__ == "__main__":
    _test_resilience()
