'use client';

/**
 * The XAUUSD AI Trading Platform — single-page app entry (the port of the
 * Vite app's App.tsx). Next.js serves the whole SPA from the single "/" route:
 *   loading → Login (demo auth) → Dashboard (4 tabs: Home / Charts / AI / Settings)
 */

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, useAuth } from "../lib/auth";
import Login from "../components/Login";
import Dashboard from "../components/Dashboard";

/** Route guard: no session → Login (the SPA's only two "routes"). */
function Shell() {
  const { session, loading } = useAuth();
  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center bg-zinc-950">
        <div className="flex items-center gap-2 text-sm text-zinc-400">
          <span className="inline-block h-2.5 w-2.5 animate-pulse rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
          loading session…
        </div>
      </div>
    );
  }
  return session ? <Dashboard /> : <Login />;
}

export default function Page() {
  // one QueryClient per browser session (constructed lazily on the client)
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <AuthProvider>
        <Shell />
      </AuthProvider>
    </QueryClientProvider>
  );
}
