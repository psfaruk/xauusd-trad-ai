import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * Supabase client (SPEC §4). Auth flows arrive in Phase 1; the client is null
 * until VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY are configured.
 */
export const supabase: SupabaseClient | null =
  import.meta.env.VITE_SUPABASE_URL && import.meta.env.VITE_SUPABASE_ANON_KEY
    ? createClient(
        import.meta.env.VITE_SUPABASE_URL,
        import.meta.env.VITE_SUPABASE_ANON_KEY
      )
    : null;
