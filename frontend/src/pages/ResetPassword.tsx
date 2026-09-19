/**
 * /reset-password placeholder — PKCE recovery token handling + new password
 * form arrive in Phase 1 (SPEC §10).
 */
export default function ResetPassword() {
  return (
    <div className="grid min-h-screen place-items-center bg-zinc-950 p-4">
      <div className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900/60 p-6">
        <h1 className="mb-4 text-lg font-semibold">Reset password</h1>
        <div className="space-y-3">
          <div className="h-9 rounded-md border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-500">
            New password
          </div>
          <div className="h-9 rounded-md bg-gold/90 text-center text-sm font-semibold leading-9 text-zinc-950">
            Update password
          </div>
        </div>
        <p className="mt-4 text-center text-xs text-zinc-500">
          Recovery token (PKCE) handling — Phase 1
        </p>
      </div>
    </div>
  );
}
