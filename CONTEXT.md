# GrapeRoot Review — Session Context

> Drop this into a new Claude Code chat to resume with full context.

---

## What this project is

Standalone Next.js frontend for **GrapeRoot Review** — a GitHub App that posts
graph-proven AI code review comments on every PR. Every finding cites the real
AST import chain, not an LLM guess.

- **Frontend (this repo):** Next.js 14, Tailwind CSS, App Router → `review.graperoot.dev`
- **Backend:** Flask on Railway → `graperoot-review-production.up.railway.app`
- **Main site:** `graperoot.dev` (separate repo: `kunal12203/GrapeRoot`) — links to this

---

## Current state

### Pages (all built, styled, deployed)
| Route | File | Status |
|-------|------|--------|
| `/` | `src/app/page.tsx` | Hero + CTA |
| `/how` | `src/app/how/page.tsx` | How it works + About (3 cards + Grafana findings strip) |
| `/compare` | `src/app/compare/page.tsx` | 9-row table vs CodeRabbit / CodeAnt / Greptile + callout + graph citation |
| `/pricing` | `src/app/pricing/page.tsx` | Free + Pro plans + Enterprise card + FAQ accordion |
| `/login` | Railway (Flask) | GitHub OAuth → `/dashboard` |
| `/dashboard` | Railway (Flask) | Review analytics |

### Components
| File | What it does |
|------|-------------|
| `src/components/Navbar.tsx` | Fixed nav: About → `/how`, Compare, Pricing, Dashboard + GitHub + Login CTAs |
| `src/components/Footer.tsx` | 3-col: brand + product links + get started |
| `src/components/SiteBackground.tsx` | Fixed hex-grid background (z-0) |
| `src/components/HexGridBG.tsx` | Interactive SVG hex grid with cursor heatmap |

### Design system
- **Colors:** Custom `grape` scale (50–950) in `tailwind.config.ts`
- **Fonts:** Mulish (`font-sans`) + JetBrains Mono (`font-mono`)
- **Background:** Animated hex grid from `SiteBackground` (in layout) — never add `bg-[#09090b]` to `<main>` or it hides the grid
- **Animations:** `animate-blob`, `animate-fade-in-up-delay-1/2/3`, `animate-flow-border` — all in `globals.css`

---

## Key decisions made

1. **Separated from main GrapeRoot repo** — was living at `graperoot.dev/review` via middleware. Now its own repo + Vercel project.
2. **No middleware** — this IS the root domain, routes work as normal Next.js pages.
3. **Login button** → `https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876`
4. **Navbar links are relative** (`/how`, `/compare`, `/pricing`) — not absolute like before.
5. **Free tier = 5 reviews/month**, **Pro = $15/mo unlimited**.
6. **No emojis anywhere** — was requested explicitly.

---

## What still needs doing

- [ ] **Vercel deployment** — import this repo, add domain `review.graperoot.dev`, remove domain from old GrapeRoot project
- [ ] **Set OAuth callback URL** on GitHub App: `https://graperoot-review-production.up.railway.app/auth/callback`
- [ ] **Test login flow end-to-end** (install App → open PR → review posted → login → dashboard)
- [ ] **Stripe billing** for Pro tier
- [ ] `graperoot.yml` custom rules support
- [ ] ProductHunt launch

---

## GitHub App

- **Install URL:** `https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876`
- **App ID:** `3745471`
- **Webhook:** `https://graperoot-review-production.up.railway.app/webhook`
- **OAuth callback:** `https://graperoot-review-production.up.railway.app/auth/callback` ← needs to be set in GitHub App settings

---

## Backend (Railway Flask)

Repo: `kunal12203/graperoot-review` (same repo, `webhook.py` + `review.py`)

Key env vars on Railway:
```
GITHUB_APP_ID=3745471
GITHUB_PRIVATE_KEY=<RSA key from graperoot-review.2026-05-17.private-key.pem>
GITHUB_WEBHOOK_SECRET=000002eb30f9b1a0fe6e6f7b20c29d1a4ad9f397de1321f4a81a7446a43f5198
GITHUB_OAUTH_CLIENT_ID=Iv23li0cLujS1WvpPdvf
ANTHROPIC_API_KEY=<in ~/Downloads/src/graproot/.env>
DATABASE_URL=postgresql://neondb_owner:npg_tzuhBA63sibL@ep-aged-water-aq6zdu38-pooler.c-8.us-east-1.aws.neon.tech/neondb?sslmode=require
```

---

## Deployment (remaining steps)

```bash
# 1. Vercel — import kunal12203/graperoot-review
# 2. Add domain: review.graperoot.dev
# 3. Remove review.graperoot.dev from old GrapeRoot Vercel project
# 4. Set OAuth callback in GitHub App settings (link above)
```

---

## Repo links

- **This repo:** `github.com/kunal12203/graperoot-review`
- **Main site:** `github.com/kunal12203/GrapeRoot`
- **Live (once deployed):** `https://review.graperoot.dev`
