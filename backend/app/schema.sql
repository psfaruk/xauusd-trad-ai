-- XAUUSD platform schema (SPEC §6). Applied idempotently on backend start.
-- `auth.users` exists in Supabase; on plain Postgres tests create a stub first
-- (see tests/test_schema.py, DECISIONS.md D-009).

create table if not exists profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  role text not null default 'viewer' check (role in ('admin','viewer')),
  created_at timestamptz not null default now()
);

create table if not exists mt5_connections (
  id uuid primary key default gen_random_uuid(),
  owner uuid not null references profiles(id),
  server text not null,
  login text not null,
  enc_password text not null,              -- Fernet-encrypted
  terminal_path text,
  symbol text,                             -- discovered, e.g. XAUUSDm
  status text not null default 'disconnected',
  last_heartbeat timestamptz,
  created_at timestamptz not null default now()
);

-- Multi-user trading planes (Phase 4): one row per user, plus per-user arm.
alter table mt5_connections add column if not exists mode text not null default 'demo';
alter table mt5_connections add column if not exists auto_trade boolean not null default false;
-- D-075 — the link was ADMIN-verified against the live institution terminal
-- (restores at boot probe the terminal; public broker-links never do).
alter table mt5_connections add column if not exists broker_admin boolean not null default false;

create table if not exists engine_config (
  id int primary key default 1 check (id = 1),   -- single row
  config jsonb not null,
  auto_trade boolean not null default false,
  updated_by uuid references profiles(id),
  updated_at timestamptz not null default now()
);

-- D-036: AI-signal -> auto-order on the REAL MT5 terminal (explicit live
-- arm, separate from the paper auto_trade kill switch).
alter table engine_config add column if not exists auto_trade_live boolean not null default false;
alter table engine_config add column if not exists auto_trade_live_by uuid references profiles(id);

create table if not exists signals (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  symbol text not null,
  tf text not null default 'M15',
  direction text not null check (direction in ('BUY','SELL')),
  entry double precision not null,
  sl double precision not null,
  tp double precision not null,
  confidence double precision not null,
  trace jsonb not null,
  status text not null default 'active'
    check (status in ('active','won','lost','expired','cancelled')),
  result_r double precision,
  closed_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists signals_ts_idx on signals (ts desc);

-- D-054 — pending-limit signals carry their entry contract through REST so
-- the UI can show "PENDING LIMIT · waiting for fill price" after a refetch
-- (the in-memory tracker owns the live pending -> active/expired lifecycle).
alter table signals add column if not exists entry_type text not null default 'market';
alter table signals add column if not exists market_ref double precision;
alter table signals add column if not exists entry_note text;

create table if not exists trades (
  id uuid primary key default gen_random_uuid(),
  signal_id uuid references signals(id),
  owner uuid references profiles(id),
  ticket bigint unique,
  side text not null,
  volume double precision not null,
  price_open double precision not null,
  sl double precision, tp double precision,
  price_close double precision,
  profit double precision,
  opened_at timestamptz, closed_at timestamptz
);

-- D-044: per-user trading accounts (auto-provisioned practice plane for
-- every user — balance/settings persisted so a restart never loses state).
create table if not exists user_accounts (
  owner uuid primary key references profiles(id) on delete cascade,
  kind text not null default 'practice' check (kind in ('practice','broker')),
  balance double precision not null default 10000,
  currency text not null default 'USD',
  auto_trade boolean not null default false,
  settings jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

-- D-044: open positions of the per-user practice planes (restored on boot).
create table if not exists user_positions (
  ticket bigint primary key,
  owner uuid not null references profiles(id) on delete cascade,
  symbol text not null,
  side text not null check (side in ('BUY','SELL')),
  volume double precision not null,
  price_open double precision not null,
  sl double precision, tp double precision,
  opened_at timestamptz not null default now()
);
create index if not exists user_positions_owner_idx on user_positions (owner);

create table if not exists logs (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  level text not null,
  source text not null,
  message text not null,
  meta jsonb
);
create index if not exists logs_ts_idx on logs (ts desc);

-- Auto-create profile on signup (Supabase trigger). Idempotent: a profile
-- row created by /api/me's ensure_auth_user on plain Postgres (D-017) must
-- not make a re-insert fail.
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id, coalesce(new.raw_user_meta_data->>'full_name', new.email))
  on conflict (id) do nothing;
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Seed the single engine_config row with D-042 M1 ICT/MTF defaults (D-043: min_confluence 3).
insert into engine_config (id, config, auto_trade)
values (1, '{
  "timeframe": "M1", "trend_tf": "H1",
  "confirm_tfs": ["M5", "M15"], "min_tf_agree": 1,
  "ema_fast": 20, "ema_slow": 50, "trend_ema": 50,
  "rsi_period": 14, "rsi_buy_min": 40, "rsi_buy_max": 65,
  "rsi_sell_min": 35, "rsi_sell_max": 60,
  "atr_period": 14, "min_atr": 0.15,
  "sfp_lookback": 20, "sfp_wick_atr_ratio": 0.35,
  "sl_buffer_atr": 0.2, "rr": 1.1, "expiry_bars": 20, "cooldown_bars": 4,
  "pullback_enabled": true,
  "pullback_min_range_atr": 0.35, "pullback_wick_ratio": 0.45,
  "min_sl_atr": 1.5, "max_spread_to_risk": 0.5,
  "sessions": [{"name": "tokyo", "utc": [0, 7]}, {"name": "london", "utc": [7, 16]}, {"name": "newyork", "utc": [13, 20]}],
  "news_blackout_min": 30, "max_spread_points": 35,
  "risk_mode": "percent", "risk_percent": 0.5, "fixed_lot": 0.01,
  "max_positions": 3, "daily_max_loss_pct": 3.0, "magic": 234000,
  "smc_enabled": true, "bias_tfs": ["H4"], "min_confluence": 3,
  "max_zone_atr": 0.9, "vol_z_min": 0.8, "max_sl_atr": 3.5
}'::jsonb, false)
on conflict (id) do nothing;
