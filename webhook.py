#!/usr/bin/env python3
"""GrapeRoot Review — GitHub App webhook server.

Receives pull_request events from GitHub and runs review.py automatically.

Environment variables:
    GITHUB_WEBHOOK_SECRET   — from GitHub App settings (for signature verification)
    GITHUB_APP_ID           — numeric App ID from GitHub App settings
    GITHUB_PRIVATE_KEY      — RSA private key PEM (newlines as \\n or real newlines)
    ANTHROPIC_API_KEY       — Claude API key
    GITHUB_TOKEN            — fallback PAT (used if App credentials not set)
    PORT                    — HTTP port (default 8080)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from threading import Thread

from flask import Flask, jsonify, request, abort

try:
    import jwt  # PyJWT
    _HAS_JWT = True
except ImportError:
    _HAS_JWT = False

# ── Config ─────────────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
APP_ID         = os.environ.get("GITHUB_APP_ID", "")
PRIVATE_KEY    = os.environ.get("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
FALLBACK_TOKEN = os.environ.get("GITHUB_TOKEN", "")

app = Flask(__name__)


# ── GitHub App auth ────────────────────────────────────────────────────────────

def _installation_token(installation_id: int) -> str:
    """Generate a short-lived GitHub installation token via JWT."""
    if not (APP_ID and PRIVATE_KEY and _HAS_JWT):
        return FALLBACK_TOKEN

    import urllib.request

    now   = int(time.time())
    token = jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": APP_ID},
        PRIVATE_KEY,
        algorithm="RS256",
    )
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        data=b"{}",
        method="POST",
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent":           "graperoot-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["token"]


# ── Signature verification ─────────────────────────────────────────────────────

def _verify_sig(payload: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True  # dev mode — no secret configured
    mac = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={mac}", sig_header or "")


# ── Review runner ──────────────────────────────────────────────────────────────

def _run_review(owner: str, repo: str, pr_num: int, github_token: str) -> None:
    pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_num}"
    print(f"[review] starting {owner}/{repo}#{pr_num}", flush=True)
    env = {
        **os.environ,
        "GITHUB_TOKEN":    github_token,
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
    }
    result = subprocess.run(
        [sys.executable, "review.py", pr_url],
        env=env,
        timeout=300,
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"[review] {owner}/{repo}#{pr_num} exited {result.returncode}", flush=True)
    else:
        print(f"[review] {owner}/{repo}#{pr_num} done", flush=True)


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_data()

    if not _verify_sig(payload, request.headers.get("X-Hub-Signature-256", "")):
        abort(401)

    event = request.headers.get("X-GitHub-Event", "")

    # Only handle pull_request events
    if event != "pull_request":
        return jsonify({"ok": True, "skipped": event})

    data   = request.get_json(force=True) or {}
    action = data.get("action", "")

    # Only trigger on new PRs or new commits pushed to a PR
    if action not in ("opened", "synchronize", "reopened"):
        return jsonify({"ok": True, "skipped": action})

    pr           = data["pull_request"]
    owner        = data["repository"]["owner"]["login"]
    repo         = data["repository"]["name"]
    pr_num       = pr["number"]
    installation_id = data.get("installation", {}).get("id", 0)

    # Get a token scoped to this installation
    try:
        github_token = _installation_token(installation_id) if installation_id else FALLBACK_TOKEN
    except Exception as e:
        print(f"[webhook] token fetch failed: {e} — falling back to GITHUB_TOKEN", flush=True)
        github_token = FALLBACK_TOKEN

    if not github_token:
        print("[webhook] no GitHub token available — skipping review", flush=True)
        return jsonify({"ok": False, "error": "no_token"}), 500

    # Fire-and-forget: ack GitHub in <3s, run review in background
    Thread(
        target=_run_review,
        args=(owner, repo, pr_num, github_token),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "queued": f"{owner}/{repo}#{pr_num}"})


# ── Health check ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "app_mode":  bool(APP_ID and PRIVATE_KEY and _HAS_JWT),
        "jwt_lib":   _HAS_JWT,
    })


@app.route("/")
def index():
    return jsonify({
        "service": "GrapeRoot Review",
        "docs":    "https://review.graperoot.dev",
        "health":  "/health",
        "webhook": "/webhook",
    })


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"GrapeRoot webhook server on :{port}")
    print(f"  App mode: {'yes' if APP_ID else 'no (PAT fallback)'}")
    print(f"  JWT lib:  {'yes' if _HAS_JWT else 'no — pip install PyJWT'}")
    app.run(host="0.0.0.0", port=port)
