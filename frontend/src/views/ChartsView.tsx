/**
 * ChartsView (D-041) — the chart tab: pair + timeframe pills, the live chart,
 * and the SIGNAL ANALYSIS panel (when each signal fired, its entries, and the
 * exact factors the AI verified — the user's requested panel).
 */

import { useEffect, useMemo, useState } from "react";
import { useTick } from "../state/feed";
import {
  TIMEFRAMES,
  type Candle,
  type Mt5Status,
  type Signal,
  type Timeframe,
} from "../types";
import ErrorBoundary from "../components/ErrorBoundary";
import PriceChart from "../components/PriceChart";
import { SignalDetail, SignalRow, fmtTime } from "../components/SignalDetail";
import { Badge, Card, Dot, EmptyState, SectionTitle } from "../components/ui";

interface Props {
  symbol: string;
  symbols: string[];
  onSymbolChange: (s: string) => void;
  tf: Timeframe;
  onTfChange: (tf: Timeframe) => void;
  candles: Candle[];
  candlesLoading: boolean;
  signals: Signal[];
  mt5: Mt5Status | null;
  wsState: "connecting" | "open" | "closed";
  onDesync: () => void;
  focusSignalId: string | null;
  onFocusSignalConsumed: () => void;
}

export default function ChartsView({
  symbol,
  symbols,
  onSymbolChange,
  tf,
  onTfChange,
  candles,
  candlesLoading,
  signals,
  mt5,
  wsState,
  onDesync,
  focusSignalId,
  onFocusSignalConsumed,
}: Props) {
  const tick = useTick(symbol);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const symbolSignals = useMemo(
    () => signals.filter((s) => s.symbol === symbol),
    [signals, symbol],
  );
  const chartSignals = useMemo(
    () => symbolSignals.slice(0, 40),
    [symbolSignals],
  );

  // follow "open analysis" deep-links from other tabs
  useEffect(() => {
    if (focusSignalId) {
      setSelectedId(focusSignalId);
      onFocusSignalConsumed();
    }
  }, [focusSignalId, onFocusSignalConsumed]);

  // default selection: newest signal
  const selected = useMemo(
    () =>
      symbolSignals.find((s) => s.id === selectedId) ??
      symbolSignals[0] ??
      null,
    [symbolSignals, selectedId],
  );

  const market = mt5?.feed?.symbols?.[symbol]?.market ?? "unknown";
  const feedProvider = mt5?.feed?.symbols?.[symbol];

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {/* header: pair + tf + live quote */}
      <Card padded={false} className="p-3">
        <div className="flex min-w-0 flex-col gap-2.5">
          <div className="flex min-w-0 items-center gap-2">
            {symbols.length > 1 && (
              <div className="flex min-w-0 gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                {symbols.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => onSymbolChange(s)}
                    className={`shrink-0 rounded-full border px-3.5 py-1.5 text-xs font-bold transition-colors ${
                      s === symbol
                        ? "border-gold/60 bg-gold/15 text-gold"
                        : "border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200"
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
            <div className="ml-auto flex shrink-0 items-center gap-2">
              {tick ? (
                <span className="font-mono text-sm font-bold tabular-nums text-zinc-100">
                  {tick.bid.toFixed(2)}
                </span>
              ) : (
                <span className="h-4 w-14 animate-pulse rounded bg-zinc-800" />
              )}
              <span
                className={`flex items-center gap-1 text-[10px] font-semibold ${
                  wsState === "open"
                    ? "text-emerald-400"
                    : wsState === "connecting"
                      ? "text-amber-400"
                      : "text-red-400"
                }`}
              >
                <Dot tone={wsState === "open" ? "green" : wsState === "connecting" ? "amber" : "red"} />
                {wsState === "open" ? "live" : wsState === "connecting" ? "connecting" : "offline"}
              </span>
            </div>
          </div>
          <div className="flex min-w-0 gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            {TIMEFRAMES.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => onTfChange(t)}
                className={`shrink-0 rounded-lg border px-3 py-1 font-mono text-[11px] font-bold transition-colors ${
                  t === tf
                    ? "border-gold/60 bg-gold/15 text-gold"
                    : "border-zinc-800 bg-zinc-900/60 text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {t}
              </button>
            ))}
            {feedProvider?.mt5 && (
              <span className="ml-auto shrink-0 self-center text-[10px] text-zinc-600">
                MT5 terminal · {feedProvider.provider ?? ""}
              </span>
            )}
          </div>
        </div>
      </Card>

      {/* chart */}
      <div className="h-[52vh] min-h-[320px] w-full min-w-0 sm:h-[56vh] lg:h-[60vh]">
        <ErrorBoundary label="Chart">
          <PriceChart
            symbol={symbol}
            tf={tf}
            candles={candles}
            candlesLoading={candlesLoading}
            signals={chartSignals}
            selectedSignal={selected?.status === "active" ? selected : null}
            market={market}
            wsConnected={wsState === "open"}
            onDesync={onDesync}
          />
        </ErrorBoundary>
      </div>

      {/* signal analysis panel */}
      <Card>
        <SectionTitle
          title="Signal Analysis"
          right={
            <Badge tone="zinc">
              {symbolSignals.length} signal{symbolSignals.length === 1 ? "" : "s"}
            </Badge>
          }
        />
        {symbolSignals.length === 0 ? (
          <EmptyState
            title="No signals yet for this pair"
            hint="The engine analyzes every M1 close (H1 trend + M5/M15 confirmation + pattern trigger). The first confirmed signal appears here with its full analysis."
          />
        ) : (
          <div className="flex min-w-0 flex-col gap-3">
            {/* recent signals list */}
            <div className="flex max-h-64 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
              {symbolSignals.slice(0, 30).map((s) => (
                <SignalRow
                  key={s.id}
                  signal={s}
                  selected={selected?.id === s.id}
                  onSelect={() => setSelectedId(s.id)}
                />
              ))}
            </div>
            {selected && (
              <div className="border-t border-zinc-800/70 pt-3">
                <SignalDetail signal={selected} />
              </div>
            )}
          </div>
        )}
      </Card>

      {/* engine activity (recent log lines) */}
      {selected && (
        <p className="px-1 text-center text-[10px] text-zinc-600">
          Signal times are UTC · entry {selected.entry.toFixed(2)} · taken {fmtTime(selected.ts)}
        </p>
      )}
    </div>
  );
}
