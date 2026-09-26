/**
 * D-076 — the platform's markets: ONE shared constant for every UI surface.
 *
 * User directive (Bengali, verbatim): "আর সব পেয়ার গুলো একটি ড্রপ ডাউন
 * বক্সে থাকবে, যেনো পরিবর্তন করলে সহজ হয়, এতে করে জায়গা বাঁচবে" — every
 * pair lives in ONE dropdown so switching is easy and space is saved.
 *
 * The backend mirrors this list in `app/mt5/base.py: MARKETS` — the two
 * lists MUST stay in sync (broker spellings there, market keys here).
 */

export interface MarketMeta {
  /** market key — the backend's signal/feed identity (never broker-suffixed) */
  key: string;
  /** short badge label (PriceHero / dropdown rows) */
  label: string;
  /** full name for descriptions */
  name: string;
  /** display decimals for price labels */
  digits: number;
}

export const MARKETS: MarketMeta[] = [
  { key: "XAUUSD", label: "XAU / Gold", name: "Gold vs US Dollar", digits: 2 },
  { key: "BTCUSD", label: "BTC / Bitcoin", name: "Bitcoin vs US Dollar", digits: 2 },
  { key: "USOIL", label: "WTI / Crude Oil", name: "US Crude Oil (WTI)", digits: 2 },
  { key: "USTEC", label: "US100 / Tech 100", name: "US Tech 100 Index", digits: 1 },
];

/** Every market key (the dropdown's source of truth). */
export const MARKET_KEYS = MARKETS.map((m) => m.key);

export function marketMeta(symbol: string | null | undefined): MarketMeta {
  const mk = marketKey(symbol || "");
  return MARKETS.find((m) => m.key === mk) ?? MARKETS[0];
}

/**
 * D-076 — the frontend mirror of the backend's market_key (base.py).
 *
 * Broker-suffixed spellings (XAUUSDm, BTCUSDm, USOILm, USTECmicro, …) must
 * normalize to the market key so per-symbol filters ALWAYS match. The old
 * suffix-strip required ≥6 base chars, which silently broke the new 5-char
 * bases (USOILm -> "USOILM" matched nothing) — known-market PREFIX matching
 * (longest base first) fixes it; unknown symbols keep the legacy rule.
 */
const KNOWN = [...MARKET_KEYS].sort((a, b) => b.length - a.length);

export function marketKey(symbol: string | null | undefined): string {
  let s = (symbol || "").trim().toUpperCase();
  if (!s) return "";
  s = s.split(".")[0];
  for (const m of KNOWN) {
    if (s.startsWith(m)) return m;
  }
  for (const suffix of ["MICRO", "PRO", "M"]) {
    if (s.endsWith(suffix) && s.length - suffix.length >= 6) {
      return s.slice(0, s.length - suffix.length);
    }
  }
  return s;
}

/** Union of the backend-provided symbols with the full market list (deduped,
 *  backend order first so live markets lead when the feed is partial). */
export function allSymbols(backend: string[] | null | undefined): string[] {
  const live = (backend || []).filter((s) => s && s.trim());
  const merged = [...new Set([...live, ...MARKET_KEYS])];
  // stable, market-list order for anything the backend knows
  const order = new Map(MARKET_KEYS.map((k, i) => [k, i]));
  return merged.sort(
    (a, b) => (order.get(marketKey(a)) ?? 99) - (order.get(marketKey(b)) ?? 99)
  );
}
