/**
 * market-service — the XAUUSD AI platform backend (port of the Python
 * FastAPI app to a bun mini-service):
 *  - HTTP REST under /api/* (served on port 3003; the Next.js frontend
 *    reaches it through the gateway via ?XTransformPort=3003)
 *  - socket.io on path "/" for real-time tick/bar/signal/pulse frames
 *  - the market engine — 100% MetaTrader 5 data: the REAL terminal
 *    (Exness-MT5Trial6, login 414350770) running under user-space Wine
 *    feeds every price, candle, tick, history and account number via its
 *    built-in MCP server. NO other source exists in this codebase.
 *  - per-token practice trading planes (marked-to-market on real MT5 quotes)
 */

import { createServer } from "http";
import { Server } from "socket.io";
import { engine } from "./engine";
import { MARKET_SPECS, MARKET_MAP, TIMEFRAMES } from "./tape";
import {
  mt5Quotes,
  mt5History,
  mt5AccountInfo,
  mt5OpenPositions,
  sourceHealth,
} from "./providers";
import { planes, planeFor, DEFAULT_SETTINGS } from "./trading";

const PORT = 3003;

/* -------------------------------------------------------------------- auth */

interface UserRec {
  id: string;
  email: string;
  role: "admin" | "viewer";
}

const users = new Map<string, UserRec>();
let userSeq = 1;

function b64url(s: string): string {
  return Buffer.from(s).toString("base64url");
}

function mintToken(email: string): { token: string; user: UserRec } {
  const user: UserRec = {
    id: `u-${b64url(email).slice(0, 10)}`,
    email,
    role: "admin", // demo preview: everyone is admin (full feature surface)
  };
  const token = `tok.${b64url(email)}.${Math.random().toString(36).slice(2, 10)}`;
  users.set(token, user);
  return { token, user };
}

/**
 * Stateless resurrection: the token embeds the email (base64url segment 1),
 * so a token minted by a PREVIOUS process instance (hot-reload / restart)
 * still resolves to its user — sessions survive service restarts.
 */
function userOf(token: string | null | undefined): UserRec | null {
  if (!token) return null;
  const known = users.get(token);
  if (known) return known;
  const parts = token.split(".");
  if (parts.length === 3 && parts[0] === "tok") {
    try {
      const email = Buffer.from(parts[1], "base64url").toString("utf8");
      if (email.includes("@") && email.length < 200) {
        const user: UserRec = {
          id: `u-${b64url(email).slice(0, 10)}`,
          email,
          role: "admin",
        };
        users.set(token, user);
        return user;
      }
    } catch {
      /* malformed token — treat as unauthenticated */
    }
  }
  return null;
}

function bearer(req: import("http").IncomingMessage): string | null {
  const h = req.headers.authorization;
  if (!h || !h.startsWith("Bearer ")) return null;
  return h.slice(7);
}

/* ------------------------------------------------------------------ helpers */

type Handler = (
  req: import("http").IncomingMessage,
  res: import("http").ServerResponse,
  url: URL,
  body: Record<string, unknown> | null,
) => void | Promise<void>;

function json(res: import("http").ServerResponse, code: number, payload: unknown): void {
  const buf = Buffer.from(JSON.stringify(payload));
  res.writeHead(code, {
    "Content-Type": "application/json",
    "Content-Length": buf.length,
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  });
  res.end(buf);
}

function fail(res: import("http").ServerResponse, code: number, detail: string): void {
  json(res, code, { detail });
}

function readBody(req: import("http").IncomingMessage): Promise<Record<string, unknown> | null> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      if (!chunks.length) return resolve(null);
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        resolve(null);
      }
    });
    req.on("error", () => resolve(null));
  });
}

/** Feed status block — 100% MetaTrader 5 (mirrors the Python /api/mt5/status payload). */
function feedStatus(): Record<string, unknown> {
  const symbols = engine.feedInfo();
  const xau = engine.ticks.XAUUSD;
  const degraded = MARKET_SPECS.some((s) => {
    const info = symbols[s.key] as { degraded?: boolean } | undefined;
    return info?.degraded === true;
  });
  const account = engine.mt5Account ?? null;
  return {
    provider: "metatrader5",
    detail:
      "MetaTrader 5 terminal (build " + (account?.terminal?.build ?? "?") + ") — " +
      "Exness-MT5Trial6, login 414350770. Every symbol streams from the broker; " +
      "no other data source exists in this platform.",
    last_price: xau ? Number((((xau.bid + xau.ask) / 2)).toFixed(2)) : null,
    spread: xau ? Number((xau.ask - xau.bid).toFixed(2)) : null,
    last_tick_age_s: xau ? Math.max(0, Math.floor(Date.now() / 1000) - xau.ts) : null,
    tps: Number((MARKET_SPECS.reduce((a, s) => a + engine.tickMeter[s.key].tps, 0)).toFixed(1)),
    symbols,
    mt5: {
      connected: account?.terminal?.server_connected === true,
      broker: account?.account?.broker ?? "Exness Technologies Ltd",
      server: account?.account?.server ?? "Exness-MT5Trial6",
      login: account?.account?.login ?? "414350770",
      build: account?.terminal?.build ?? null,
      last_ping_ms: account?.terminal?.server_last_ping ?? null,
      note:
        "The REAL MetaTrader 5 terminal runs in this environment (user-space Wine) " +
        "and serves every market via its MCP bridge — all pairs, all timeframes, " +
        "history and account data come from the broker.",
    },
    note:
      "DATA_SOURCE=metatrader5 — Exness-MT5Trial6 terminal (login 414350770); " +
      "no Binance/Yahoo/simulated path exists anywhere in the code",
    degraded,
  };
}

/**
 * MT5 status — the REAL terminal account (balance/equity/positions from
 * Exness) + the practice-plane (paper trading) view the UI trades on.
 */
async function mt5Status(token: string): Promise<Record<string, unknown>> {
  const plane = planeFor(token);
  const prices = engine.prices();
  const equity = plane.mark(prices);
  // fresh probe when the engine cache is empty (e.g. right after a reload)
  const info = engine.mt5Account ?? (await mt5AccountInfo());
  const acct = info?.account ?? null;
  const term = info?.terminal ?? null;
  const serverUp = term?.server_connected === true;
  return {
    status: serverUp ? "connected" : "reconnecting",
    symbol: "XAUUSD",
    symbols: MARKET_SPECS.map((s) => s.key),
    broker: {
      status: serverUp ? "linked" : "reconnecting",
      login: acct?.login ?? "414350770",
      login_masked: String(acct?.login ?? "414350770").replace(/^(\d{3})\d+(\d{2})$/, "$1••••$2"),
      server: acct?.server ?? "Exness-MT5Trial6",
      connected_at: Math.floor(engine.startedAt / 1000),
      account: {
        login: Number(acct?.login ?? 414350770),
        name: acct?.name ?? "bot",
        server: acct?.server ?? "Exness-MT5Trial6",
        broker: acct?.broker ?? "Exness Technologies Ltd",
        currency: acct?.currency ?? "USD",
        balance: acct ? Number(acct.balance ?? 0) : null,
        equity: acct ? Number(acct.equity ?? 0) : null,
        margin_free: acct ? Number(acct.margin_free ?? 0) : null,
        profit: acct ? Number(acct.profit ?? 0) : null,
        leverage: 500,
        type: acct?.type ?? "demo",
      },
    },
    account: {
      balance: acct ? Number(acct.balance ?? 0) : null,
      equity: acct ? Number(acct.equity ?? 0) : null,
      currency: acct?.currency ?? "USD",
      login: acct?.login ?? "414350770",
      server: acct?.server ?? "Exness-MT5Trial6",
      leverage: 500,
      practice_balance: Number(plane.balance.toFixed(2)),
      practice_equity: Number(equity.toFixed(2)),
    },
    broker_time_utc_offset: 0,
    engine_running: true,
    feed: feedStatus(),
  };
}

function autoTradeStatus(token: string): Record<string, unknown> {
  const plane = planeFor(token);
  const prices = engine.prices();
  const equity = plane.mark(prices);
  const markets: Record<string, { open: boolean; detail: string }> = {};
  for (const spec of MARKET_SPECS) {
    const tape = engine.tapes[spec.key];
    markets[spec.key] = {
      open: Boolean(tape?.live),
      detail: tape?.live ? "real feed streaming" : "market closed (exchange hours)",
    };
  }
  return {
    armed: plane.autoTrade,
    armed_at: null,
    armed_by: null,
    scope: "account",
    account: {
      mode: plane.mode,
      balance: Number(plane.balance.toFixed(2)),
      equity: Number(equity.toFixed(2)),
      currency: plane.currency,
    },
    terminal: null,
    markets,
    why: plane.autoTrade
      ? null
      : { code: "disarmed", text: "Arm the engine to let it place orders on confirmed signals." },
    risk: {
      risk_mode: plane.settings.risk_mode,
      risk_percent: plane.settings.risk_percent,
      fixed_lot: plane.settings.fixed_lot,
      max_positions: plane.settings.max_positions,
      daily_max_loss_pct: plane.settings.daily_max_loss_pct,
      max_spread_points: plane.settings.max_spread_points,
      rr: engine.config.rr,
      min_sl_atr: engine.config.sl_buffer_atr,
      timeframe: engine.config.timeframe,
    },
    pending_orders: [],
    pending_count: 0,
    last_skip_reason: null,
  };
}

function tradingStatus(token: string): Record<string, unknown> {
  const plane = planeFor(token);
  const prices = engine.prices();
  const equity = plane.mark(prices);
  return {
    connected: plane.connected,
    mode: plane.mode,
    status: "practice plane running",
    detail: "auto-provisioned practice plane ($10,000 paper account)",
    server: plane.server,
    login_masked: plane.loginMasked,
    auto_trade: plane.autoTrade,
    account: {
      balance: Number(plane.balance.toFixed(2)),
      equity: Number(equity.toFixed(2)),
      currency: plane.currency,
      login: 100042,
      server: plane.server,
      leverage: 500,
    },
    symbol: "XAUUSD",
    point_size: 0.01,
    stored: true,
  };
}

function tradingOrder(token: string, body: Record<string, unknown>): Record<string, unknown> {
  const plane = planeFor(token);
  const side = body.side === "SELL" ? "SELL" : "BUY";
  const volume = Number(body.volume ?? 0.01);
  const symbol = typeof body.symbol === "string" && MARKET_MAP[body.symbol] ? body.symbol : "XAUUSD";
  const t = engine.ticks[symbol];
  if (!t) return { ok: false, ticket: null, price: null, retcode: null, comment: "no price" };
  const price = side === "BUY" ? t.ask : t.bid;
  const sl = body.sl != null ? Number(body.sl) : null;
  const tp = body.tp != null ? Number(body.tp) : null;
  const pos = plane.openPosition(symbol, side, price, volume, sl, tp, null);
  return {
    ok: true, ticket: pos.ticket, price: Number(price.toFixed(MARKET_MAP[symbol].digits)),
    retcode: 10009, comment: `xauai-manual-${pos.ticket}`,
  };
}

/* ------------------------------------------------------------------- routes */

const routes: Record<string, Handler> = {
  "GET /api/health": (_req, res) => {
    json(res, 200, {
      status: "ok",
      version: "3.0.0",
      data_source: "metatrader5",
      requested_data_source: "metatrader5",
      detail: "MetaTrader 5 terminal (Exness-MT5Trial6 / 414350770) — every pair streams from the broker; no other source exists",
      ready: engine.ready,
      degraded: MARKET_SPECS.some((s) => !engine.tapes[s.key]),
      db: false,
      tps: Number((MARKET_SPECS.reduce((a, s) => a + engine.tickMeter[s.key].tps, 0)).toFixed(1)),
    });
  },

  "POST /api/auth/login": async (_req, res, _url, body) => {
    const email = typeof body?.email === "string" && body.email.includes("@") ? body.email : null;
    const password = typeof body?.password === "string" ? body.password : null;
    if (!email || !password || password.length < 4) {
      fail(res, 400, "Provide a valid email and a password of at least 4 characters.");
      return;
    }
    const { token, user } = mintToken(email);
    planeFor(token); // provision the practice plane immediately
    json(res, 200, {
      access_token: token,
      token_type: "bearer",
      expires_in: 86400,
      user: { id: user.id, email: user.email, role: user.role },
    });
  },

  "GET /api/me": (req, res) => {
    const user = userOf(bearer(req));
    if (!user) return fail(res, 401, "Not authenticated");
    json(res, 200, { id: user.id, email: user.email, display_name: user.email.split("@")[0], role: user.role });
  },

  "GET /api/candles": (req, res, url) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const tf = url.searchParams.get("tf") ?? "M1";
    const limit = Math.min(2000, Math.max(10, Number(url.searchParams.get("limit") ?? 500)));
    const symbol = url.searchParams.get("symbol") ?? "XAUUSD";
    const tape = engine.tapes[symbol];
    if (!tape || !TIMEFRAMES[tf]) return fail(res, 400, "unknown symbol or timeframe");
    const candles = tape.getClosed(tf, limit);
    json(res, 200, { symbol, tf, count: candles.length, candles });
  },

  "GET /api/signals": (req, res, url) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const limit = Math.min(300, Math.max(1, Number(url.searchParams.get("limit") ?? 100)));
    const status = url.searchParams.get("status");
    let list = engine.signals;
    if (status) list = list.filter((s) => s.status === status);
    const out = list.slice(0, limit).map((s) => ({
      id: s.id, ts: s.ts, symbol: s.symbol, tf: s.tf, direction: s.direction,
      entry: s.entry, sl: s.sl, tp: s.tp, rr: s.rr, confidence: s.confidence,
      trace: s.trace, status: s.status, result_r: s.result_r, closed_at: s.closed_at,
      entry_type: s.entry_type, market_ref: s.market_ref, entry_note: s.entry_note,
      filled_at: s.filled_at, context: s.context, close_reason: s.close_reason,
    }));
    json(res, 200, { count: out.length, signals: out });
  },

  "GET /api/stats": (req, res, url) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const days = Math.min(90, Math.max(1, Number(url.searchParams.get("days") ?? 30)));
    json(res, 200, engine.stats(days));
  },

  "GET /api/analysis": (req, res, url) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const symbol = url.searchParams.get("symbol") ?? "XAUUSD";
    if (!engine.tapes[symbol]) return fail(res, 400, "unknown symbol");
    json(res, 200, engine.analysis(symbol));
  },

  "GET /api/mt5/status": async (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, await mt5Status(token!));
  },

  "POST /api/mt5/connect": async (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, await mt5Status(token!));
  },

  "POST /api/mt5/disconnect": async (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, { ...(await mt5Status(token!)), status: "disconnected" });
  },

  "GET /api/mt5/auto-trade": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, autoTradeStatus(token!));
  },

  "POST /api/mt5/auto-trade": (req, res, _url, body) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const enabled = Boolean(body?.enabled);
    // D-042 — the money-management window in the UI IS the confirmation
    // gate (it replaced typing "ENABLE"); `confirm` stays accepted for
    // API compatibility but is no longer required.
    const plane = planeFor(token!);
    plane.autoTrade = enabled;
    engine.io?.emit("msg", {
      type: "mt5_auto", ts: new Date().toISOString(), level: enabled ? "info" : "warn",
      message: enabled ? "AI auto-trading ARMED — engine will execute confirmed signals" : "AI auto-trading disarmed",
      event: enabled ? "armed" : "disarmed", ok: true,
    });
    json(res, 200, { armed: enabled, terminal: null });
  },

  "GET /api/mt5/account": async (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const st = (await mt5Status(token!)) as { account: Record<string, unknown> };
    // REAL open positions from the Exness terminal
    const positions = await mt5OpenPositions();
    json(res, 200, { ok: true, account: st.account, positions, orders: [] });
  },

  "GET /api/mt5/positions": async (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    // REAL open positions from the user's Exness account (via the terminal)
    const real = await mt5OpenPositions();
    const plane = planeFor(token!);
    engine.prices();
    json(res, 200, {
      terminal_positions: real,
      positions: plane.positions.map((p) => ({
        ticket: p.ticket, symbol: p.symbol, side: p.side, volume: p.volume,
        price_open: p.price_open, sl: p.sl, tp: p.tp,
        profit: Number(p.profit.toFixed(2)), time: p.time,
      })),
      orders: [],
    });
  },

  "GET /api/mt5/history": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    json(res, 200, {
      positions: plane.trades.slice(-50).reverse().map((t) => ({
        ticket: t.ticket ?? 0, symbol: "XAUUSD", side: t.side, volume: t.volume,
        price_open: t.price_open, price_close: t.price_close ?? 0,
        profit: Number((t.profit ?? 0).toFixed(2)), time: t.opened_at, closed: t.closed_at,
      })),
    });
  },

  "GET /api/mt5/symbols": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    json(res, 200, {
      symbols: MARKET_SPECS.map((s) => ({
        name: s.key, digits: s.digits, point: Math.pow(10, -s.digits),
        contract_size: s.contractSize, volume_min: s.volumeMin,
        volume_max: 100, volume_step: s.volumeStep,
      })),
    });
  },

  "POST /api/mt5/order": (req, res, _url, body) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, tradingOrder(token!, body ?? {}));
  },

  "POST /api/mt5/close": (req, res, _url, body) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    const ticket = Number(body?.ticket ?? 0);
    const pos = plane.positions.find((p) => p.ticket === ticket);
    if (!pos) return fail(res, 404, "position not found");
    const t = engine.ticks[pos.symbol];
    const price = pos.side === "BUY" ? (t?.bid ?? pos.price_open) : (t?.ask ?? pos.price_open);
    const r = plane.closePosition(ticket, price);
    json(res, 200, { ok: true, ticket, price: Number(price.toFixed(2)), retcode: 10009, comment: "closed" });
  },

  "GET /api/trading/status": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, tradingStatus(token!));
  },

  "POST /api/trading/connect": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, tradingStatus(token!));
  },

  "POST /api/trading/disconnect": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, { connected: false });
  },

  "GET /api/trading/positions": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    engine.prices();
    json(res, 200, {
      positions: plane.positions.map((p) => ({
        ticket: p.ticket, symbol: p.symbol, side: p.side, volume: p.volume,
        price_open: p.price_open, sl: p.sl, tp: p.tp,
        profit: Number(p.profit.toFixed(2)), time: p.time,
      })),
      pending: [],
    });
  },

  "POST /api/trading/order": (req, res, _url, body) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    json(res, 200, tradingOrder(token!, body ?? {}));
  },

  "POST /api/trading/positions/0/close": (req, res) => {
    // dynamic path handled below — placeholder to satisfy the router map
    fail(res, 404, "not found");
  },

  "GET /api/trading/trades": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    const limit = Math.min(200, Math.max(1, Number(new URL(req.url ?? "/", `http://x`).searchParams.get("limit") ?? 100)));
    json(res, 200, { trades: plane.trades.slice(-limit).reverse() });
  },

  "POST /api/trading/auto-trade": (req, res, _url, body) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const enabled = Boolean(body?.enabled);
    const plane = planeFor(token!);
    plane.autoTrade = enabled;
    json(res, 200, { auto_trade: enabled });
  },

  "GET /api/trading/settings": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    // the frontend expects the UserSettings object DIRECTLY (not wrapped)
    json(res, 200, plane.settings);
  },

  "PUT /api/trading/settings": (req, res, _url, body) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    const patch = (body?.settings ?? body) as Record<string, unknown> | null;
    if (patch) {
      const merged = { ...plane.settings };
      for (const [k, v] of Object.entries(patch)) {
        if (k in DEFAULT_SETTINGS || k === "symbol_lots" || k === "daily_loss_usd" || k === "daily_profit_usd" || k === "day_start_balance") {
          (merged as Record<string, unknown>)[k] = v;
        }
      }
      plane.settings = merged as typeof plane.settings;
    }
    json(res, 200, { settings: plane.settings });
  },

  "POST /api/trading/reset": (req, res) => {
    const token = bearer(req);
    if (!userOf(token)) return fail(res, 401, "Not authenticated");
    const plane = planeFor(token!);
    plane.reset();
    json(res, 200, { balance: plane.balance, auto_trade: false });
  },

  "GET /api/positions": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    json(res, 200, { positions: [] });
  },

  "GET /api/config": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    json(res, 200, { config: engine.config, auto_trade: [...planes.values()].some((p) => p.autoTrade) });
  },

  "PUT /api/config": (req, res, _url, body) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const patch = (body?.config ?? body) as Record<string, unknown> | null;
    if (patch) {
      const merged = { ...engine.config } as Record<string, unknown>;
      for (const [k, v] of Object.entries(patch)) {
        if (k in engine.config) merged[k] = v;
      }
      engine.config = merged as unknown as typeof engine.config;
    }
    json(res, 200, { config: engine.config, auto_trade: [...planes.values()].some((p) => p.autoTrade) });
  },

  "POST /api/config/auto-trade": (req, res, _url, body) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const enabled = Boolean(body?.enabled);
    engine.autoTrade = enabled;
    json(res, 200, { auto_trade: enabled });
  },

  "GET /api/logs": (req, res, url) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const limit = Math.min(400, Math.max(1, Number(url.searchParams.get("limit") ?? 200)));
    const level = url.searchParams.get("level");
    let list = engine.logs;
    if (level) list = list.filter((l) => l.level === level);
    json(res, 200, { count: list.length, logs: list.slice(0, limit) });
  },

  "GET /api/logs/export.csv": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const rows = [["ts", "level", "source", "message"].join(",")];
    for (const l of engine.logs) {
      rows.push([l.ts ?? "", l.level, l.source, `"${l.message.replace(/"/g, '""')}"`].join(","));
    }
    const csv = rows.join("\n");
    res.writeHead(200, { "Content-Type": "text/csv", "Access-Control-Allow-Origin": "*" });
    res.end(csv);
  },

  "GET /api/market/external": async (_req, res) => {
    if (!userOf(bearer(_req))) return fail(res, 401, "Not authenticated");
    // REAL market references — 100% from the MetaTrader 5 terminal:
    //   gold_reference : XAUUSDm 24h stats from broker M1/D1 history
    //   eur_usd        : EURUSDm live broker quote
    //   usd_strength   : DXY (real ICE formula) from MT5 FX legs
    // Cached 60s. NO Binance, NO Yahoo, NO ECB — MT5 or "unavailable".
    const now = Date.now();
    if (!externalCache || now - externalCache.at > 60_000) {
      // one Market Watch round trip for all quotes
      const quotes = await mt5Quotes();
      const mid = (sym: string): number | null => {
        const q = quotes[sym];
        return q ? (q.bid + q.ask) / 2 : null;
      };

      // ---- gold 24h stats from REAL broker history (XAUUSDm) ----
      let gold: Record<string, unknown>;
      try {
        const nowSec = Math.floor(now / 1000);
        const d1 = await mt5History("XAUUSDm", "D1", nowSec - 10 * 86_400, nowSec + 86_400, 20);
        const m1 = await mt5History("XAUUSDm", "M1", nowSec - 86_400, nowSec + 60, 2_000);
        const last = m1.length ? m1[m1.length - 1].c : (d1.length ? d1[d1.length - 1].c : null);
        const prevClose = d1.length >= 2 ? d1[d1.length - 2].c : null;
        let hi = -Infinity;
        let lo = Infinity;
        let vol = 0;
        for (const b of m1) { hi = Math.max(hi, b.h); lo = Math.min(lo, b.l); vol += b.v; }
        gold = {
          ok: last != null,
          provider: "mt5:Exness/XAUUSDm",
          symbol: "XAUUSD",
          price: last,
          change_24h_pct: last != null && prevClose ? Number((((last - prevClose) / prevClose) * 100).toFixed(2)) : undefined,
          high_24h: Number.isFinite(hi) ? hi : undefined,
          low_24h: Number.isFinite(lo) ? lo : undefined,
          volume_24h: vol || undefined,
          quote_volume_24h: undefined,
          source_market: quotes.XAUUSDm
            ? (Date.now() - quotes.XAUUSDm.ts < 120_000 ? "open" : "closed (weekend)")
            : "unknown",
        };
      } catch (err) {
        gold = { ok: false, provider: "mt5:Exness/XAUUSDm", error: err instanceof Error ? err.message : "unavailable" };
      }

      // ---- EUR/USD straight from the terminal ----
      const eurMid = mid("EURUSDm");
      const eur: Record<string, unknown> = eurMid != null
        ? { ok: true, provider: "mt5:Exness/EURUSDm", rate: Number(eurMid.toFixed(4)) }
        : { ok: false, provider: "mt5:Exness/EURUSDm", error: "symbol unavailable in Market Watch" };

      // ---- DXY (ICE formula) from MT5 FX legs ----
      const eurUsd = mid("EURUSDm");
      const usdJpy = mid("USDJPYm");
      const gbpUsd = mid("GBPUSDm");
      const usdCad = mid("USDCADm");
      const usdSek = mid("USDSEKm");
      const usdChf = mid("USDCHFm");
      const legsReady = [eurUsd, usdJpy, gbpUsd, usdCad, usdSek, usdChf].every((v) => v != null && v > 0);
      const usd: Record<string, unknown> = legsReady
        ? {
            ok: true,
            provider: "mt5:Exness (6 FX legs, live broker quotes)",
            name: "US Dollar Index (DXY, real ICE formula on MT5 legs)",
            value: Number((50.14348112 *
              Math.pow(eurUsd as number, -0.576) *
              Math.pow(usdJpy as number, 0.136) *
              Math.pow(gbpUsd as number, -0.119) *
              Math.pow(usdCad as number, 0.091) *
              Math.pow(usdSek as number, 0.042) *
              Math.pow(usdChf as number, 0.036)).toFixed(2)),
            legs: {
              EURUSD: Number((eurUsd as number).toFixed(4)),
              USDJPY: Number((usdJpy as number).toFixed(2)),
              GBPUSD: Number((gbpUsd as number).toFixed(4)),
              USDCAD: Number((usdCad as number).toFixed(4)),
              USDSEK: Number((usdSek as number).toFixed(2)),
              USDCHF: Number((usdChf as number).toFixed(4)),
            },
          }
        : { ok: false, provider: "mt5:Exness", error: "DXY legs unavailable in Market Watch" };

      externalCache = { at: now, payload: { ts: new Date().toISOString(), gold_reference: gold, eur_usd: eur, usd_strength: usd } };
    }
    json(res, 200, externalCache.payload);
  },

  "GET /api/mt5/bridge": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const health = sourceHealth()["mt5:terminal"];
    json(res, 200, {
      url: "127.0.0.1:22346 (local MetaTrader 5 terminal, MCP)",
      source: "default",
      available: health?.ok === true,
    });
  },

  "PUT /api/mt5/bridge": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    const health = sourceHealth()["mt5:terminal"];
    json(res, 200, {
      url: "127.0.0.1:22346 (local MetaTrader 5 terminal, MCP)",
      source: "custom",
      updated: true,
      available: health?.ok === true,
      note: "the bridge is the local terminal itself — always in use",
    });
  },

  "DELETE /api/mt5/bridge": (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    json(res, 200, { url: "127.0.0.1:22346 (local MetaTrader 5 terminal, MCP)", source: "default" });
  },

  "POST /api/mt5/bridge/diagnose": async (req, res) => {
    if (!userOf(bearer(req))) return fail(res, 401, "Not authenticated");
    // LIVE probes — a diagnose must test right now, not read stale cache
    const t0 = Date.now();
    const info = await mt5AccountInfo();
    const termLatency = Date.now() - t0;
    const quotes = await mt5Quotes();
    const watchOk = Object.keys(quotes).length > 0;
    const acct = info?.account ?? null;
    const term = info?.terminal ?? null;
    const stages = [
      {
        stage: "terminal",
        ok: info != null,
        detail: info != null
          ? `MetaTrader 5 terminal reachable (build ${term?.build ?? "?"}, latency ${termLatency}ms)`
          : "terminal unreachable: no response from the MCP bridge (:22346)",
        ms: termLatency,
        hint: null,
      },
      {
        stage: "account",
        ok: term?.server_connected === true,
        detail: term?.server_connected === true
          ? `Exness server link UP — ${acct?.server ?? "Exness-MT5Trial6"} (login ${acct?.login ?? "414350770"}, balance ${acct?.balance ?? "?"} ${acct?.currency ?? "USD"})`
          : "Exness server link down",
        ms: 0,
        hint: null,
      },
      {
        stage: "market-feed",
        ok: watchOk,
        detail: watchOk
          ? `Market Watch serving ${Object.keys(quotes).length} symbols — every pair streams from the broker`
          : "feed unavailable: Market Watch returned no quotes",
        ms: Date.now() - t0 - termLatency,
        hint: null,
      },
    ];
    json(res, 200, {
      ok: stages.every((s) => s.ok),
      stages,
      verdict: stages.every((s) => s.ok)
        ? "online — the REAL MetaTrader 5 terminal is the sole data source"
        : "degraded — terminal unreachable (prices go stale; never fabricated)",
    });
  },
};

/* ------------------------------------------------------------------- server */

const httpServer = createServer(async (req, res) => {
  try {
    // CORS preflight
    if (req.method === "OPTIONS") {
      res.writeHead(204, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
      });
      res.end();
      return;
    }
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
    const path = url.pathname;

    // dynamic route: POST /api/trading/positions/:ticket/close
    const closeMatch = path.match(/^\/api\/trading\/positions\/(\d+)\/close$/);
    if (closeMatch && req.method === "POST") {
      const token = bearer(req);
      if (!userOf(token)) {
        json(res, 401, { detail: "Not authenticated" });
        return;
      }
      const plane = planeFor(token!);
      const ticket = Number(closeMatch[1]);
      const pos = plane.positions.find((p) => p.ticket === ticket);
      if (!pos) {
        json(res, 404, { detail: "position not found" });
        return;
      }
      const t = engine.ticks[pos.symbol];
      const price = pos.side === "BUY" ? (t?.bid ?? pos.price_open) : (t?.ask ?? pos.price_open);
      plane.closePosition(ticket, price);
      json(res, 200, { ok: true, ticket, price: Number(price.toFixed(2)), retcode: 10009, comment: "closed" });
      return;
    }

    const key = `${req.method} ${path}`;
    const handler = routes[key];
    if (handler) {
      const body = req.method === "POST" || req.method === "PUT" ? await readBody(req) : null;
      await handler(req, res, url, body);
      return;
    }
    fail(res, 404, "not found");
  } catch (err) {
    fail(res, 500, err instanceof Error ? err.message : "internal error");
  }
});

const io = new Server(httpServer, {
  // Default engine.io path ("/socket.io") — the browser client connects with
  // io("/?XTransformPort=3003"): the URL path "/" is the NAMESPACE and the
  // XTransformPort query param is what Caddy routes on. Keeping the default
  // transport path lets /api/* REST routes pass through untouched.
  cors: { origin: "*", methods: ["GET", "POST"] },
  pingTimeout: 60000,
  pingInterval: 25000,
});

io.on("connection", (socket) => {
  const token = (socket.handshake.auth as { token?: string }).token ?? null;
  if (token) (socket.data as { token?: string }).token = token;
  if (token && users.has(token)) planeFor(token);

  socket.on("subscribe", (data: { channel?: string; symbol?: string; tf?: string }) => {
    (socket.data as { sub?: { symbol: string; tf: string } }).sub = {
      symbol: data?.symbol ?? "XAUUSD",
      tf: data?.tf ?? "M1",
    };
    socket.emit("msg", {
      type: "subscribed", symbol: data?.symbol ?? "XAUUSD", tf: data?.tf ?? "M1",
      ts: Math.floor(Date.now() / 1000),
    });
  });

  socket.on("unsubscribe", () => {
    (socket.data as { sub?: unknown }).sub = undefined;
    socket.emit("msg", { type: "unsubscribed", ts: Math.floor(Date.now() / 1000) });
  });

  socket.on("ping", () => {
    socket.emit("msg", { type: "heartbeat", ts: Math.floor(Date.now() / 1000) });
  });
});

// heartbeat broadcast every 15s (stale-socket detection on the client)
setInterval(() => {
  io.emit("msg", { type: "heartbeat", ts: Math.floor(Date.now() / 1000) });
}, 15000);

/** Cache for the real /api/market/external references (60s TTL). */
let externalCache: { at: number; payload: Record<string, unknown> } | null = null;

// mt5 status broadcast every 10s — REAL terminal state
setInterval(() => {
  io.emit("msg", {
    type: "mt5_status", symbol: "XAUUSD",
    status: engine.mt5Account?.terminal?.server_connected === true ? "connected" : "reconnecting",
    symbols: MARKET_SPECS.map((s) => s.key), feed: feedStatus(),
  });
}, 10000);

engine.start(io);

httpServer.listen(PORT, () => {
  console.log(`[market-service] listening on :${PORT} (HTTP /api/* + socket.io path "/")`);
  console.log(`[market-service] DATA SOURCE: MetaTrader 5 terminal — Exness-MT5Trial6 / login 414350770 (MCP :22346). No other source exists.`);
});
