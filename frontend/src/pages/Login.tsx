/**
 * /login placeholder — Google OAuth + email/password + "Forgot password?"
 * arrive in Phase 1 (SPEC §10) via Supabase Auth.
 */
export default function Login() {
  return (
    <div className="grid min-h-screen place-items-center bg-zinc-950 p-4">
      <div className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900/60 p-6">
        <div className="mb-5 flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
          <h1 className="text-lg font-semibold">Sign in</h1>
        </div>
        <div className="space-y-3">
          <div className="h-9 rounded-md border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-500">
            Email
          </div>
          <div className="h-9 rounded-md border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-500">
            Password
          </div>
          <div className="h-9 rounded-md bg-gold/90 text-center text-sm font-semibold leading-9 text-zinc-950">
            Sign in
          </div>
          <div className="h-9 rounded-md border border-zinc-700 text-center text-sm leading-9 text-zinc-300">
            Continue with Google
          </div>
        </div>
        <p className="mt-4 text-center text-xs text-zinc-500">
          Auth flows (Google / email / reset) — Phase 1
        </p>
      </div>
    </div>
  );
}
