# Session Context

## Current Task
License server fully operational: KV sync, dedup, Razorpay removed, Railway on GitHub auto-deploy.

## Key Decisions
- **Railway deploys via GitHub** (`kunal12203/graperoot-license-server`) — `git push` = auto-deploy. Never use `railway up` (gitignore breaks uploads).
- **KV sync fixed**: Cloudflare blocked bare `urllib.request` (no User-Agent) → added `GrapeRoot-License-Server/1.0` header.
- **Duplicate key/email fix**: `INSERT OR IGNORE` + `welcome_email_sent` flag + unique index on `lemonsqueezy_subscription_id`. Razorpay code fully removed.

## Next Steps
- Fix 4 remaining bugs from adversarial testing: comment-@auth false positive, @author false positive, empty string in graph_find_missing, unicode identifiers
- Implement Phase 5: control flow annotations (empty catch, await-in-loop, N+1)
- Test on coir repo (https://github.com/coir-team/coir)
