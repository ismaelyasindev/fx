# GBP/USD Trading Intelligence — Cursor Project Brief

> **Read this entire document before touching any file.**
> This is a live trading tool built for a real user. Every decision has consequences on data integrity and trading decisions. Do not guess. Do not improvise structure. Follow this brief exactly.

---

## 1. What This Project Is

A personal trading intelligence platform for one trader (Yafet) trading GBP/USD on a 5-minute timeframe using a news-driven strategy. The platform has three jobs:

1. **Record** every trade manually with outcome, entry timing, R:R, and notes
2. **Analyse** historical GBP/USD price reactions to specific news events automatically
3. **Inform** future trades with data-driven pre-trade briefs before each news release

This is not a demo, not a toy, not a tutorial project. It informs real trading decisions. Accuracy matters.

Live trading (real money) is tracked separately from backtesting: the **Live Trades** page shows only `trade_type = 'live'`; Journal "My Trades" continues to show `trade_type = 'backtesting'`. Daily loss and trade-count limits protect capital.

---

## 2. The Trading Strategy (Read Carefully)

Yafet trades GBP/USD on the 5-minute chart. He enters trades around major USD and GBP economic news releases.

**Core rules:**
- Trade GBP/USD only
- 5-minute timeframe
- Entry: typically 5 minutes after news release, after confirming direction of reaction
- Stop Loss: default 10 pips (suggested SL from briefs is now `max(15, round(avg_heat * 1.2))` based on historical max adverse excursion)
- Take Profit: based on R:R ratio (suggested R:R from briefs uses post-entry data; see Section 11)
- Only trade red impact (HIGH) and orange impact (MEDIUM) news events
- Only trade events from his personal watchlist of 27 events (listed in scraper.py NEWS_MAP)

**Why post-entry metrics matter:** because he enters ~5 minutes after release, the 0–5m move is not capturable. Verdicts, tradeable flags, and suggested R:R use `post_entry_5_30` (move from the 5m price onward), not `pip_5m`.

**News confluence logic (from his Notes sheet):**
- 2 Good news events releasing same day = CAUTION (can cancel each other out)
- 2 Bad news events releasing same day = potentially tradeable reaction
- 1 Good + 1 Bad = potentially tradeable reaction
- Data BEAT forecast = strong directional signal
- Data MISS forecast = strong opposing signal
- Data inline with forecast = weak signal, be careful

**His news quality ratings (stored in news_ratings table):**
- Good = reacts well, worth trading
- Bad = low reaction power, avoid unless extreme move
- Caution = uncertain, check before entering
- Very Bad = skip entirely
- Unreliable = skip entirely

**Country matters:**
- USD news (🇺🇸) = moves GBP/USD because USD is the quote currency
- GBP news (🇬🇧) = moves GBP/USD because GBP is the base currency
- Both matter but behave differently — always show which country the news is from

---

## 3. Technical Stack

| Component | Technology |
|---|---|
| Backend | FastAPI (Python) |
| Database | **Supabase Postgres** (cloud) |
| DB driver | psycopg2-binary or psycopg3 (auto-detected in db.py) |
| DB wrapper | `db.py` — thin wrapper providing `get_db()` → `DbConnection` |
| Frontend | Single `public/index.html`, vanilla JS only |
| HTTP client | httpx + cloudscraper (for Forex Factory) |
| HTML parser | BeautifulSoup4 |
| Data validation | Pydantic v2 |
| Server | Uvicorn on port 8000 |
| News source | Forex Factory (scraped via cloudscraper to bypass Cloudflare) |
| Price data | OANDA v20 REST API |
| Config | `.env` file with `DATABASE_URL` pointing to Supabase connection pooler |

**No React. No npm. No build step. No TypeScript. No external CSS frameworks.**

The entire frontend is one `public/index.html` file served by FastAPI. All JavaScript is vanilla. All styles are inline in the `<style>` tag. This is intentional — keep it that way.

---

## 4. File Structure

```
trading_app 3/
├── app.py              # FastAPI backend — all routes, DB init, models
├── scraper.py          # Forex Factory scraper + OANDA price reaction engine
│                       #   + co-occurrence group builder + backfill + match audit
├── db.py               # Postgres connection wrapper (get_db, DbConnection, DbCursor)
├── public/
│   └── index.html      # ENTIRE frontend — HTML + CSS + JS in one file
├── schema.sql          # Supabase schema creation script (run once in SQL Editor)
├── requirements.txt
├── start.sh            # Mac startup script
├── CURSOR_BRIEF.md     # This file
└── .env                # DATABASE_URL=postgres://... (not committed to git)
```

**There is only one `index.html` — it lives at `public/index.html`.** The server serves it at `GET /`. Do not create a second one at the project root.

**Do not add new files unless absolutely necessary.** Add new Python logic to `scraper.py` or `app.py`. Add new frontend code to `public/index.html`.

The `.env` must contain:
```
DATABASE_URL=postgresql://postgres.[ref]:[password]@aws-0-eu-west-2.pooler.supabase.com:6543/postgres
```
Use the **Connection Pooler** URI (port 6543), not the direct db.* URI.

---

## 5. Database Schema

The database is **Supabase Postgres**. All SQL uses `%s` placeholders (not `?`). Primary keys are `BIGSERIAL` or `SERIAL`. The `db.py` wrapper returns rows as dicts (`RealDictRow` via psycopg2 or `dict_row` via psycopg3).

### trades
Manual trade log. One row per trade Yafet logged himself.

```sql
CREATE TABLE trades (
    id BIGSERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    news1 TEXT,
    news2 TEXT,
    news3 TEXT,
    entry TEXT,              -- timing description e.g. "5 min after checking reaction"
    ratio REAL,              -- R:R ratio e.g. 5 means 1:5
    sl REAL,                 -- stop loss in pips
    previous TEXT,
    forecast TEXT,
    actual TEXT,
    outcome TEXT,            -- "Win" or "Loss"
    improvement TEXT,        -- his notes on what he'd do differently
    news_event_id INTEGER,   -- FK to news_events (nullable)
    trade_type TEXT NOT NULL DEFAULT 'backtesting',  -- "live" or "backtesting"
    -- Live-money fields (added Jul 2026; nullable for older backtesting rows)
    account_balance REAL,    -- account balance at time of trade
    risk_percent REAL,       -- risk % used on this trade
    risk_amount REAL,        -- currency amount at risk
    lot_size REAL,           -- lots traded
    entry_price REAL,        -- actual entry price (5 d.p.)
    exit_price REAL,         -- actual exit price
    pnl REAL,                -- realised P&L in account currency
    spread_at_entry REAL,    -- spread in pips at entry
    direction TEXT,          -- "BUY" or "SELL"
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### news_events
Auto-populated by the scraper. One row per news release occurrence.

```sql
CREATE TABLE news_events (
    id BIGSERIAL PRIMARY KEY,
    your_name TEXT NOT NULL,   -- Yafet's name e.g. "Non Farm Payroll US"
    ff_title TEXT,             -- Forex Factory's title e.g. "Non-Farm Employment Change"
    country TEXT NOT NULL,     -- "USD" or "GBP"
    event_date TEXT NOT NULL,  -- YYYY-MM-DD
    event_time TEXT,           -- raw FF time string e.g. "8:30am"
    previous TEXT,
    forecast TEXT,
    actual TEXT,
    impact TEXT,               -- "red" or "orange"
    beat_miss TEXT,            -- "beat", "miss", "inline", or "unknown"
    user_note TEXT,            -- Yafet's personal note on this specific occurrence
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(your_name, event_date, event_time)
);
```

### price_reactions
OANDA candle data around each news event. One row per news_event.

```sql
CREATE TABLE price_reactions (
    id BIGSERIAL PRIMARY KEY,
    news_event_id INTEGER NOT NULL,
    pip_5m REAL,        -- absolute pip move from open to 5 min after release (unsigned; kept for backwards compatibility)
    pip_15m REAL,
    pip_30m REAL,
    pip_60m REAL,
    direction_5m TEXT,  -- "up" or "down"
    direction_15m TEXT,
    open_price REAL,    -- open of the 5M candle that starts at the release timestamp
                        -- Correctly: candles[event_idx]["mid"]["o"] where event_idx = 3
                        --   in fetch_price_reaction() — the candle open at event_dt
                        -- BUG NOTE: rows synced before Jul 2026 used candles[0]["mid"]["c"]
                        --   (close of candle 15 min before the event) — wrong baseline.
                        --   A full resync is needed to correct existing rows.
    price_5m REAL,
    price_15m REAL,
    price_30m REAL,
    price_60m REAL,
    -- Signed / post-entry metrics (added Jul 2026)
    signed_pip_5m REAL,     -- (price_at_Xm - open_price) * 10000; positive = price rose
    signed_pip_15m REAL,
    signed_pip_30m REAL,
    signed_pip_60m REAL,
    post_entry_5_30 REAL,   -- signed move from 5m price to 30m price, signed relative to
                            -- 5m direction so positive = continued in the 5m direction.
                            -- This is what the user can capture after a 5-min entry.
    post_entry_5_60 REAL,   -- same, 5m → 60m
    max_adverse_5_30 REAL,  -- largest move against the 5m direction between 5m and 30m
                            -- (candle highs/lows), as a positive pip number ("heat")
    is_co_released BOOLEAN DEFAULT FALSE,  -- TRUE when this event shared a timestamp with
                            -- another watchlist event. Excluded from per-event direction
                            -- consistency stats; still counted in occurrence totals.
    fetched_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(news_event_id)
);
```

### event_groups
Co-occurrence index — one row per unique (group, date) combination where 2+ watchlist events released at the same timestamp. Created by `ensure_tables()` in `scraper.py` and populated by `build_event_groups()` after every sync.

```sql
CREATE TABLE event_groups (
    id BIGSERIAL PRIMARY KEY,
    group_key TEXT NOT NULL,         -- pipe-joined sorted member names
                                     -- e.g. "Non Farm Payroll US|Unemployment US"
    event_date TEXT NOT NULL,        -- YYYY-MM-DD
    event_time TEXT,                 -- raw FF time string
    member_names TEXT NOT NULL,      -- same as group_key (for readability)
    scenario TEXT,                   -- pipe-joined beat_miss per member in same
                                     -- sorted order e.g. "beat|miss"
    price_reaction_id INTEGER,       -- FK to price_reactions.id (lowest-id member)
    created_at TEXT,
    UNIQUE(group_key, event_date)
);
```

`build_event_groups()` also sets `price_reactions.is_co_released = TRUE` for every member of a group with 2+ events.

### news_ratings
Yafet's personal quality rating for each event type.

```sql
CREATE TABLE news_ratings (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,  -- matches your_name in news_events
    type TEXT NOT NULL,         -- Good / Bad / Very Bad / Caution / Unreliable
    comment TEXT
);
```

### settings
Key-value store for app configuration.

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Keys:
--   token, account_id, env (practice/live)     — OANDA connection
--   starting_balance (default "5000")
--   account_currency (default "GBP")
--   default_risk_pct (default "2")
--   daily_loss_limit_pct (default "6")
--   max_trades_per_day (default "3")
```

### sync_log
One row per sync run.

```sql
CREATE TABLE sync_log (
    id BIGSERIAL PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    events_found INTEGER DEFAULT 0,
    events_new INTEGER DEFAULT 0,
    reactions_fetched INTEGER DEFAULT 0,
    status TEXT,
    error TEXT
);
```

---

## 6. The 27 Tracked News Events (NEWS_MAP in scraper.py)

**Do not change the 27 event names or their keywords.** These are Yafet's calibrated event list. Do not add or remove entries without being explicitly told to. You may only add or edit **exclusion lists** (see below).

`NEWS_MAP` entries are **5-tuples**:

```python
(your_name, ff_keyword, country, min_impact, exclusions_list)
```

Matching (`match_event(ff_title, ff_currency, ff_impact)`):
1. Currency must match
2. Keyword substring must appear in `ff_title` (case-insensitive)
3. Reject if any exclusion substring appears in `ff_title` (case-insensitive)
4. Impact guard: reject if impact is a downgrade vs `min_impact` (orange required → red is OK; red required → orange is not)

| Yafet's Name | Country | His Rating |
|---|---|---|
| Non Farm Payroll US | USD | Good |
| Unemployment US | USD | Good |
| ISM Services US | USD | Good |
| Retail UK | GBP | Good |
| GDP UK | GBP | Good |
| Existing Home Sales US | USD | Good |
| Core Inflation MoM US | USD | Good |
| Inflation Rate YoY US | USD | Caution |
| Inflation Rate YoY UK | GBP | Caution |
| FOMC US | USD | Caution |
| Building Permits US | USD | Caution |
| Housing Starts US | USD | Caution |
| S&P Global Manufacturing PMI UK | GBP | Caution |
| S&P Global Services PMI UK | GBP | Caution |
| BOE Interest Rate US | GBP | Caution |
| Fed Chair Powell Speech US | USD | Caution |
| ISM Manufacturing US | USD | Bad |
| Personal Spending US | USD | Bad |
| Core PCE MoM US | USD | Bad |
| PPI US | USD | Bad |
| PPI MoM US | USD | Bad |
| Michigan Consumer Sentiment US | USD | Bad |
| Retail US | USD | Very Bad |
| Durable Goods US | USD | Bad |
| JOLTs Job Openings US | USD | Bad |
| GDP US | USD | Unreliable |
| Unemployment UK | GBP | Bad |

**Exclusion lists** (disqualifying substrings; all other events use `[]`):

| Event | Exclusions |
|---|---|
| Non Farm Payroll US | `["ADP"]` |
| Unemployment US | `["Claims", "ADP"]` |
| GDP US | `["Price Index", "Prelim", "Final"]` |
| GDP UK | `["Price Index", "Prelim", "Final"]` |
| PPI US | `["Core"]` |
| PPI MoM US | `["Core"]` |
| BOE Interest Rate US | `["Speech", "Minutes", "Testimony", "Gov ", "Report", "Summary"]` |
| Fed Chair Powell Speech US | `["Minutes"]` |
| Retail US | `["Core"]` |
| Retail UK | `["Core"]` |
| Inflation Rate YoY US | `["Core", "PPI"]` |
| Inflation Rate YoY UK | `["Core", "PPI"]` |
| Core Inflation MoM US | `["PPI"]` |

**Match audit:** Settings → Match Audit (`GET /api/audit/matches`) lists distinct `ff_title` values per watchlist name. Amber flag if more than one distinct title. **Re-check event matching** (`POST /api/admin/reclassify`) re-runs `match_event` and deletes only rows that no longer match any watchlist entry (cascades to `price_reactions` / `event_groups`), after a confirmation dialog showing the count.

**Known co-release pairs** (always release at the same timestamp):
- Non Farm Payroll US + Unemployment US (first Friday of every month)
- S&P Global Manufacturing PMI UK + S&P Global Services PMI UK (Flash PMI day)

These pairs have a dedicated `event_groups` table and a Combined Brief that shows true co-occurrence statistics rather than duplicated individual stats. Per-event direction stats additionally exclude rows where `is_co_released = TRUE` (see Section 11).

---

## 7. Design System (Do Not Deviate)

### Colours

```css
--bg: #0a0f1e;           /* Page background */
--surface: #141b2d;      /* Cards, sidebar */
--surface2: #1c2640;     /* Input fields, hover states */
--border: #2a3556;       /* All borders */
--accent: #f5a623;       /* Primary accent, active nav, buttons */
--accent-dim: rgba(245,166,35,0.12);
--green: #22c55e;        /* Win, positive, beat */
--green-dim: rgba(34,197,94,0.12);
--red: #ef4444;          /* Loss, negative, miss */
--red-dim: rgba(239,68,68,0.12);
--amber: #f59e0b;        /* Caution, warning */
--amber-dim: rgba(245,158,11,0.12);
--blue: #3b82f6;         /* Data/info, sync button */
--blue-dim: rgba(59,130,246,0.12);
--text: #e8edf5;         /* Primary text */
--text2: #8b9ab8;        /* Secondary text, labels */
--text3: #4a5568;        /* Muted text, placeholders */
```

### Typography

```css
--mono: 'JetBrains Mono', monospace;  /* ALL numbers, prices, pips, dates, codes */
--sans: 'Inter', sans-serif;           /* ALL labels, UI text, body copy */
```

**Rule:** Every number displayed to the user must use `font-family: var(--mono)`. This includes: prices, pip counts, R:R ratios, percentages in analysis, dates, times, balances, lot sizes, and P&L.

### Country Display

Always show country with flag emoji and coloured text:
- USD: `🇺🇸 USD` in `#60a5fa` (blue)
- GBP: `🇬🇧 GBP` in `#f472b6` (pink)

### Badges

```html
<!-- Outcome -->
<span class="badge win">WIN</span>
<span class="badge loss">LOSS</span>

<!-- Beat/Miss -->
<span class="badge win">BEAT</span>
<span class="badge loss">MISS</span>
<span class="badge neutral">INLINE</span>

<!-- Impact -->
<span class="badge loss">RED</span>
<span class="badge caution">ORANGE</span>

<!-- Tradeable -->
<span class="tradeable-badge tradeable-yes">✓ YES</span>
<span class="tradeable-badge tradeable-no">✗ NO</span>
```

### Pip Pills (Analysis Table)

```html
<!-- >= 15 pips = high = green -->
<span class="pip-pill high">19.1p</span>

<!-- >= 8 pips = mid = amber -->
<span class="pip-pill mid">9.5p</span>

<!-- < 8 pips = low = red -->
<span class="pip-pill low">4.2p</span>

<!-- No data -->
<span class="pip-pill none">—</span>
```

### Special Tags

```html
<!-- Multi-event release warning (This Week page) -->
<span class="multi-event-tag">⚠ Multi-event release — open together</span>

<!-- Shared reading attribution warning (Analysis page) -->
<span class="shared-reading-tag" title="...tooltip text...">SHARED READING</span>

<!-- Fewer than 5 solo occurrences — use Combined Brief -->
<span class="shared-reading-tag" title="...">FEW SOLO</span>
```

---

## 8. Current Sidebar Navigation Order

```
📅 This Week
💷 Live Trades          ← real-money trades only (trade_type = 'live')
📊 Dashboard
📈 Performance
🔬 Analysis
📖 Journal              ← My Trades tab = backtesting only
🎯 News Scorer
➕ Log Trade
📰 News Ratings
⚙️ Settings
```

---

## 9. What Has Already Been Built (Do Not Rebuild)

### Infrastructure
- Supabase Postgres database with `db.py` wrapper (auto-detects psycopg2 vs psycopg3)
- FastAPI backend with all routes in `app.py`
- OANDA API proxy — **all OANDA calls go through Python**; browser never calls OANDA directly
- Background sync with live progress tracking (SSE-style polling every 3s in UI)
- Full resync from **Jan 2015** (`POST /api/sync/full`) — warns user it takes **2–3 hours; run overnight**
- Regular sync (`POST /api/sync`) default start remains **2025-01-20**
- Automatic `backfill_missing_reactions()` on app startup and at end of every sync; Settings card + `POST /api/backfill`

### Scraper (`scraper.py`)
- `scrape_month(year, month, client, watchlist_only)` — scrapes one FF calendar month
  - `watchlist_only=True` (sync): only NEWS_MAP matches
  - `watchlist_only=False` (This Week): all USD/GBP red/orange events + maps to watchlist where possible
- `scrape_upcoming(days=7)` — scrapes next N days; saves only watchlist matches to DB; returns all FF events to frontend
- `save_events(events)` — upserts to news_events with ON CONFLICT DO UPDATE
- `fetch_price_reaction(event, client, cfg)` — fetches OANDA 5M candle window; computes absolute pips, signed pips, post-entry moves, max adverse heat
- `save_reaction(event_id, reaction)` — saves to price_reactions (including new signed/post-entry columns)
- `build_event_groups()` — called at end of every sync; populates `event_groups` and sets `is_co_released`
- `get_group_analysis(group_key)` — returns co-occurrence statistics by scenario for a group
- `get_brief(event_name)` — full pre-trade brief data (post-entry averages, heat, solo count, suggested SL/R:R)
- `get_analysis_summary()` — per-event aggregated statistics; direction stats from solo rows only; tradeable = `avg_post_entry_5_30 >= 10`
- `backfill_missing_reactions()` — fills price_reactions for past events that have actuals but no reaction row
- `audit_matches()` / `audit_reclassify()` — match-quality audit and optional cleanup of false-positive rows

### Pages

**This Week** — Upcoming 7 days of USD/GBP news scraped live from Forex Factory:
- Shows ALL red/orange USD/GBP events (not just watchlist) with their FF titles
- For watchlist events: shows mapped name, your rating badge, Pre-Trade Brief button
- For unmapped events: shows "Not on watchlist yet", disabled brief button
- Detects same-time watchlist groups (events within 30 min of each other): shows `⚠ Multi-event release` amber tag and a "Combined Brief" button that opens all events together
- Refresh button to re-scrape live

**Live Trades** — Real-money positions only (`trade_type = 'live'`):
- Four stat cards: Account Balance (`starting_balance + sum(pnl)`), Total P&L, Today's P&L, Live Win Rate
- Daily limit banner (red / amber / green) from `/api/daily-status`
- Position size calculator (also embedded in Live Trade Assistant Stage C) with drawdown table and risk warnings
- Consistency check if `default_risk_pct > daily_loss_limit_pct`
- Table: Date | Events | Direction | Entry | Exit | Lots | SL | Spread | Risk | P&L | Result | Notes | delete
- Does **not** merge with Journal backtesting trades

**Dashboard** — At-a-glance trade performance:
- Win rate, avg R:R, total trades, P&L chart by month
- Filterable by trade type (live / backtesting / all)
- Live GBP/USD price in sidebar header (polls OANDA)

**Performance** — Detailed personal statistics:
- Top stats (total, wins, losses, win rate, avg R:R, expectancy)
- Monthly performance table with bars
- Performance by event type and entry timing
- Last 10 trades as coloured dots
- Improvement tracker and next milestone

**Analysis** — Historical news event statistics (from price_reactions join):
- Sortable table: Event, Impact, Count (total · solo · co-released), Pre-Entry 5M (not capturable, muted), Post-Entry (5→30m), Avg Heat, 15M/30M, Beat/Miss direction, Tradeable, What This Means
- Tradeable flag uses `avg_post_entry_5_30 >= 10` (not avg 5M)
- Events with &lt; 5 solo occurrences: direction cells show `—` with FEW SOLO / SHARED READING guidance toward Combined Brief
- Filters: date range (**All Time** / **Post-Covid (2020-03-01+)** / **Post-Trump (2025-01-20+)** / Custom), country, impact, tradeable-only (post-entry)
- Note under filter: different historical periods behave differently; do not mix regimes casually
- `SHARED READING` amber tag on any event in a co-occurrence group with 8+ joint occurrences (hover shows tooltip explaining the double-attribution problem)
- Click any row to expand full occurrence history with all dates and actual pip moves

**Journal** — Two-tab view:
- Tab 1 (All Events): every news_events row joined with price_reactions, date/country/impact filters (same range presets as Analysis), editable inline user notes, "I Traded This" button creates a linked trade
- Tab 2 (My Trades): full manual trade history with delete — **backtesting** trades

**Pre-Trade Brief (modal)** — Triggered from This Week for any watchlist event:
- Historical stats: occurrence count, avg pip move, beat/miss direction consistency (solo-based)
- "What the data says": after a 5-minute entry, historically [X]p further movement with [Y]p heat
- Decision tree: three boxes (IF BEAT / IF MATCH / IF MISS) with directional signals — replaced by Combined Brief link when solo count &lt; 5
- Magnitude section: small/medium/large surprise expectations
- Your trade plan: 6-step checklist; suggested SL = `max(15, round(avg_heat * 1.2))`; suggested R:R from post-entry / SL (capped 1–5; warning if &lt; 1)
- Warnings: low power, unpredictable direction, your own rating note, same-day events
- Your history: past trades on this event
- **Live Trade Assistant** (see below)

**Live Trade Assistant (inside Pre-Trade Brief)** — Guides entry in real time:
- **Blocking**: before Stage A, checks `/api/daily-status`; if daily loss or trade limit hit, replaces the whole assistant with a red explanation (no override)
- **Stage Before**: Enter actual number when news releases. Detects same-time events automatically from `thisWeekEventsCache`; if found, shows multi-input UI and "Get Combined Signal" button instead of "Start Live Mode"
- **Stage A**: Calculates beat/miss/match (5% threshold), shows BUY/SELL/SKIP verdict with confidence and expected pip move. In multi-event mode, shows combined verdict (see below)
- **Stage B**: 5-minute countdown timer with live OANDA price polling every 15s. Shows open price, current price, pips moved, direction status
- **Stage C**: After countdown, checks direction confirmation; required fields: entry price, lot size (from calculator); optional spread; position calculator embedded
- **Stage D**: Live currency P&L (not pip-only) using lots × pip value; SL/TP progress; on close prompts for exit price, saves `trade_type = 'live'` with money fields, navigates to Live Trades

**Multi-Event Live Mode** — When 2+ watchlist events release within 30 minutes of each other:
- Auto-detected using `thisWeekEventsCache` (populated on This Week page load)
- Fetches briefs for all events in parallel via `Promise.all()`
- Builds individual verdicts (signal, confidence, occurrences, surprise %)
- **If `event_groups` has 8+ joint occurrences (Case A)**: looks up the exact beat/miss/inline scenario string in `get_group_analysis()` results; uses real co-occurrence direction/consistency/pip data
- **If fewer than 8 joint occurrences (Case B)**: resolves conflicts with tiebreaker algorithm: confidence gap ≥15% → surprise magnitude ≥1.5× → occurrence count → NO TRADE
- All subsequent stages (B/C/D) use the winning combined direction

**Combined Brief (modal)** — Opened from "Combined Brief" button on This Week:
- Shows co-occurrence table "What Happens When These Release Together" if 8+ joint occurrences, with per-scenario rows (label, times seen, direction, avg pips, reliability%)
- Otherwise shows side-by-side individual decision trees with amber note about limited data
- Live Trade Assistant initialised in multi-event mode with pre-loaded briefs (no double-fetch)

**News Scorer** — Signal engine: enter 1-3 events, get GO / CAUTION / NO TRADE based on confluence rules + news ratings

**Log Trade** — Manual trade entry form with all fields; supports pre-fill from Live Trade Assistant close

**News Ratings** — Editable list of all 27 event quality ratings with comments

**Settings** — OANDA API connection (token, account ID, env); **Risk Management** card (starting_balance, account_currency, default_risk_pct, daily_loss_limit_pct, max_trades_per_day); Match Audit + Re-check event matching; Missing price reactions count + Backfill now; Full Resync button (2015, overnight warning); co-occurrence group count; strategy rules reference

---

## 10. All API Endpoints (app.py)

```
GET  /api/health                    # DB + app status
GET  /api/trades                    # all trades (filterable by trade_type)
POST /api/trades                    # create trade (includes live money fields)
DELETE /api/trades/{id}             # delete trade
GET  /api/trade/{id}/analysis       # single trade detail

GET  /api/ratings                   # all news_ratings
POST /api/ratings                   # create/update rating
DELETE /api/ratings/{name}          # delete rating

GET  /api/settings                  # OANDA config + risk settings
POST /api/settings                  # save OANDA config
POST /api/settings/risk             # save risk management settings
GET  /api/daily-status              # today P&L, trade count, limit breach flags

GET  /api/oanda/price               # historical price data
GET  /api/oanda/current-price       # live GBP/USD mid price
POST /api/oanda/test                # test OANDA connection

POST /api/score                     # News Scorer signal engine

GET  /api/analytics                 # trade stats aggregated for Dashboard
GET  /api/performance               # detailed performance stats

POST /api/sync                      # start background sync (default from 2025-01-20)
POST /api/sync/full                 # full resync from Jan 2015 (2–3 hours; overnight)
GET  /api/sync/status               # current sync progress + last sync log

GET  /api/analysis                  # per-event stats summary + group_count + shared_reading
                                    # + post-entry / heat / solo vs co-released counts
GET  /api/analysis/{event_name}     # full occurrence history for one event
GET  /api/group-brief?events=A&events=B  # co-occurrence stats for a group of events

GET  /api/journal                   # news_events joined with price_reactions
POST /api/journal/{id}/note         # save user_note on a news_event row
POST /api/journal/{id}/trade        # create trade linked to a news_event

GET  /api/upcoming                  # next 7 days from FF (live scrape or DB fallback)
GET  /api/brief/{event_name}        # full pre-trade brief for one event type

GET  /api/audit/matches             # distinct ff_title values per watchlist name
POST /api/admin/reclassify          # ?confirm=false dry-run count; ?confirm=true delete mismatches
GET  /api/backfill/status           # count of events missing price_reactions
POST /api/backfill                  # background backfill of missing reactions

GET  /                              # serves public/index.html
```

---

## 11. Key Architecture Decisions

### The `This Week` display vs the analysis pipeline

`scrape_upcoming()` returns ALL USD/GBP red/orange events from Forex Factory (even unmapped ones) to show the full calendar. But it only **saves** watchlist-matched events to `news_events` in the database. This keeps the analysis pipeline clean — only your 27 tracked events contribute to statistics.

### Why verdicts use post-entry data rather than the 0–5m move

The user enters ~5 minutes after release, after confirming direction. The 0–5m move (`pip_5m` / `signed_pip_5m`) has already happened and is **not capturable**. Analysis and briefs therefore:

- Treat Pre-Entry 5M as context only (muted in the UI)
- Use `avg_post_entry_5_30` for tradeable flags (`>= 10`), "What This Means", power verdicts, and suggested R:R
- Size suggested SL from historical heat: `max(15, round(avg_heat * 1.2))` where heat is `avg(max_adverse_5_30)`
- Compute suggested R:R as `floor(avg_post_entry_5_30 / suggested_sl * 10) / 10`, capped between 1 and 5; if below 1, show that the event does not support usual targets

### The co-occurrence attribution problem

When two events release at the same timestamp (e.g. NFP + Unemployment), `fetch_price_reaction()` fetches the **same OANDA candle** for both and writes identical pip values to two separate `price_reactions` rows. This means per-event statistics are computed by correlating one price move against two different beat/miss labels. The `event_groups` table solves this by storing the combined scenario → reaction mapping, so the Combined Brief shows real joint statistics instead of doubled-up individual stats.

Additionally, `is_co_released` flags those rows so **per-event direction-consistency stats exclude co-released occurrences** while still including them in total occurrence counts. Analysis shows `Count: [total] ([N] solo, [M] co-released)`. With fewer than 5 solo occurrences, direction cells are blanked and the brief points to Combined Brief.

### `event_groups` is populated only during sync

After deploying a change that introduces `event_groups`, the user must run a sync once (Sync Now or Full Resync) for the table to fill. The Settings page shows the current count. Until then, Combined Brief falls back to the tiebreaker algorithm — no data is lost. `build_event_groups()` also refreshes `is_co_released` flags.

### Automatic price reaction backfill

Events saved via This Week often have `actual` before a full sync has fetched OANDA data. `backfill_missing_reactions()` selects past `news_events` with non-empty `actual` and no `price_reactions` row, fetches reactions with a 0.3s delay between calls, runs at end of `run_sync()`, on app startup (background), and via Settings / `POST /api/backfill`. This is independent of whether the user logged a trade.

### `thisWeekEventsCache` (frontend global)

This Week loads all events into `thisWeekEventsCache` (a JS array). The Live Trade Assistant reads this cache to auto-detect same-time events when a brief is opened. If the user opens a brief without visiting This Week first, the cache is empty and single-event mode is used as the safe fallback.

### `trade_type` field

All trades have `trade_type` = `"live"` or `"backtesting"`. Seed data uses `"backtesting"`. Dashboard and Performance pages can filter by this. Live Trades page is live-only; Journal My Trades remains backtesting-focused for the personal log workflow.

### Position sizing (GBP account trading GBP/USD)

```
pip_value_gbp_per_lot = 10 / current_gbpusd_rate
risk_amount           = balance * risk_pct / 100
lot_size              = risk_amount / (sl_pips * pip_value_gbp_per_lot)
```

---

## 12. Critical Rules — Do Not Break These

### Never do these:
- **Do not make the browser call OANDA directly.** All OANDA calls go through Python. Browser → Python → OANDA. OANDA blocks browser requests (CORS).
- Do not add React, Vue, npm, webpack, or any build toolchain.
- Do not create new HTML files. The only frontend file is `public/index.html`.
- Do not create a second `index.html` at the project root. There is only one.
- Do not change the database schema destructively. Only ADD columns/tables, never remove or rename existing ones.
- Do not change the 27 event names or their keywords in `NEWS_MAP`. Only add/edit exclusion lists when instructed.
- Do not change `news_ratings` seed data in `app.py` `init_db()`.
- Do not change the colour scheme.
- Do not add loading spinners that block the entire page — use inline "Loading..." text in the specific section being loaded.
- Do not use `localStorage` or `sessionStorage` for anything except temporary UI state. All persistent data goes to Postgres via the API.
- Do not manually delete or rebuild `price_reactions` rows. They represent real historical data. The only allowed deletion path is `POST /api/admin/reclassify?confirm=true` after the Settings confirmation dialog stating how many rows will be removed.
- Do not hard-code database names or table names outside of `db.py`, `scraper.py`, and `app.py`.
- Do not provide an override button when daily loss or trade limits are breached in the Live Trade Assistant.

### Always do these:
- Use `font-family: var(--mono)` for every number, price, pip value, percentage, date, and time shown to the user.
- Show country flags for every news event everywhere it appears.
- Wrap all new database operations in `try/except` so failures never break sync or page load.
- Use `%s` placeholders in all SQL queries (Postgres style, not `?`).
- Wrap all frontend API calls in `try/catch` and display errors inline, never as `alert()`.
- Keep the sidebar connection status dot updated — green when OANDA is connected, red when not.
- After any sync or data change, allow the user to refresh the current view without navigating away.
- When adding a new DB table, create it in `ensure_tables()` in `scraper.py` using `CREATE TABLE IF NOT EXISTS`.
- When adding columns, use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in its own try/except.
- Use post-entry metrics (`avg_post_entry_5_30`) for tradeable verdicts, not `avg_pip_5m`.
- Exclude `is_co_released` rows from per-event direction-consistency calculations.

---

## 13. The Bigger Picture (Context for Decisions)

This platform is the foundation of Yafet's trading journey. The goal is to build a data-driven edge over time by:

1. Logging every trade with real data
2. Comparing actual entries to what the data says is optimal
3. Building pattern recognition across hundreds of news event occurrences
4. Eventually reaching a point where before any news release, the system tells him exactly: tradeable or not, which direction to expect, suggested entry/SL/TP — based on real historical data from his own market

Every feature you build should serve that journey. If a feature doesn't help him make better trading decisions or log his trades more accurately, don't build it.

---

## 14. Running the App

```bash
cd "/Users/yafetwolde/Downloads/trading_app 3"
python app.py
```

Opens at `http://localhost:8000`. Stop with `Ctrl+C`.

**Required before first run:** create a `.env` file in the project root:
```
DATABASE_URL=postgresql://postgres.[ref]:[password]@aws-0-eu-west-2.pooler.supabase.com:6543/postgres
```

To install dependencies (one time):
```bash
pip install -r requirements.txt
```

Current `requirements.txt`:
```
fastapi==0.115.0
uvicorn==0.30.6
httpx==0.27.2
pydantic==2.9.2
beautifulsoup4==4.12.3
psycopg2-binary==2.9.10
psycopg[binary]>=3.3.0
python-dotenv==1.0.1
cloudscraper==1.2.71
```

**After first deploy or after a schema change:** open the Supabase SQL Editor, paste `schema.sql` (or the migration ALTER/INSERT statements for new columns), and click Run. Then click **Sync Now** in the app to populate data. Restart the Python server after code changes so new API routes (e.g. `/api/settings/risk`) are registered.
