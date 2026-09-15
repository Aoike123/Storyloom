/**
 * Shared plumbing for the account dock: own-key sign-in, and the "beans ran out" signal.
 *
 * The dock is the only place that manages who pays, so pages announce the problem and let the dock
 * explain the way forward instead of each page inventing its own prompt.
 */
import {modelAccessHeaders, saveModelAccess} from './model-access';

export const BEANS_EXHAUSTED_EVENT = 'storyloom:beans-exhausted';
/** Fired after the browser switches to its own API keys, so stopped work can continue. */
export const PAYER_CHANGED_EVENT = 'storyloom:payer-changed';

export type OwnKeyInput = {deepseek: string; siliconflow: string; minimax: string};

/**
 * The three fixed providers, the field that holds each key, and where a visitor creates one.
 * Visitors never choose an endpoint or model, so this is the only place that names them.
 */
export type OwnKeyKind = keyof OwnKeyInput;
export type ModelKind = 'llm' | 'image' | 'video';
export type ProviderEntry = {
  kind: OwnKeyKind;
  statusKind: ModelKind;
  label: string;
  purpose: string;
  placeholder: string;
  applyUrl: string;
};

export const OWN_KEY_PROVIDERS: ProviderEntry[] = [
  {kind: 'deepseek', statusKind: 'llm', label: 'DeepSeek API Key', purpose: '文本理解与创作',
   placeholder: '用于文本理解与创作', applyUrl: 'https://platform.deepseek.com/api_keys'},
  {kind: 'siliconflow', statusKind: 'image', label: '硅基流动 API Key', purpose: '人物、服装与场景画面',
   placeholder: '用于人物与场景生图', applyUrl: 'https://cloud.siliconflow.cn/account/ak'},
  {kind: 'minimax', statusKind: 'video', label: 'MiniMax API Key', purpose: '镜头视频生成',
   placeholder: '用于镜头视频生成', applyUrl: 'https://platform.minimax.cn/console/access?tab=api-keys'},
];

export type AttachedKey = {attached: boolean; tail: string; length: number};
export type OwnKeySummary = {
  readable: boolean;
  expires_at: number;
  providers: Partial<Record<ModelKind, AttachedKey>>;
};

/** Drop the attached own keys so the browser goes back to paying from its account wallet. */
export async function clearOwnKeys() {
  const response = await fetch('/api/model-access/session', {method: 'DELETE', headers: modelAccessHeaders()});
  if (!response.ok) throw new Error('暂时无法移除已录入的 Key。');
}

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

export function announcePayerChanged() {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(PAYER_CHANGED_EVENT));
}

export function onPayerChanged(handler: () => void) {
  if (typeof window === 'undefined') return () => undefined;
  window.addEventListener(PAYER_CHANGED_EVENT, handler);
  return () => window.removeEventListener(PAYER_CHANGED_EVENT, handler);
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
