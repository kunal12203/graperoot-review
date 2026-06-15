# GrapeRoot Pro — Frontend Dashboard Guide

Complete reference for building the Pro dashboard: how savings are calculated,
every API endpoint, every field, and which metrics map to which UI elements.

---

## How savings work (the mental model)

When Claude Code runs **without** GrapeRoot Pro, it reads files and runs greps with
native tools (Bash, Read). Every character it reads costs tokens.

GrapeRoot Pro intercepts those reads and does three things:

1. **TAR (Token Avoidance Ratio)** — graph_read returns only the relevant symbol/section
   of a file instead of the full file. Avoided = full_file_chars − returned_chars.

2. **Cross-turn pointer** — if Claude already read a file earlier in the session,
   GrapeRoot returns a 1-line pointer ("already read in turn 3") instead of re-sending
   the whole file into context again.

3. **Shadow savings** — background measurement of what vanilla Claude would have spent
   on grep + file reads if GrapeRoot weren't there. This is never sent to Claude;
   it's purely a measurement of what was replaced.

All three are summed into `tokens_avoided`. The dashboard shows the breakdown.

---

## Savings calculation (all server-side)

### Per-session formula (in `_calc_cost` in webhook.py)

```
# Actual cost — benefits from GrapeRoot + prompt cache
total_cost = (
  input_tokens        × inp_price  +
  cache_write_tokens  × cw_price   +
  cache_read_tokens   × cr_price   +
  output_tokens       × out_price
) / 1_000_000

# Naive cost — what it would have cost WITHOUT GrapeRoot
# (tokens_avoided = TAR + cross-turn + shadow, all at full input price)
naive_cost = total_cost
           + tokens_avoided             × inp_price / 1_000_000
           + tokens_avoided_compounding × cr_price  / 1_000_000

savings_pct  = (naive_cost − total_cost) / naive_cost   # 0.0–1.0
saved_usd    = naive_cost − total_cost
```

### Token savings breakdown (4 components)

| Field stored | What it measures |
|---|---|
| `tokens_avoided_tar` | Chars skipped by symbol-level excerpting (TAR) |
| `tokens_avoided_cross_turn` | Re-read avoidance (same file, same session) |
| `shadow_tokens_avoided` | Greps + full file reads replaced by graph lookups |
| `graperoot_overhead_tokens` | GrapeRoot's own response overhead (small) |
| `tokens_avoided` | Total = tar + cross_turn + shadow |

### Model pricing (per million tokens, June 2026)

| Model | Input (`inp`) | Cache Write (`cw`) | Cache Read (`cr`) | Output (`out`) |
|---|---|---|---|---|
| Haiku 4.5 | $1.00 | $1.25 | $0.10 | $5.00 |
| Sonnet 4.x (default) | $3.00 | $3.75 | $0.30 | $15.00 |
| Fable 5 | $10.00 | $12.50 | $1.00 | $50.00 |
| Opus 4.8 / 4.7 | $5.00 | $6.25 | $0.50 | $25.00 |
| Opus 4 / 4.1 | $15.00 | $18.75 | $1.50 | $75.00 |

---

## API endpoints

Base URL: `https://graperoot-review-production.up.railway.app`

All endpoints take `license_key=GRP-XXXX-XXXX-XXXX` as a query param. No OAuth needed.

---

### GET /api/usage/stats

**Main dashboard data** — monthly summary, daily breakdown, recent sessions.

Query params:
- `license_key` (required)
- `month` — `YYYY-MM`, defaults to current UTC month

```json
{
  "license_key": "GRP-XXXX",
  "month": "2026-06",

  "summary": {
    "total_turns":               42,
    "total_input_tokens":        180000,
    "total_output_tokens":       820000,
    "total_cache_read_tokens":   9400000,
    "total_cache_write_tokens":  1200000,

    "total_cost_usd":            12.40,
    "total_naive_cost_usd":      27.80,
    "total_saved_usd":           15.40,
    "avg_savings_pct":           55.4,

    "total_tokens_served":       10000000,
    "total_tokens_avoided":      3200000,
    "tool_savings_pct":          24.2,

    "tokens_avoided_tar":        1200000,
    "tokens_avoided_cross_turn": 400000,
    "shadow_tokens_avoided":     1600000,
    "graperoot_overhead_tokens": 52000
  },

  "by_day": [
    {
      "date":                    "2026-06-15",
      "turns":                   8,
      "input_tokens":            34000,
      "cache_read_tokens":       1800000,
      "total_cost_usd":          2.31,
      "naive_cost_usd":          5.20,
      "savings_pct":             0.555,
      "tokens_served":           1900000,
      "tokens_avoided":          620000,
      "tokens_avoided_tar":      230000,
      "tokens_avoided_cross_turn": 80000,
      "shadow_tokens_avoided":   310000
    }
  ],

  "recent": [
    {
      "id":                       101,
      "session_id":               "uuid-...",
      "timestamp":                "2026-06-15T13:12:36Z",
      "model":                    "claude-sonnet-4-6",
      "input_tokens":             4200,
      "output_tokens":            19600,
      "cache_read_tokens":        220000,
      "cache_write_tokens":       28000,
      "total_cost_usd":           0.454,
      "naive_cost_usd":           0.841,
      "savings_pct":              0.459,
      "tokens_served":            29000,
      "tokens_avoided":           26700,
      "tokens_avoided_tar":       1600,
      "tokens_avoided_cross_turn": 0,
      "shadow_tokens_avoided":    25100,
      "graperoot_overhead_tokens": 1025,
      "tool_hits":                "{\"symbol_excerpt\": 1, \"native_full\": 1}",
      "task_type":                "prompt",
      "confidence":               "none",
      "project_hash":             "a08ac173d8b4ddbf"
    }
  ]
}
```

---

### GET /api/usage/savings-chart

**Savings trend chart** — daily tokens saved, for the sparkline / bar chart.

Query params:
- `license_key` (required)
- `days` — integer 1–90, defaults to 30

```json
{
  "license_key": "GRP-XXXX",
  "days": 30,
  "total_tokens_saved":   28400000,
  "total_requests":       42,
  "estimated_saved_usd":  426.00,
  "chart": [
    { "date": "2026-05-16", "tokens_saved": 620000, "requests_count": 1 },
    { "date": "2026-05-17", "tokens_saved": 0,      "requests_count": 0 },
    { "date": "2026-06-15", "tokens_saved": 840000, "requests_count": 3 }
  ]
}
```

Notes:
- `chart` only contains days with at least one session (no zero-fill). Fill gaps client-side.
- `tokens_saved` per day = `tokens_avoided` sum for that day (TAR + cross-turn + shadow).
- `estimated_saved_usd` uses the Opus rate ($15/M) as a conservative ceiling. Multiply
  `total_tokens_saved × user_model_price / 1_000_000` for a model-specific estimate.

---

### GET /api/usage/export

CSV download of all sessions for a month.

Query params: `license_key`, `month`

Response: `text/csv` with `Content-Disposition: attachment; filename=graperoot-usage-YYYY-MM.csv`

Columns: `timestamp, model, input_tokens, output_tokens, cache_read_tokens,
          cache_write_tokens, total_cost_usd, savings_pct, tokens_served,
          tokens_avoided, task_type, confidence, session_id`

---

## Dashboard UI — what to show

### Hero stats (top of page)

| Metric | Formula | Display |
|---|---|---|
| Money saved this month | `total_saved_usd` | **$15.40 saved** |
| Savings % | `avg_savings_pct` | **55% cheaper than Claude alone** |
| Sessions | `total_turns` | **42 sessions** |
| Actual spend | `total_cost_usd` | **$12.40 spent** |

### Savings breakdown (donut or stacked bar)

```
Total tokens avoided: tokens_avoided

  ├── TAR (smart excerpting)      tokens_avoided_tar         (e.g. 37%)
  ├── Cross-turn dedup            tokens_avoided_cross_turn  (e.g. 12%)
  └── Shadow (grep/read replaced) shadow_tokens_avoided      (e.g. 51%)
```

Label as:
- **Smart excerpting** — graph_read returned only the relevant symbol, not the full file
- **Re-read dedup** — same file referenced again; returned a pointer, not the full content
- **Search replacement** — grep + file read calls replaced by graph lookups

### Savings trend chart (line or bar, by day)

Use `/api/usage/savings-chart?days=30`. X-axis = date, Y-axis = tokens_saved or
estimated_saved_usd per day. Fill zero-gaps client-side so the chart is continuous.

### Per-session table (`recent[]`)

Columns to show: `timestamp`, `model`, `savings_pct × 100` (as %), `total_cost_usd`,
`naive_cost_usd`, `tokens_avoided`.

Format `savings_pct` as a percentage badge:
- ≥ 60% → green
- 30–60% → yellow
- < 30% → grey (low-savings session, likely short or first-run)

`tool_hits` is a JSON string — parse it and show the top hit type as a tooltip
(e.g. "symbol_excerpt × 3" means 3 symbol-level reads this session).

### Cache efficiency card

```
cache_hit_rate = total_cache_read_tokens
               / (total_cache_read_tokens + total_cache_write_tokens)
               × 100
```

High is good (≥ 90% is excellent). First session in a project always writes;
subsequent sessions read from cache. Long sessions → higher hit rate.

---

## Field reference (all fields in usage_events)

| Field | Type | Meaning |
|---|---|---|
| `license_key` | text | `GRP-XXXX-XXXX-XXXX` |
| `session_id` | text | UUID from Claude Code Stop hook |
| `timestamp` | ISO 8601 | When the session ended |
| `model` | text | Last model seen in the transcript |
| `input_tokens` | int | Uncached input tokens |
| `output_tokens` | int | Output tokens |
| `cache_read_tokens` | int | Tokens read from prompt cache (cheap) |
| `cache_write_tokens` | int | Tokens written to prompt cache |
| `total_cost_usd` | float | Actual API cost this session |
| `naive_cost_usd` | float | What it would have cost without GrapeRoot |
| `savings_pct` | float | 0.0–1.0 — multiply by 100 for % |
| `tokens_served` | int | Tokens GrapeRoot actually returned to Claude |
| `tokens_avoided` | int | Total tokens not sent to Claude (TAR + cross + shadow) |
| `tokens_avoided_tar` | int | Avoided by symbol-level excerpting |
| `tokens_avoided_cross_turn` | int | Avoided by cross-turn dedup pointers |
| `shadow_tokens_avoided` | int | Greps + file reads replaced by graph lookups |
| `graperoot_overhead_tokens` | int | GrapeRoot's own token cost (small) |
| `tool_hits` | JSON string | `{"symbol_excerpt": 2, "native_full": 1}` |
| `task_type` | text | Always `"prompt"` — ignore |
| `confidence` | text | Always `"none"` — ignore |
| `project_hash` | text | SHA256 prefix of cwd — group sessions by project |
| `device_host` | text | Hostname — group by machine |

---

## Notes

- Old sessions (pre-v1.0.45) have `tokens_avoided = 0` and the breakdown fields = 0.
  Display them normally — the zero just means "no graph-savings data for this session".
  The `total_cost_usd` and `naive_cost_usd` are still accurate (cache savings still counted).

- `savings_pct` stored as 0.0–1.0. Always multiply by 100 before displaying as a percentage.

- `by_day` in `/api/usage/stats` only returns days with sessions. Fill gaps client-side
  for a continuous chart.

- `/api/usage/savings-chart` is the canonical source for the daily savings trend.
  Use it instead of computing from `by_day` — it aggregates from the `token_savings`
  table which is written per-session and de-duplicated by device, so multi-machine
  users are handled correctly.

- The `month` param in `/api/usage/stats` defaults to current UTC month.
  Offer a month picker — pass `month=2026-05` for history.

- Auth: `license_key` query param only. No OAuth, no JWT.
