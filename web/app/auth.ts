// Client-side account session for Storyloom.
//
// The backend keeps a single opaque bearer token per account (rotated on every login). The browser
// stores that token in localStorage after a successful register/login and sends it as an
// `Authorization: Bearer …` header on the owner-scoped (author/*) requests. Public browsing and the
// workbench need no token. This is deliberately framework-agnostic: no third-party auth, no cookies
// that would need CORS, no token-decoding on the client — the token is opaque.

export type AuthUser = {
  id: string;
  username: string;
  email?: string;
  created?: number;
};

const TOKEN_KEY = 'storyloom.auth.token.v1';
const USER_KEY = 'storyloom.auth.user.v1';

function storage(): Storage | null {
  return typeof window === 'undefined' ? null : window.localStorage;
}

/** The opaque bearer token, or null when logged out. */
export function getToken(): string | null {
  const s = storage();
  return s ? s.getItem(TOKEN_KEY) : null;
}

/** The logged-in user, or null when logged out. */
export function getUser(): AuthUser | null {
  const s = storage();
  if (!s) return null;
  const raw = s.getItem(USER_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && parsed.id && parsed.username ? parsed : null;
  } catch {
    return null;
  }
}

// The backend also accepts the token as an `sl_auth` cookie. Writing it (in addition to the
// localStorage copy) means the existing workbench fetches — which do not set an Authorization
// header — still carry the token through the same-origin Next.js rewrite, so the owner check
// passes without touching any workbench request.
const COOKIE_KEY = 'sl_auth';
const COOKIE_TTL = 30 * 24 * 60 * 60; // 30 days

function writeCookie(token: string) {
  if (typeof document === 'undefined') return;
  document.cookie = `${COOKIE_KEY}=${token}; path=/; samesite=lax; max-age=${COOKIE_TTL}`;
}

function eraseCookie() {
  if (typeof document === 'undefined') return;
  document.cookie = `${COOKIE_KEY}=; path=/; samesite=lax; max-age=0`;
}

export function setSession(token: string, user: AuthUser) {
  const s = storage();
  if (s) {
    s.setItem(TOKEN_KEY, token);
    s.setItem(USER_KEY, JSON.stringify(user));
  }
  writeCookie(token);
}

export function clearSession() {
  const s = storage();
  if (s) {
    s.removeItem(TOKEN_KEY);
    s.removeItem(USER_KEY);
  }
  eraseCookie();
}

export function isLoggedIn(): boolean {
  return !!getToken();
}

/** Authorization header for owner-scoped (author/*) requests; empty when logged out. */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? {Authorization: 'Bearer ' + token} : {};
}

export class AuthError extends Error {}

async function call(path: string, init?: RequestInit): Promise<any> {
  const response = await fetch(path, {
    ...init,
    headers: {...(init?.headers as Record<string, string> | undefined), ...authHeaders()},
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new AuthError(typeof data.detail === 'string' ? data.detail : '操作失败，请重试。');
  }
  return data;
}

/** Sign in; stores the returned token + user and returns the user. */
export async function login(username: string, password: string): Promise<AuthUser> {
  const data = await call('/api/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username, password}),
  });
  setSession(data.token, data.user);
  return data.user;
}

/** Create an account; the new token + user are stored and returned. */
export async function register(username: string, password: string): Promise<AuthUser> {
  const data = await call('/api/auth/register', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username, password}),
  });
  setSession(data.token, data.user);
  return data.user;
}

/** Drop the local session (the server-side token is simply no longer sent). */
export function logout() {
  clearSession();
}
