/**
 * ErrorBoundary (D-041) — a rendering crash (e.g. inside the chart library)
 * must NEVER blank the whole app: show a compact recovery card instead.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary]", this.props.label ?? "", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex min-h-40 min-w-0 flex-col items-center justify-center gap-2 rounded-2xl border border-red-500/25 bg-red-500/5 p-6 text-center">
          <p className="text-sm font-semibold text-red-300">
            {this.props.label ?? "Section"} hit a problem
          </p>
          <p className="max-w-sm break-words font-mono text-[11px] leading-relaxed text-zinc-500">
            {this.state.error.message}
          </p>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="mt-1 rounded-xl border border-zinc-700 bg-zinc-800 px-3.5 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-700"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
