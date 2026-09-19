import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { supabase } from "../lib/supabase";

const inputClass =
  "w-full rounded-md border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-gold/60 focus:outline-none focus:ring-1 focus:ring-gold/40";
const primaryButton =
  "w-full rounded-md bg-gold/90 px-3 py-2 text-sm font-semibold text-zinc-950 transition hover:bg-gold disabled:cursor-not-allowed disabled:opacity-50";

/**
 * /reset-password (SPEC §10, Phase 1).
 * - no session  -> request the recovery email (resetPasswordForEmail, PKCE,
 *   redirectTo this page)
 * - session from the recovery link (PASSWORD_RECOVERY) or an existing
 *   session -> set a new password (auth.updateUser)
 */
export default function ResetPassword() {
  const { session } = useAuth();
  const [recovery, setRecovery] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  // Supabase emits PASSWORD_RECOVERY when the user lands here from the
  // emailed link (detectSessionInUrl). Any other session also unlocks the
  // "set password" form (works as a change-password page).
  useEffect(() => {
    if (!supabase) return;
    const { data: sub } = supabase.auth.onAuthStateChange((event, next) => {
      if (event === "PASSWORD_RECOVERY" && next) setRecovery(true);
    });
    return () => sub.subscription.unsubscribe();
  }, []);

  const mode = recovery || session !== null ? "set" : "request";

  const requestReset = async (event: FormEvent) => {
    event.preventDefault();
    if (!supabase) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const { error: err } = await supabase.auth.resetPasswordForEmail(email, {
        redirectTo: `${window.location.origin}/reset-password`,
      });
      if (err) throw err;
      setDone("Reset link sent — check your inbox (and spam folder).");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send the reset email");
    } finally {
      setBusy(false);
    }
  };

  const setNewPassword = async (event: FormEvent) => {
    event.preventDefault();
    if (!supabase) return;
    if (password.length < 6) {
      setError("Password too short (minimum 6 characters).");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const { error: err } = await supabase.auth.updateUser({ password });
      if (err) throw err;
      setDone("Password updated — you are signed in.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the password");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-screen place-items-center bg-zinc-950 p-4">
      <div className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900/60 p-6">
        <div className="mb-5 flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
          <h1 className="text-lg font-semibold text-zinc-100">
            {mode === "set" ? "Set new password" : "Reset password"}
          </h1>
        </div>

        {mode === "request" ? (
          <form onSubmit={requestReset} className="space-y-3" noValidate>
            <p className="text-xs text-zinc-400">
              Enter your account email — we will send a recovery link.
            </p>
            <input
              type="email"
              required
              autoComplete="email"
              placeholder="Email"
              className={inputClass}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            {error && (
              <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
                {error}
              </p>
            )}
            {done && (
              <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
                {done}
              </p>
            )}
            <button type="submit" disabled={busy || !supabase} className={primaryButton}>
              {busy ? "Sending…" : "Send reset link"}
            </button>
          </form>
        ) : (
          <form onSubmit={setNewPassword} className="space-y-3" noValidate>
            <p className="text-xs text-zinc-400">
              {recovery
                ? "Recovery link verified — choose a new password."
                : "You are signed in — set a new password."}
            </p>
            <input
              type="password"
              required
              minLength={6}
              autoComplete="new-password"
              placeholder="New password"
              className={inputClass}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <input
              type="password"
              required
              minLength={6}
              autoComplete="new-password"
              placeholder="Confirm new password"
              className={inputClass}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
            {error && (
              <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
                {error}
              </p>
            )}
            {done && (
              <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
                {done}
              </p>
            )}
            <button type="submit" disabled={busy} className={primaryButton}>
              {busy ? "Updating…" : "Update password"}
            </button>
          </form>
        )}

        <div className="mt-4 text-center">
          <Link to="/login" className="text-xs text-zinc-400 transition hover:text-gold">
            Back to sign in
          </Link>
        </div>
      </div>
    </div>
  );
}
