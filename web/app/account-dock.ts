/**
 * Shared plumbing for the account dock: own-key sign-in, and the "beans ran out" signal.
 *
 * The dock is the only place that manages who pays, so pages announce the problem and let the dock
 * explain the way forward instead of each page inventing its own prompt.
 */
import {saveModelAccess} from './model-access';

export const BEANS_EXHAUSTED_EVENT = 'storyloom:beans-exhausted';

export type OwnKeyInput = {deepseek: string; siliconflow: string; minimax: string};

/** True when a failed call failed because the wallet could not cover it. */
export function isBeansProblem(message: string) {
  return /算力豆不足|算力豆已用完/.test(message || '');
}

/** Ask the dock to explain how to continue after the wallet could not pay. */
export function announceBeansProblem(detail: string) {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(BEANS_EXHAUSTED_EVENT, {detail}));
}

export function onBeansProblem(handler: (detail: string) => void) {
  const listener = (event: Event) => handler(String((event as CustomEvent).detail || ''));
  window.addEventListener(BEANS_EXHAUSTED_EVENT, listener);
  return () => window.removeEventListener(BEANS_EXHAUSTED_EVENT, listener);
}

/** Store the visitor's own keys as an encrypted server session, then remember its token. */
export async function saveOwnKeys(keys: OwnKeyInput) {
  const provided = Object.fromEntries(
    Object.entries(keys).filter(([, value]) => value.trim().length > 0),
  );
  const response = await fetch('/api/model-access/sessions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode: 'own', keys: provided}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '暂时无法保存这些 Key。');
  saveModelAccess(data);
  return data as {mode: 'own'; expires_at: number};
}
