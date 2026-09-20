import { useQuery } from "@tanstack/react-query";
import { getExternalSnapshot } from "../lib/api";

interface MarketDataPanelProps {
  token: string;
}

/**
 * MarketDataPanel — data-transparency view (user req #5): what data the
 * platform serves, where each stream comes from, plus free external
 * references (Binance PAXG gold, ECB FX) for cross-checking MT5 pricing.
 */
export default function MarketDataPanel({ token }: MarketDataPanelProps) {
  const ext = useQuery({
    queryKey: ["external"],
    queryFn: () => getExternalSnapshot(token),
    refetchInterval: 60_000,
    enabled: !!token,
  });

  const snap = ext.data;

  return (
    <div className="space-y-4">
      {/* internal feed */}
      <div>
        <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-gold/70">
          Platform feed
        </h3>
        <div className="space-y-1.5 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-[11px] text-zinc-400">
          <p className="flex justify-between">
            <span>candles / ticks</span>
            <b className="text-zinc-200">MetaTrader 5 · Exness (via backend)</b>
          </p>
          <p className="flex justify-between">
            <span>AI signals</span>
            <b className="text-zinc-200">SFP engine · M15 + H1 context</b>
          </p>
          <p className="flex justify-between">
            <span>news filter</span>
            <b className="text-zinc-200">FMP economic calendar (high-impact USD)</b>
          </p>
          <p className="flex justify-between">
            <span>transport</span>
            <b className="text-zinc-200">REST backfill + WebSocket live</b>
          </p>
        </div>
      </div>

      {/* external references */}
      <div>
        <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-gold/70">
          External references · free APIs
        </h3>
        <div className="grid grid-cols-1 gap-2">
          {/* gold reference */}
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
            <div className="flex items-baseline justify-between">
              <span className="text-[11px] text-zinc-500">
                Gold spot reference · {snap?.gold_reference.provider ?? "binance"}
              </span>
              {snap?.gold_reference.ok ? (
                <span className="font-mono text-sm font-semibold text-zinc-100">
                  {snap.gold_reference.price?.toFixed(2)}{" "}
                  <span className={`text-[11px] ${(snap.gold_reference.change_24h_pct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    {(snap.gold_reference.change_24h_pct ?? 0) >= 0 ? "+" : ""}
                    {snap.gold_reference.change_24h_pct?.toFixed(2)}%
                  </span>
                </span>
              ) : (
                <span className="text-[11px] text-zinc-600">unavailable</span>
              )}
            </div>
            {snap?.gold_reference.ok && (
              <p className="mt-1 font-mono text-[10px] text-zinc-600">
                24h {snap.gold_reference.low_24h?.toFixed(2)} — {snap.gold_reference.high_24h?.toFixed(2)} · PAXG = 1 oz fine gold
              </p>
            )}
          </div>

          {/* FX */}
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
              <span className="text-[11px] text-zinc-500">EUR/USD · ECB</span>
              {snap?.eur_usd.ok ? (
                <p className="font-mono text-sm font-semibold text-zinc-100">
                  {snap.eur_usd.rate?.toFixed(4)}
                </p>
              ) : (
                <p className="text-[11px] text-zinc-600">unavailable</p>
              )}
            </div>
            <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
              <span className="text-[11px] text-zinc-500">USD strength (6-FX mean)</span>
              {snap?.usd_strength.ok ? (
                <p className="font-mono text-sm font-semibold text-zinc-100">
                  {snap.usd_strength.value?.toFixed(3)}
                </p>
              ) : (
                <p className="text-[11px] text-zinc-600">unavailable</p>
              )}
            </div>
          </div>

          <p className="text-[10px] leading-relaxed text-zinc-600">
            External references come from free, key-less public APIs (Binance PAXG,
            Frankfurter/ECB). They are for cross-checking only — trading prices
            always come from your broker's MT5 feed.
          </p>
        </div>
      </div>
    </div>
  );
}
