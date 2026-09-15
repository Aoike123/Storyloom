/** Only 'own' can pay for itself here; the retired shared pool no longer exists. */
export type ModelAccessMode = 'own';
export type ModelAccessSession = {token: string; mode: ModelAccessMode; expires_at: number};

const storageKey = 'storyloom.model-access.v1';

export function readModelAccess(): ModelAccessSession | null {
  if (typeof window === 'undefined') return null;
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    // A token for a retired mode is discarded instead of shadowing the signed-in account: sending
    // it made every call fail even though the browser had a working login.
    if (!value || typeof value.token !== 'string' || value.mode !== 'own' || Number(value.expires_at) <= Date.now() / 1000) {
      sessionStorage.removeItem(storageKey);
      return null;
    }
    return {token: value.token, mode: value.mode, expires_at: Number(value.expires_at)};
  } catch {
    return null;
  }
}

export function saveModelAccess(value: ModelAccessSession) {
  sessionStorage.setItem(storageKey, JSON.stringify(value));
}

export function clearModelAccess() {
  if (typeof window !== 'undefined') sessionStorage.removeItem(storageKey);
}

export function modelAccessHeaders(): Record<string, string> {
  const session = readModelAccess();
  return session ? {'X-Storyloom-Model-Access': session.token} : {};
}
