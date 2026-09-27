'use client';

/**
 * Auth context (port of the Vite app's lib/auth.tsx): local demo auth —
 * the token comes from the market-service /api/auth/login endpoint and
 * the session persists in localStorage. Same surface the original
 * Supabase provider exposed (session / signOut) plus signIn.
 *
 * The store is read through useSyncExternalStore — hydration-safe (the
 * server snapshot is always null; the client snapshot re-renders once
 * the stored session is available) with no setState-in-effect.
 */

import {
  createContext,
  useCallback,
  useContext,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { postLogin, type LoginResponse } from "./api";

export interface DemoSession {
  access_token: string;
  user: { id: string; email: string; role: string };
}

interface AuthState {
  session: DemoSession | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const STORAGE_KEY = "goldai.session";

/* ------------------------------------------------------ external store */

function parse(raw: string | null): DemoSession | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as LoginResponse;
    if (parsed?.access_token && parsed?.user) {
      return { access_token: parsed.access_token, user: parsed.user };
    }
  } catch {
    /* corrupted storage — treat as signed out */
  }
  return null;
}

let cached: DemoSession | null = null;
let hydrated = false;

function hydrate(): void {
  if (hydrated || typeof window === "undefined") return;
  hydrated = true;
  cached = parse(window.localStorage.getItem(STORAGE_KEY));
}

const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // cross-tab sync
  window.addEventListener("storage", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

function getSnapshot(): DemoSession | null {
  hydrate();
  return cached;
}

function getServerSnapshot(): DemoSession | null {
  return null;
}

function writeSession(next: DemoSession | null): void {
  cached = next;
  if (typeof window !== "undefined") {
    if (next) {
      const full: LoginResponse = {
        access_token: next.access_token,
        token_type: "bearer",
        expires_in: 86400,
        user: next.user,
      };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(full));
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  }
  listeners.forEach((l) => l());
}

/* ------------------------------------------------------------------ ctx */

const AuthContext = createContext<AuthState>({
  session: null,
  loading: false,
  signIn: async () => {},
  signOut: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const session = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const signIn = useCallback(async (email: string, password: string) => {
    const res = await postLogin(email, password);
    writeSession({ access_token: res.access_token, user: res.user });
  }, []);

  const signOut = useCallback(async () => {
    writeSession(null);
  }, []);

  return (
    <AuthContext.Provider value={{ session, loading: false, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
