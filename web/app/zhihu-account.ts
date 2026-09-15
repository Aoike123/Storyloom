/**
 * Zhihu account session for the browser.
 *
 * The browser only ever holds an HttpOnly cookie set by the server; the OAuth token and the App Key
 * stay server-side. Everything here is a thin read of the public status endpoint.
 */

export type BeanCosts = {llm: string; image: string; video: string};
export type BeanLedgerEntry = {at: number; action: string; kind: string; amount: string; balance: string; detail?: string};
export type ZhihuWallet = {uid: string; beans: string; granted: string; costs: BeanCosts; ledger: BeanLedgerEntry[]};
export type ZhihuAccount = {
  uid: string;
  fullname: string | null;
  avatar_path: string | null;
  headline: string | null;
  url: string | null;
  expires_at: number;
};
export type ZhihuStatus = {
  configured: boolean;
  authorized: boolean;
  account: ZhihuAccount | null;
  wallet: ZhihuWallet | null;
};

export const emptyZhihuStatus: ZhihuStatus = {configured: false, authorized: false, account: null, wallet: null};

export async function readZhihuStatus(signal?: AbortSignal): Promise<ZhihuStatus> {
  const response = await fetch('/api/zhihu/status', {cache: 'no-store', signal});
  if (!response.ok) throw new Error('暂时无法读取知乎登录状态。');
  const data = await response.json();
  return {
    configured: data?.configured === true,
    authorized: data?.authorized === true,
    account: data?.account ?? null,
    wallet: data?.wallet ?? null,
  };
}

/** Start the login. The page navigates to Zhihu, so there is no fetch to await. */
export function zhihuLoginLink(next: string) {
  const safe = next.startsWith('/') && !next.startsWith('//') ? next : '/author';
  return '/api/zhihu/login?next=' + encodeURIComponent(safe);
}

export async function zhihuLogout() {
  await fetch('/api/zhihu/logout', {method: 'POST'});
}

/** Copy for the `?zhihu=` flag the callback redirects back with. */
export function zhihuNotice(flag: string | null): {text: string; error: boolean} | null {
  if (flag === 'ok') return {text: '已用知乎账号登录，算力豆已到账。', error: false};
  if (flag === 'denied') return {text: '你取消了知乎授权，尚未登录。', error: true};
  if (flag === 'error') return {text: '知乎登录没有完成，请再试一次；也可以继续使用共享额度或自己的 Key。', error: true};
  return null;
}

export function isLowBeans(wallet: ZhihuWallet | null) {
  if (!wallet) return false;
  const beans = Number(wallet.beans);
  const video = Number(wallet.costs?.video ?? 0);
  return Number.isFinite(beans) && Number.isFinite(video) && video > 0 && beans < video;
}
