-- GBP/USD Trading Intelligence — runnable Supabase schema
-- Paste into Supabase SQL Editor and click Run.
-- Safe to re-run: uses IF NOT EXISTS throughout.

-- ── Core tables ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.news_events (
  id SERIAL PRIMARY KEY,
  your_name TEXT NOT NULL,
  ff_title TEXT,
  country TEXT NOT NULL,
  event_date TEXT NOT NULL,
  event_time TEXT,
  previous TEXT,
  forecast TEXT,
  actual TEXT,
  impact TEXT,
  beat_miss TEXT,
  user_note TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (your_name, event_date, event_time)
);

CREATE TABLE IF NOT EXISTS public.price_reactions (
  id SERIAL PRIMARY KEY,
  news_event_id INTEGER NOT NULL UNIQUE
    REFERENCES public.news_events(id) ON DELETE CASCADE,
  pip_5m REAL,
  pip_15m REAL,
  pip_30m REAL,
  pip_60m REAL,
  direction_5m TEXT,
  direction_15m TEXT,
  open_price REAL,
  price_5m REAL,
  price_15m REAL,
  price_30m REAL,
  price_60m REAL,
  fetched_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.trades (
  id SERIAL PRIMARY KEY,
  date TEXT NOT NULL,
  news1 TEXT,
  news2 TEXT,
  news3 TEXT,
  entry TEXT,
  ratio REAL,
  sl REAL,
  previous TEXT,
  forecast TEXT,
  actual TEXT,
  outcome TEXT,
  improvement TEXT,
  news_event_id INTEGER
    REFERENCES public.news_events(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  trade_type TEXT NOT NULL DEFAULT 'backtesting'
);

CREATE TABLE IF NOT EXISTS public.news_ratings (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  comment TEXT
);

CREATE TABLE IF NOT EXISTS public.settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS public.sync_log (
  id SERIAL PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  events_found INTEGER DEFAULT 0,
  events_new INTEGER DEFAULT 0,
  reactions_fetched INTEGER DEFAULT 0,
  status TEXT,
  error TEXT
);

-- ── Agent chat (optional; used if you enable agent features) ─

CREATE TABLE IF NOT EXISTS public.agent_sessions (
  id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  title TEXT DEFAULT 'New conversation'
);

CREATE TABLE IF NOT EXISTS public.agent_messages (
  id SERIAL PRIMARY KEY,
  session_id TEXT NOT NULL
    REFERENCES public.agent_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Indexes ───────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_news_events_date ON public.news_events (event_date);
CREATE INDEX IF NOT EXISTS idx_news_events_name ON public.news_events (your_name);
CREATE INDEX IF NOT EXISTS idx_trades_news_event_id ON public.trades (news_event_id);
CREATE INDEX IF NOT EXISTS idx_trades_trade_type ON public.trades (trade_type);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON public.agent_messages (session_id);

-- ── RLS (service role / backend bypasses; keep tables locked for anon) ─

ALTER TABLE public.news_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_reactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.news_ratings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_messages ENABLE ROW LEVEL SECURITY;
