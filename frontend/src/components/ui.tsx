/**
 * Shared UI atoms (D-041) — small, dependency-free building blocks with a
 * consistent dark-premium look. Everything is overflow-safe (min-w-0,
 * truncate, tabular numbers) so NOTHING ever renders outside the viewport.
 */

import type { ReactNode } from "react";

/* ------------------------------------------------------------------ card */

export function Card({
  children,
  className = "",
  padded = true,
}: {
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <section
      className={`min-w-0 rounded-2xl border border-zinc-800/80 bg-zinc-900/60 shadow-sm backdrop-blur-sm ${
        padded ? "p-4" : ""
      } ${className}`}
    >
      {children}
    </section>
  );
}

export function SectionTitle({
  title,
  right,
  icon,
}: {
  title: string;
  right?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="mb-3 flex min-w-0 items-center justify-between gap-2">
      <h2 className="flex min-w-0 items-center gap-2 text-sm font-semibold tracking-wide text-zinc-200">
        {icon}
        <span className="truncate">{title}</span>
      </h2>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}

/* ----------------------------------------------------------------- stats */

export function Stat({
  label,
  value,
  tone = "default",
  loading,
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: "default" | "up" | "down" | "gold";
  loading?: boolean;
  hint?: string;
}) {
  const toneCls =
    tone === "up"
      ? "text-emerald-400"
      : tone === "down"
        ? "text-red-400"
        : tone === "gold"
          ? "text-gold"
          : "text-zinc-100";
  return (
    <div className="min-w-0 rounded-xl border border-zinc-800/70 bg-zinc-900/40 px-3 py-2.5">
      <p className="truncate text-[10px] font-medium uppercase tracking-wider text-zinc-500">
        {label}
      </p>
      {loading ? (
        <div className="mt-1.5 h-5 w-16 animate-pulse rounded bg-zinc-800" />
      ) : (
        <p className={`mt-0.5 truncate font-mono text-[15px] font-semibold tabular-nums ${toneCls}`}>
          {value}
        </p>
      )}
      {hint && <p className="truncate text-[10px] text-zinc-500">{hint}</p>}
    </div>
  );
}

/* ---------------------------------------------------------------- badges */

export function Badge({
  children,
  tone = "zinc",
  pulse,
}: {
  children: ReactNode;
  tone?: "zinc" | "green" | "amber" | "red" | "gold" | "blue";
  pulse?: boolean;
}) {
  const map: Record<string, string> = {
    zinc: "border-zinc-700 bg-zinc-800/60 text-zinc-300",
    green: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
    amber: "border-amber-500/30 bg-amber-500/10 text-amber-300",
    red: "border-red-500/30 bg-red-500/10 text-red-300",
    gold: "border-gold/40 bg-gold/10 text-gold",
    blue: "border-sky-500/30 bg-sky-500/10 text-sky-300",
  };
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[10px] font-semibold tracking-wide ${map[tone]}`}
    >
      {pulse && (
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
        </span>
      )}
      {children}
    </span>
  );
}

/* -------------------------------------------------------------- skeleton */

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-zinc-800/80 ${className}`} />;
}

export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="flex min-w-0 items-center justify-center gap-2.5 py-10 text-sm text-zinc-400">
      <svg className="h-5 w-5 animate-spin text-gold" viewBox="0 0 24 24" fill="none">
        <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
        <path
          className="opacity-90"
          d="M12 2a10 10 0 0 1 10 10"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
        />
      </svg>
      <span className="truncate">{label}</span>
    </div>
  );
}

/* ------------------------------------------------------------- empty box */

export function EmptyState({ icon, title, hint }: { icon?: ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex min-w-0 flex-col items-center justify-center gap-1.5 py-8 text-center">
      {icon && <div className="text-zinc-600">{icon}</div>}
      <p className="text-sm font-medium text-zinc-400">{title}</p>
      {hint && <p className="max-w-xs text-xs leading-relaxed text-zinc-500">{hint}</p>}
    </div>
  );
}

/* ---------------------------------------------------------------- button */

export function Btn({
  children,
  onClick,
  variant = "default",
  disabled,
  className = "",
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "gold" | "danger" | "ghost" | "success";
  disabled?: boolean;
  className?: string;
  type?: "button" | "submit";
}) {
  const map: Record<string, string> = {
    default:
      "border-zinc-700 bg-zinc-800 text-zinc-200 hover:bg-zinc-700 active:scale-[0.98]",
    gold: "border-gold/50 bg-gold/15 text-gold hover:bg-gold/25 active:scale-[0.98]",
    danger:
      "border-red-500/40 bg-red-500/15 text-red-300 hover:bg-red-500/25 active:scale-[0.98]",
    success:
      "border-emerald-500/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 active:scale-[0.98]",
    ghost: "border-transparent bg-transparent text-zinc-400 hover:text-zinc-200",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-1.5 rounded-xl border px-3.5 py-2 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${map[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------ input row */

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-[10px] leading-snug text-zinc-500">{hint}</span>}
    </label>
  );
}

export const inputCls =
  "w-full min-w-0 rounded-xl border border-zinc-700/80 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 tabular-nums placeholder:text-zinc-600 focus:border-gold/60 focus:outline-none focus:ring-1 focus:ring-gold/30";

/* ---------------------------------------------------------------- toggle */

export function Dot({ tone }: { tone: "green" | "amber" | "red" | "zinc" }) {
  const map = {
    green: "bg-emerald-400 shadow-[0_0_6px_#34d399]",
    amber: "bg-amber-400 shadow-[0_0_6px_#fbbf24]",
    red: "bg-red-400 shadow-[0_0_6px_#f87171]",
    zinc: "bg-zinc-600",
  } as const;
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${map[tone]}`} />;
}
