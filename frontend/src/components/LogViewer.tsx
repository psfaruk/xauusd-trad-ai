import { useEffect, useState } from "react";
import { getLogs, logsExportUrl } from "../lib/api";
import type { LogEntry } from "../types";

interface LogViewerProps {
  open: boolean;
  onClose: () => void;
  token: string;
}

const LEVELS = ["", "INFO", "WARNING", "ERROR"] as const;

/**
 * LogViewer — platform/engine activity (Phase 4 SPEC §12: LogViewer + CSV
 * export). CSV downloads via authorized fetch → blob (Bearer header needed).
 */
export default function LogViewer({ open, onClose, token }: LogViewerProps) {
  const [logs, setLogs] = useState<LogEntry[] | null>(null);
  const [level, setLevel] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    const load = () =>
      getLogs(token, 200, level || undefined)
        .then((res) => alive && setLogs(res.logs))
        .catch((e) => alive && setError(e instanceof Error ? e.message : "load failed"));
    load();
    const timer = window.setInterval(load, 10_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [open, token, level]);

  if (!open) return null;

  const exportCsv = async () => {
    try {
      const res = await fetch(logsExportUrl(), {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `xauusd-logs-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : "export failed");
    }
  };

  const levelColor = (lvl: string) =>
    lvl === "ERROR"
      ? "text-red-400"
      : lvl === "WARNING"
        ? "text-amber-400"
        : lvl === "CRITICAL"
          ? "text-red-300 font-bold"
          : "text-zinc-500";

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-3">
          <h2 className="text-base font-semibold text-zinc-100">Platform Logs</h2>
          <div className="flex items-center gap-2">
            <select
              value={level}
              onChange={(e) => setLevel(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-300"
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>{l || "all levels"}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => void exportCsv()}
              className="rounded-md border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:border-gold/50 hover:text-gold"
            >
              export CSV
            </button>
            <button type="button" onClick={onClose} className="text-zinc-500 hover:text-zinc-300">✕</button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4 font-mono text-[11px] leading-relaxed">
          {logs === null ? (
            <p className="text-zinc-500">loading…</p>
          ) : logs.length === 0 ? (
            <p className="text-zinc-500">no logs (DB logging inactive or empty)</p>
          ) : (
            logs.map((l, i) => (
              <p key={i} className="flex gap-2 border-b border-zinc-800/50 py-1">
                <span className="shrink-0 text-zinc-600">
                  {l.ts ? new Date(l.ts).toLocaleTimeString() : "—"}
                </span>
                <span className={`w-16 shrink-0 ${levelColor(l.level)}`}>[{l.level}]</span>
                <span className="w-28 shrink-0 truncate text-zinc-500">{l.source}</span>
                <span className="text-zinc-300">{l.message}</span>
              </p>
            ))
          )}
        </div>

        {error && <p className="border-t border-zinc-800 px-5 py-2 text-xs text-red-400">{error}</p>}
      </div>
    </div>
  );
}
