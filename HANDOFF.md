# GrapeRoot Review — Project Handoff

**Repo:** github.com/kunal12203/graperoot-review  
**Live URL:** https://review.graperoot.dev  
**Backend:** graperoot-review-production.up.railway.app (Railway, Flask)  
**Date:** 2026-05-18

---

## What this repo is

Standalone Next.js frontend for the GrapeRoot Review product — graph-proven AI code review
delivered as a GitHub App. Fully separate from the main graperoot.dev repo.

---

## Pages

| Route | File | Description |
|-------|------|-------------|
| `/` | `src/app/page.tsx` | Hero landing page |
| `/how` | `src/app/how/page.tsx` | How it works + About (3 cards + findings strip) |
| `/compare` | `src/app/compare/page.tsx` | Comparison table vs CodeRabbit / CodeAnt / Greptile |
| `/pricing` | `src/app/pricing/page.tsx` | Pricing plans + Enterprise card + FAQ |
| `/login` | Railway server | GitHub OAuth (proxied by Railway) |
| `/dashboard` | Railway server | Review analytics (proxied by Railway) |

---

## Components

| File | Purpose |
|------|---------|
| `src/components/Navbar.tsx` | Fixed nav: About, Compare, Pricing, Dashboard + Login CTA |
| `src/components/Footer.tsx` | 3-column footer: Brand, Product links, Get started |
| `src/components/SiteBackground.tsx` | Fixed hex-grid background layer (z-0) |
| `src/components/HexGridBG.tsx` | Interactive SVG hex grid with cursor heatmap |

---

## Tech stack

- **Framework:** Next.js 14, App Router, TypeScript
- **Styling:** Tailwind CSS + custom grape color scale + globals.css animations
- **Icons:** lucide-react
- **Fonts:** Mulish (sans), JetBrains Mono (mono)
- **Background:** Custom SVG hex grid with mouse-tracking (HexGridBG.tsx)
- **Deployment:** Vercel → review.graperoot.dev

---

## Backend (Railway Flask server)

| URL | Purpose |
|-----|---------|
| `graperoot-review-production.up.railway.app/webhook` | GitHub App webhook handler |
| `graperoot-review-production.up.railway.app/login` | GitHub OAuth start |
| `graperoot-review-production.up.railway.app/auth/callback` | OAuth callback |
| `graperoot-review-production.up.railway.app/dashboard` | Analytics dashboard |
| `graperoot-review-production.up.railway.app/api/reviews` | JSON review history |

### Railway env vars
| Variable | Value |
|----------|-------|
| `GITHUB_APP_ID` | `3745471` |
| `GITHUB_PRIVATE_KEY` | RSA key from `graperoot-review.2026-05-17.private-key.pem` |
| `GITHUB_WEBHOOK_SECRET` | `000002eb30f9b1a0fe6e6f7b20c29d1a4ad9f397de1321f4a81a7446a43f5198` |
| `GITHUB_OAUTH_CLIENT_ID` | `Iv23li0cLujS1WvpPdvf` |
| `GITHUB_OAUTH_CLIENT_SECRET` | `3274c9abb9cd437d0e622e4644bc2d45a8a229e1` |
| `ANTHROPIC_API_KEY` | In `~/Downloads/src/graproot/.env` |
| `DATABASE_URL` | NeonDB — see REVIEW_HANDOFF.md in main repo |
| `SESSION_SECRET` | `73c1a93166dfec9bd3f648767ba6df17ac2ed...` |

---

## GitHub App

- **App name:** GrapeRoot Review
- **App ID:** `3745471`
- **Install URL:** https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876
- **Webhook URL:** `https://graperoot-review-production.up.railway.app/webhook`
- **OAuth Callback:** `https://graperoot-review-production.up.railway.app/auth/callback`

---

## DNS (Cloudflare)

| Record | Type | Value |
|--------|------|-------|
| `review` | A | `76.76.21.21` (Vercel) |

---

## Vercel deployment

1. Import this repo into Vercel
2. Framework: Next.js (auto-detected)
3. Add domain: `review.graperoot.dev`
4. Remove `review.graperoot.dev` from the old GrapeRoot Vercel project

---

## Pricing

| Tier | Price | Reviews |
|------|-------|---------|
| Free | $0 | 5/mo (public repos) |
| Pro | $15/user/mo | Unlimited (private + public) |
| Enterprise | Custom | Self-hosted, BYOK, air-gapped |

API cost: ~$0.05/review (Claude Sonnet 4.6, diff-only mode)

---

## What's next

- [ ] Set OAuth callback URL on GitHub App settings (needed for login to work)
- [ ] Install App on a test repo, open a PR to validate end-to-end
- [ ] Add Stripe billing for Pro tier
- [ ] Add `graperoot.yml` config file support (custom rules per repo)
- [ ] Publish accuracy benchmark vs CodeRabbit
- [ ] ProductHunt launch
