import { useEffect, useState } from "react";
import { getConfig, putConfig, postAutoTrade, ApiError } from "../lib/api";
import type { EngineConfig } from "../types";

/**
 * SettingsDialog (SPEC §10, Phase 3): admin edits the validated engine_config
 * and toggles auto-trade with a typed ENABLE confirmation (SPEC §7.1).
 * Auto-trade is OFF by default and Phase 4 implements execution — enabling it
 * now only stores the flag.
 */

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  token: string;
  isAdmin: boolean;
  onSaved?: (autoTrade: boolean) => void;
}

const inputCls =
  "w-full rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-zinc-100 focus:border-gold/60 focus:outline-none";

function NumField({
  label, value, onChange, step = "any", min,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  step?: string;
  min?: number;
}) {
  return (
    <label className="block text-xs text-zinc-400">
      {label}
      <input
        type="number"
        className={`${inputCls} mt-1`}
        value={value}
        step={step}
        min={min}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

export default function SettingsDialog({
  open, onClose, token, isAdmin, onSaved,
}: SettingsDialogProps) {
  const [cfg, setCfg] = useState<EngineConfig | null>(null);
  const [autoTrade, setAutoTrade] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setSaved(false);
    getConfig(token)
      .then((res) => {
        setCfg(res.config);
        setAutoTrade(res.auto_trade);
      })
      .catch((exc) => setError(exc instanceof ApiError ? exc.message : "load failed"));
  }, [open, token]);

  if (!open) return null;

  const patch = (partial: Partial<EngineConfig>) =>
    setCfg((prev) => (prev ? { ...prev, ...partial } : prev));

  const save = async () => {
    if (!cfg) return;
    setBusy(true);
    setError(null);
    try {
      await putConfig(token, cfg);
      setSaved(true);
      onSaved?.(autoTrade);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (exc) {
      setError(
        exc instanceof ApiError
          ? exc.message
          : "save failed (validation error — check values)"
      );
    } finally {
      setBusy(false);
    }
  };

  const toggleAutoTrade = async (enable: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const res = await postAutoTrade(token, enable, enable ? confirmText : undefined);
      setAutoTrade(res.auto_trade);
      setConfirmText("");
      onSaved?.(res.auto_trade);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "update failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Engine settings"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-base font-semibold text-zinc-100">
              Engine <span className="text-gold">settings</span>
            </h2>
            <p className="mt-0.5 text-xs text-zinc-500">
              validated against the backend schema — invalid values are rejected
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-800 px-2 py-0.5 text-xs text-zinc-400 hover:text-zinc-200"
          >
            ✕
          </button>
        </div>

        {!isAdmin && (
          <p className="mb-3 rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-xs text-gold/90">
            read-only — admin role required to change the engine config
          </p>
        )}

        {cfg ? (
          <div className="space-y-4">
            <fieldset className="rounded-lg border border-zinc-800 p-3">
              <legend className="px-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                strategy
              </legend>
              <div className="grid grid-cols-2 gap-3">
                <NumField label="risk:reward" value={cfg.rr} onChange={(v) => patch({ rr: v })} step="0.1" />
                <NumField label="SL buffer × ATR" value={cfg.sl_buffer_atr} onChange={(v) => patch({ sl_buffer_atr: v })} step="0.05" />
                <NumField label="SFP lookback (bars)" value={cfg.sfp_lookback} onChange={(v) => patch({ sfp_lookback: v })} min={2} />
                <NumField label="wick ≥ × ATR" value={cfg.sfp_wick_atr_ratio} onChange={(v) => patch({ sfp_wick_atr_ratio: v })} step="0.05" />
                <NumField label="min ATR (USD)" value={cfg.min_atr} onChange={(v) => patch({ min_atr: v })} step="0.1" />
                <NumField label="RSI buy min/max" value={cfg.rsi_buy_min} onChange={(v) => patch({ rsi_buy_min: v })} />
                <NumField label="expiry (bars)" value={cfg.expiry_bars} onChange={(v) => patch({ expiry_bars: v })} min={1} />
                <NumField label="cooldown (bars)" value={cfg.cooldown_bars} onChange={(v) => patch({ cooldown_bars: v })} min={0} />
                <NumField label="max spread (points)" value={cfg.max_spread_points} onChange={(v) => patch({ max_spread_points: v })} min={1} />
                <NumField label="news blackout (min)" value={cfg.news_blackout_min} onChange={(v) => patch({ news_blackout_min: v })} min={0} />
              </div>
            </fieldset>

            <fieldset className="rounded-lg border border-zinc-800 p-3">
              <legend className="px-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                risk (phase 4 execution)
              </legend>
              <div className="grid grid-cols-2 gap-3">
                <label className="block text-xs text-zinc-400">
                  mode
                  <select
                    className={`${inputCls} mt-1`}
                    value={cfg.risk_mode}
                    onChange={(e) => patch({ risk_mode: e.target.value })}
                  >
                    <option value="percent">percent of balance</option>
                    <option value="fixed">fixed lot</option>
                  </select>
                </label>
                <NumField label="risk %" value={cfg.risk_percent} onChange={(v) => patch({ risk_percent: v })} step="0.1" />
                <NumField label="fixed lot" value={cfg.fixed_lot} onChange={(v) => patch({ fixed_lot: v })} step="0.01" />
                <NumField label="daily max loss %" value={cfg.daily_max_loss_pct} onChange={(v) => patch({ daily_max_loss_pct: v })} step="0.5" />
              </div>
            </fieldset>

            <div className="rounded-lg border border-zinc-800 p-3">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-zinc-200">Auto-trade</p>
                  <p className="text-xs text-zinc-500">
                    execution engine lands in Phase 4 — OFF by default (global kill switch)
                  </p>
                </div>
                <span
                  className={`rounded-md border px-2 py-0.5 text-xs font-bold ${
                    autoTrade
                      ? "border-red-500/50 bg-red-500/10 text-red-400"
                      : "border-zinc-700 bg-zinc-800 text-zinc-400"
                  }`}
                >
                  {autoTrade ? "ARMED" : "OFF"}
                </span>
              </div>
              {isAdmin && (
                <div className="mt-3 space-y-2">
                  {!autoTrade ? (
                    <>
                      <input
                        className={inputCls}
                        value={confirmText}
                        onChange={(e) => setConfirmText(e.target.value)}
                        placeholder='type ENABLE to arm auto-trade'
                      />
                      <button
                        type="button"
                        disabled={busy || confirmText !== "ENABLE"}
                        onClick={() => toggleAutoTrade(true)}
                        className="w-full rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm font-semibold text-red-300 hover:bg-red-500/20 disabled:opacity-40"
                      >
                        arm auto-trade
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => toggleAutoTrade(false)}
                      className="w-full rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-500/20"
                    >
                      disarm (safe)
                    </button>
                  )}
                </div>
              )}
            </div>

            {error && (
              <p className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                {error}
              </p>
            )}
            {saved && (
              <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">
                saved — engine reloaded the config live
              </p>
            )}

            <button
              type="button"
              disabled={busy || !isAdmin}
              onClick={save}
              className="w-full rounded-md border border-gold/50 bg-gold/15 px-3 py-2 text-sm font-semibold text-gold hover:bg-gold/25 disabled:opacity-50"
            >
              {busy ? "saving…" : "Save config"}
            </button>
          </div>
        ) : (
          <p className="py-6 text-center text-xs text-zinc-500">
            {error ?? "loading config…"}
          </p>
        )}
      </div>
    </div>
  );
}
