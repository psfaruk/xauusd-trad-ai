import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * Supabase client (SPEC §4). Auth flows arrive in Phase 1 (SPEC §10) via this
 * client; it is null until VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY are set.
 */
export const supabase: SupabaseClient | null =
  import.meta.env.VITE_SUPABASE_URL && import.meta.env.VITE_SUPABASE_ANON_KEY
    ? createClient(
        import.meta.env.VITE_SUPABASE_URL as string,
        import.meta.env.VITE_SUPABASE_ANON_KEY as string
      )
    : null;

/** True when the SPA was built with Supabase credentials (login possible). */
export const supabaseConfigured = supabase !== null;
