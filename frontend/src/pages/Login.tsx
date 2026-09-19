import { useState, type FormEvent } from "react";
import { Navigate, Link } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { supabase, supabaseConfigured } from "../lib/supabase";

type Mode = "signin" | "signup";

const inputClass =
  "w-full rounded-md border border-zinc-700 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-gold/60 focus:outline-none focus:ring-1 focus:ring-gold/40";
const primaryButton =
  "w-full rounded-md bg-gold/90 px-3 py-2 text-sm font-semibold text-zinc-950 transition hover:bg-gold disabled:cursor-not-allowed disabled:opacity-50";
const ghostButton =
  "w-full rounded-md border border-zinc-700 px-3 py-2 text-sm text-zinc-300 transition hover:border-zinc-500 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-50";

/** Friendly copy for Supabase auth error codes. */
function authError(message: string): string {
  if (/invalid login credentials/i.test(message)) return "Wrong email or password.";
  if (/already registered/i.test(message)) return "That email is already registered — sign in instead.";
  if (/password should be at least/i.test(message))
    return "Password too short (minimum 6 characters).";
  if (/rate limit/i.test(message)) return "Too many attempts — please wait a moment and retry.";
  return message;
}

/**
 * /login (SPEC §10, Phase 1): Google OAuth + email/password + sign-up +
 * "Forgot password?" -> /reset-password. Redirects to / when a session exists.
 */
export default function Login() {
  const { session } = useAuth();
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (session) return <Navigate to="/" replace />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!supabase) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "signin") {
        const { error: err } = await supabase.auth.signInWithPassword({ email, password });
        if (err) throw err;
        // onAuthStateChange redirects to /.
      } else {
        const { data, error: err } = await supabase.auth.signUp({
          email,
          password,
          options: { emailRedirectTo: window.location.origin },
        });
        if (err) throw err;
        if (data.session) {
          // Email confirmation disabled in the Supabase project — signed in.
        } else {
          setNotice("Account created — check your inbox to confirm your email, then sign in.");
          setMode("signin");
        }
      }
    } catch (err) {
      setError(authError(err instanceof Error ? err.message : "Authentication failed"));
    } finally {
      setBusy(false);
    }
  };

  const google = async () => {
    if (!supabase) return;
    setError(null);
    const { error: err } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
    if (err) setError(authError(err.message));
  };

  return (
    <div className="grid min-h-screen place-items-center bg-zinc-950 p-4">
      <div className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900/60 p-6">
        <div className="mb-5 flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-gold shadow-[0_0_8px_#d4af37]" />
          <h1 className="text-lg font-semibold text-zinc-100">
            {mode === "signin" ? "Sign in" : "Create account"}
          </h1>
          <span className="ml-auto text-xs text-zinc-500">XAUUSD AI Platform</span>
        </div>

        {!supabaseConfigured && (
          <p className="mb-4 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
            Supabase is not configured. Set VITE_SUPABASE_URL and
            VITE_SUPABASE_ANON_KEY, then rebuild the frontend to enable login.
          </p>
        )}

        <form onSubmit={submit} className="space-y-3" noValidate>
          <input
            type="email"
            autoComplete="email"
            required
            placeholder="Email"
            className={inputClass}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <input
            type="password"
            autoComplete={mode === "signin" ? "current-password" : "new-password"}
            required
            minLength={6}
            placeholder="Password"
            className={inputClass}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />

          {error && (
            <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
              {error}
            </p>
          )}
          {notice && (
            <p className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
              {notice}
            </p>
          )}

          <button type="submit" disabled={busy || !supabaseConfigured} className={primaryButton}>
            {busy ? "Please wait…" : mode === "signin" ? "Sign in" : "Sign up"}
          </button>
        </form>

        <div className="my-4 flex items-center gap-3 text-xs text-zinc-600">
          <span className="h-px flex-1 bg-zinc-800" />
          or
          <span className="h-px flex-1 bg-zinc-800" />
        </div>

        <button type="button" onClick={google} disabled={!supabaseConfigured} className={ghostButton}>
          Continue with Google
        </button>

        <div className="mt-4 flex items-center justify-between text-xs">
          <button
            type="button"
            className="text-zinc-400 transition hover:text-gold"
            onClick={() => {
              setMode(mode === "signin" ? "signup" : "signin");
              setError(null);
              setNotice(null);
            }}
          >
            {mode === "signin" ? "Need an account? Sign up" : "Have an account? Sign in"}
          </button>
          <Link to="/reset-password" className="text-zinc-400 transition hover:text-gold">
            Forgot password?
          </Link>
        </div>
      </div>
    </div>
  );
}
