/**
 * Practice trading plane — the port of the Python OrderExecutor §9 +
 * per-user demo plane: a $10,000 paper account priced off the SAME live
 * market feed. Lot sizing: lots = risk_usd / (sl_dist × contract_size),
 * floored to volume_step, clamped to [volume_min, 100].
 */

import { MARKET_MAP } from "./tape";

export interface PlanePosition {
  ticket: number;
  symbol: string;
  side: "BUY" | "SELL";
  volume: number;
  price_open: number;
  sl: number | null;
  tp: number | null;
  profit: number;
  time: string;
  signal_id: string | null;
}

export interface PlaneTrade {
  signal_id: string | null;
  owner: string | null;
  ticket: number | null;
  side: string;
  volume: number;
  price_open: number;
  sl: number | null;
  tp: number | null;
  price_close: number | null;
  profit: number | null;
  opened_at: string | null;
  closed_at: string | null;
  signal_direction?: string | null;
  signal_status?: string | null;
}

export interface UserSettings {
  risk_mode: "percent" | "fixed";
  risk_percent: number;
  fixed_lot: number;
  max_positions: number;
  daily_max_loss_pct: number;
  max_trades_per_day: number;
  rr: number;
  min_sl_atr: number;
  max_spread_points: number;
  daily_loss_usd: number;
  daily_profit_usd: number;
  day_start_balance: number;
  symbol_lots: Record<string, number>;
  balance: number;
  currency: string;
}

export const DEFAULT_SETTINGS: UserSettings = {
  risk_mode: "percent",
  risk_percent: 1.0,
  fixed_lot: 0.05,
  max_positions: 3,
  daily_max_loss_pct: 3.0,
  max_trades_per_day: 20,
  rr: 2,
  min_sl_atr: 0.5,
  max_spread_points: 50,
  daily_loss_usd: 0,
  daily_profit_usd: 0,
  day_start_balance: 10_000,
  symbol_lots: {},
  balance: 10_000,
  currency: "USD",
};

export class Plane {
  balance = 10_000.0;
  currency = "USD";
  autoTrade = false;
  connected = true;
  mode: "practice" = "practice";
  server = "GoldAI-Practice";
  loginMasked = "100••••42";
  positions: PlanePosition[] = [];
  trades: PlaneTrade[] = [];
  settings: UserSettings = { ...DEFAULT_SETTINGS };
  nextTicket = 100_000;
  dayTrades = 0;
  owner: string;

  constructor(owner: string) {
    this.owner = owner;
    this.settings.day_start_balance = this.balance;
  }

  /** Current price map: symbol → mid price. */
  mark(prices: Record<string, number>): number {
    let equity = this.balance;
    for (const p of this.positions) {
      const price = prices[p.symbol];
      if (price === undefined) continue;
      const spec = MARKET_MAP[p.symbol];
      const dir = p.side === "BUY" ? 1 : -1;
      p.profit = (price - p.price_open) * dir * (spec?.contractSize ?? 100) * p.volume;
      equity += p.profit;
    }
    return equity;
  }

  private lots(symbol: string, slDist: number): number {
    const spec = MARKET_MAP[symbol];
    const s = this.settings;
    if (s.risk_mode === "fixed") {
      const perSymbol = s.symbol_lots[symbol];
      return Math.max(spec?.volumeMin ?? 0.01, perSymbol ?? s.fixed_lot);
    }
    const riskUsd = (this.balance * s.risk_percent) / 100;
    const contract = spec?.contractSize ?? 100;
    if (slDist <= 0) return spec?.volumeMin ?? 0.01;
    const raw = riskUsd / (slDist * contract);
    const step = spec?.volumeStep ?? 0.01;
    const lots = Math.floor(raw / step) * step;
    return Math.max(spec?.volumeMin ?? 0.01, Math.min(lots, 100));
  }

  canOpen(): { ok: boolean; reason?: string } {
    if (this.positions.length >= this.settings.max_positions) {
      return { ok: false, reason: `max positions reached (${this.settings.max_positions})` };
    }
    if (this.dayTrades >= this.settings.max_trades_per_day) {
      return { ok: false, reason: "daily trade budget exhausted" };
    }
    const loss = this.settings.day_start_balance - this.balance;
    if (this.settings.daily_loss_usd > 0 && loss >= this.settings.daily_loss_usd) {
      return { ok: false, reason: "daily stop-loss hit" };
    }
    if (this.settings.daily_max_loss_pct > 0) {
      const lossPct = (loss / this.settings.day_start_balance) * 100;
      if (lossPct >= this.settings.daily_max_loss_pct) {
        return { ok: false, reason: `daily loss limit ${this.settings.daily_max_loss_pct}% hit` };
      }
    }
    return { ok: true };
  }

  openPosition(
    symbol: string,
    side: "BUY" | "SELL",
    price: number,
    volume: number | null,
    sl: number | null,
    tp: number | null,
    signalId: string | null,
  ): PlanePosition {
    const spec = MARKET_MAP[symbol];
    const slDist = sl !== null ? Math.abs(price - sl) : Math.max(price * 0.002, (spec ? spec.spread : 1) * 3);
    const vol = volume ?? this.lots(symbol, slDist);
    const pos: PlanePosition = {
      ticket: this.nextTicket++,
      symbol, side, volume: vol, price_open: price, sl, tp,
      profit: 0,
      time: new Date().toISOString(),
      signal_id: signalId,
    };
    this.positions.push(pos);
    this.dayTrades += 1;
    this.trades.push({
      signal_id: signalId, owner: this.owner, ticket: pos.ticket, side, volume: vol,
      price_open: price, sl, tp, price_close: null, profit: null,
      opened_at: pos.time, closed_at: null,
    });
    return pos;
  }

  closePosition(ticket: number, price: number): { ok: boolean; profit: number; position?: PlanePosition } {
    const idx = this.positions.findIndex((p) => p.ticket === ticket);
    if (idx === -1) return { ok: false, profit: 0 };
    const pos = this.positions[idx];
    const spec = MARKET_MAP[pos.symbol];
    const dir = pos.side === "BUY" ? 1 : -1;
    const profit = (price - pos.price_open) * dir * (spec?.contractSize ?? 100) * pos.volume;
    this.balance += profit;
    this.positions.splice(idx, 1);
    const rec = this.trades.find((t) => t.ticket === pos.ticket);
    if (rec) {
      rec.price_close = price;
      rec.profit = profit;
      rec.closed_at = new Date().toISOString();
    }
    return { ok: true, profit, position: pos };
  }

  /** Risk math for auto-execution of a signal. */
  signalLots(symbol: string, entry: number, sl: number): number {
    return this.lots(symbol, Math.abs(entry - sl));
  }

  reset(): void {
    this.balance = 10_000;
    this.positions = [];
    this.trades = [];
    this.autoTrade = false;
    this.dayTrades = 0;
    this.settings = { ...DEFAULT_SETTINGS };
  }
}

/** token → plane */
export const planes = new Map<string, Plane>();

export function planeFor(token: string): Plane {
  let p = planes.get(token);
  if (!p) {
    p = new Plane(token);
    planes.set(token, p);
  }
  return p;
}
