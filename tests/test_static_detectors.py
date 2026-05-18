"""
Snapshot test suite for GrapeRoot static detectors.

These tests are pinned to known PRs with expected findings.
CI fails if the output deviates — that's the point.

Run: pytest tests/test_static_detectors.py -v
"""
import json, os, re, sys, urllib.request, base64
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from backend.review import (
    detect_n_plus_one,
    detect_falsy_traps,
    detect_default_value_changes,
    detect_docstring_issues,
    detect_orphaned_methods,
    detect_rust_index_panics,
    detect_rust_unwrap_panics,
    parse_diff,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")

def fetch_diff(owner: str, repo: str, pr_num: int) -> str:
    if not GH_TOKEN:
        pytest.skip("GITHUB_TOKEN not set")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.diff",
                 "User-Agent": "graperoot-test/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")

def fetch_file(owner, repo, path, ref) -> str:
    if not GH_TOKEN:
        pytest.skip("GITHUB_TOKEN not set")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                 "User-Agent": "graperoot-test/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return base64.b64decode(d["content"].replace("\n","")).decode("utf-8", errors="replace")
    except Exception:
        return ""

def titles(findings): return [f["title"] for f in findings]
def severities(findings): return [f["severity"] for f in findings]
def checks(findings): return [f.get("check","") for f in findings]

# ── Test 1: N+1 detector on kobotoolbox/kpi#7063 ─────────────────────────────

@pytest.mark.parametrize("expected_n_plus_one_count", [4])
def test_kpi_n_plus_one(expected_n_plus_one_count):
    """kpi#7063 at commit b8b0760 has 4+ N+1 patterns. Static detector must find them."""
    diff = fetch_diff("kobotoolbox", "kpi", 7063)
    findings = detect_n_plus_one(diff)
    assert len(findings) >= expected_n_plus_one_count, (
        f"Expected >= {expected_n_plus_one_count} N+1 findings, got {len(findings)}: {titles(findings)}"
    )
    assert all(f["severity"] == "HIGH" for f in findings), "All N+1 findings must be HIGH"
    assert all("[AST-HEURISTIC: n-plus-one]" in f["title"] for f in findings), \
        "All N+1 findings must have [AST-HEURISTIC: n-plus-one] tag"
    # Must include the submission_uuid loop
    assert any("submission_uuid" in f["title"] or "submission_uuid" in f["comment"]
               for f in findings), "Must catch the submission_uuid N+1 loop"


def test_kpi_falsy_trap():
    """Synthetic diff with 'page or queryset' falsy trap — the exact pattern from kpi#7063."""
    # The kpi PR was patched after Greptile's review. Use synthetic diff to pin the pattern.
    synthetic = """\
diff --git a/kobo/apps/subsequences/views.py b/kobo/apps/subsequences/views.py
--- a/views.py
+++ b/views.py
@@ -1,5 +1,8 @@
+    def list(self, request, *args, **kwargs):
+        queryset = self.get_queryset()
+        page = self.paginate_queryset(queryset)
+        serializer = self.get_serializer(page or queryset, many=True)
+        return Response(serializer.data)
"""
    findings = detect_falsy_traps(synthetic)
    assert len(findings) >= 1, f"Expected >= 1 falsy trap, got {len(findings)}"
    assert any("page" in f["title"].lower() or "queryset" in f["title"].lower()
               for f in findings), "Must catch the 'page or queryset' pattern"
    assert all("[AST-HEURISTIC: falsy-trap]" in f["title"] for f in findings), \
        "All falsy-trap findings must be tagged [AST-HEURISTIC: falsy-trap]"

# ── Test 2: Default-change detector on flask sansio PR ────────────────────────

def test_flask_default_change():
    """Synthetic file context with accessed=False→True. Default-change detector must find it."""
    file_context = (
        "### src/flask/sessions.py  [BASE — before this PR]\n"
        "```\n"
        "class SessionMixin:\n"
        "    accessed = False\n"
        "    modified = False\n"
        "    new = False\n"
        "```\n\n"
        "### src/flask/sansio/sessions.py  [HEAD — after this PR]\n"
        "```\n"
        "class SessionMixin:\n"
        "    accessed = True\n"
        "    modified = True\n"
        "    new = False\n"
        "```"
    )
    findings = detect_default_value_changes("", file_context)
    assert len(findings) >= 1, f"Expected >= 1 default change, got 0"
    assert any("accessed" in f["title"] for f in findings), \
        "Must catch the 'accessed' default change"
    assert all("[AST-HEURISTIC: default-change]" in f["title"] for f in findings), \
        "Must be tagged [AST-HEURISTIC: default-change]"

# ── Test 3: Docstring detector on flask sansio PR ─────────────────────────────

def test_flask_docstring_issue():
    """flask#1 has 'Uses``app.config' (missing space). Docstring detector must find it."""
    diff = fetch_diff("kunal12203", "flask", 1)
    _, hunks = parse_diff(diff)
    diff_text = "\n\n".join(f"### {p}\n{h}" for p, h in list(hunks.items())[:10])
    findings = detect_docstring_issues(diff_text)
    assert len(findings) >= 1, f"Expected >= 1 docstring issue, got 0"
    assert all("[AST-HEURISTIC: docstring]" in f["title"] for f in findings), \
        "Must be tagged [AST-HEURISTIC: docstring]"

# ── Test 4: Rust bounds detector on inline code ────────────────────────────────

def test_rust_bounds_synthetic():
    """Synthetic diff with direct Rust indexing without bounds check."""
    diff = """\
diff --git a/src/lib.rs b/src/lib.rs
index 000..111 100644
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1,5 +1,8 @@
+pub fn process(items: &[u8], idx: usize) -> u8 {
+    let value = items[idx];
+    value
+}
"""
    findings = detect_rust_index_panics(diff)
    assert len(findings) >= 1, "Must catch direct Rust indexing without bounds check"
    assert findings[0]["severity"] == "HIGH"
    assert "[AST-HEURISTIC: rust-bounds]" in findings[0]["title"]


def test_rust_bounds_skips_guarded():
    """Indexing with a preceding bounds check must NOT be flagged."""
    diff = """\
diff --git a/src/lib.rs b/src/lib.rs
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1,5 +1,8 @@
+pub fn process(items: &[u8], idx: usize) -> u8 {
+    if idx >= items.len() { return 0; }
+    let value = items[idx];
+    value
+}
"""
    findings = detect_rust_index_panics(diff)
    assert len(findings) == 0, f"Guarded indexing must not be flagged, got: {titles(findings)}"


def test_rust_bounds_skips_markdown():
    """Indexing syntax in markdown code blocks (.md files) must not be flagged."""
    diff = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
+## Example
+```rust
+let x = vec[idx as usize];
+```
"""
    findings = detect_rust_index_panics(diff)
    assert len(findings) == 0, f"Markdown code blocks must not be flagged, got: {titles(findings)}"

# ── Test 5: Tagging consistency ────────────────────────────────────────────────

def test_all_static_findings_have_correct_tag():
    """Every static detector must prefix its title with [AST-HEURISTIC: <name>]."""
    synthetic_diff = """\
diff --git a/views.py b/views.py
--- a/views.py
+++ b/views.py
@@ -1,5 +1,10 @@
+def list(self, request):
+    for uuid in uuids:
+        Model.objects.filter(uuid=uuid).exists()
+    page = self.paginate_queryset(queryset)
+    data = page or queryset
+    \"\"\"The name. Uses``app.config[\"X\"]\"\"\"
"""
    all_findings = (
        detect_n_plus_one(synthetic_diff) +
        detect_falsy_traps(synthetic_diff) +
        detect_docstring_issues(synthetic_diff)
    )
    for f in all_findings:
        assert f["title"].startswith("[AST-HEURISTIC:") or f["title"].startswith("[AST-FACT:"), \
            f"Static finding missing correct tag: {f['title']}"
        assert "[LLM-HEURISTIC:" not in f["title"], \
            f"Static finding wrongly tagged LLM-HEURISTIC: {f['title']}"
