import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * Supabase client (SPEC §4).
 *
 * Explicit VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY always win (production,
 * local e2e with a dedicated Supabase). In dev, when they are empty, fall back
 * to the page's own origin + a placeholder key: vite.config.ts proxies
 * /auth/v1 to the local auth mock (:8090), so the sandbox preview can log in
 * through its own host without any baked-in credentials (D-031).
 */
const supabaseUrl =
  (import.meta.env.VITE_SUPABASE_URL as string | undefined) ||
  (import.meta.env.DEV ? window.location.origin : "");
const supabaseAnonKey =
  (import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined) ||
  (import.meta.env.DEV ? "local-dev-proxy" : "");

export const supabase: SupabaseClient | null =
  supabaseUrl && supabaseAnonKey ? createClient(supabaseUrl, supabaseAnonKey) : null;

/** True when the SPA can reach Supabase (explicit env, or dev same-origin proxy). */
export const supabaseConfigured = supabase !== null;
