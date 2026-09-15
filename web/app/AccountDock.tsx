'use client';
import {useEffect, useRef, useState} from 'react';
import {ChevronDown, Coins, KeyRound, LogOut, X} from 'lucide-react';
import {clearModelAccess, readModelAccess} from './model-access';
import {emptyZhihuStatus, readZhihuStatus, zhihuLoginLink, zhihuLogout, type ZhihuStatus} from './zhihu-account';
import {
  OWN_KEY_PROVIDERS, announcePayerChanged, clearOwnKeys, isBeansProblem, onBeansProblem,
  saveOwnKeys, type OwnKeyInput,
} from './account-dock';
import './account-dock.css';

const emptyKeys: OwnKeyInput = {deepseek: '', siliconflow: '', minimax: ''};

/**
 * The single place that answers "who is paying".
 *
 * Sits in the top-right on every page. A signed-in account shows its beans; anyone can switch to
 * their own keys here, either by choice or because the dock was asked to explain the way forward
 * after a wallet ran out.
 */
export default function AccountDock({next = '/'}: {next?: string}) {
  const [status, setStatus] = useState<ZhihuStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [keys, setKeys] = useState(emptyKeys);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [noteError, setNoteError] = useState(false);
  const [reason, setReason] = useState('');
  const panel = useRef<HTMLDivElement>(null);

  async function reload() {
    try {
      setStatus(await readZhihuStatus());
    } catch {
      setStatus({...emptyZhihuStatus});
    }
  }

  useEffect(() => {void reload();}, []);

  useEffect(() => {
    // Beans are spent by the worker, so the balance changes without this component being involved.
    // Refresh while the panel is open and whenever the tab comes back, or the number looks frozen.
    if (!open) return;
    void reload();
    const timer = window.setInterval(() => {void reload();}, 8000);
    const onVisible = () => {if (document.visibilityState === 'visible') void reload();};
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onVisible);
    };
  }, [open]);

  useEffect(() => onBeansProblem(detail => {
    // A page hit the end of the wallet: open the dock and explain rather than just showing an error.
    setReason(detail);
    setOpen(true);
  }), []);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (panel.current && !panel.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {if (event.key === 'Escape') setOpen(false);};
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  async function submitKeys(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true); setNote(''); setNoteError(false);
    try {
      await saveOwnKeys(keys);
      setKeys(emptyKeys);
      setReason('');
      setNote('已改用你自己的 API Key，额度不再来自账号。');
      await reload();
      // Tell the page so work that stopped for lack of a payer continues with the new keys.
      announcePayerChanged();
    } catch (error) {
      setNote((error as Error).message);
      setNoteError(true);
    } finally {
      setBusy(false);
    }
  }

  // The server is authoritative about the payer; local storage is only a hint.
  const usingOwnKeys = status?.own_keys ?? !!readModelAccess();
  const account = status?.account ?? null;
  const wallet = status?.wallet ?? null;
  const beans = wallet ? Number(wallet.beans) : null;
  // Which own keys the server actually recorded. The panel must show this: a failed call looked
  // like the key was never saved because nothing confirmed it.
  const keySummary = status?.own_key_summary ?? null;
  const attachedCount = keySummary ? Object.values(keySummary.providers).filter(item => item?.attached).length : 0;

  const label = account ? (account.fullname || '知乎账号')
    : usingOwnKeys ? '使用自己的 Key'
    : '登录';
  const accountInitial = Array.from(account?.fullname || '叙')[0] || '叙';

  return <div className="account-dock" ref={panel}>
    <button className="account-dock-trigger" aria-expanded={open} onClick={() => setOpen(value => !value)}>
      {account?.avatar_path
        ? <img src={account.avatar_path} alt=""/>
        : <span className="account-dock-dot" aria-hidden="true"/>}
      <span className="account-dock-name">{label}</span>
      {beans !== null && <span className={'account-dock-beans' + (beans < Number(wallet?.costs?.video ?? 0) ? ' is-low' : '')}>
        <Coins size={12}/>{beans.toFixed(0)}
      </span>}
      {beans === null && usingOwnKeys && <KeyRound size={12}/>}
      <ChevronDown size={12} className="account-dock-chevron" aria-hidden="true"/>
    </button>

    {open && <div className="account-dock-panel" role="dialog" aria-label="账号与创作额度">
      <div className="account-dock-head">
        <div className="account-dock-title">
          {account?.avatar_path
            ? <img className="account-dock-head-avatar" src={account.avatar_path} alt=""/>
            : <span className="account-dock-head-placeholder" aria-hidden="true">{accountInitial}</span>}
          <span><small>{account ? 'SIGNED IN' : 'STORYLOOM ACCOUNT'}</small>
            <strong>{account ? (account.fullname || '知乎账号') : '登录与创作额度'}</strong>
          </span>
        </div>
        <button className="account-dock-close" aria-label="关闭" onClick={() => setOpen(false)}><X size={14}/></button>
      </div>

      {reason && <p className="account-dock-alert">
        {reason}
        <br/>填写自己的 API Key 就能继续；已经做好的图片和分镜都会保留。
      </p>}

      {account && wallet && <>
        <p className="account-dock-line">
          剩余 <strong>{beans!.toFixed(0)}</strong> 算力豆（赠送 {Number(wallet.granted).toFixed(0)}）
        </p>
        <p className="account-dock-costs">
          文本 {wallet.costs.llm} 豆／次 · 生图 {wallet.costs.image} 豆／张 · 视频 {wallet.costs.video} 豆／镜
        </p>
      </>}

      {!account && status?.configured && <p className="account-dock-line">
        用知乎账号登录即可领取算力豆，也可以直接填写自己的 API Key。
      </p>}
      {!account && status && !status.configured && <p className="account-dock-line">
        当前部署未开通知乎登录，请填写自己的 API Key。
      </p>}

      {usingOwnKeys && <p className="account-dock-line">
        当前用你自己的 API Key 支付：已录入 {attachedCount} / {OWN_KEY_PROVIDERS.length} 把。
        {keySummary && !keySummary.readable && ' 已录入的 Key 无法解密，请重新填写。'}
        {keySummary?.expires_at ? ` 该使用方式有效至 ${new Date(keySummary.expires_at * 1000).toLocaleString()}，到期后需重新填写或改用账号登录。` : ''}
      </p>}

      <div className="account-dock-actions">
        {!account && status?.configured &&
          <a className="button primary" href={zhihuLoginLink(next)}>用知乎账号登录</a>}
        {account && <button className="button secondary" disabled={busy} onClick={async () => {
          await zhihuLogout(); clearModelAccess(); setNote('已退出登录。'); await reload();
        }}><LogOut size={13}/> 退出登录</button>}
      </div>

      <form className="account-dock-keys" onSubmit={submitKeys}>
        <p className="account-dock-subtitle">使用自己的 API Key</p>
        <p className="account-dock-hint">只填需要的那个也可以，缺哪一个就在需要它的那一步提示。点「前往获取」可以直接到对应平台的 Key 页面。</p>
        {OWN_KEY_PROVIDERS.map(provider => {
          const attached = keySummary?.providers?.[provider.statusKind];
          return <div className="account-dock-keyfield" key={provider.kind}>
            <div className="account-dock-keyhead">
              <label htmlFor={'own-key-' + provider.kind}>{provider.label}
                <span className="account-dock-purpose"> · {provider.purpose}</span>
              </label>
              <a href={provider.applyUrl} target="_blank" rel="noopener noreferrer">前往获取 ↗</a>
            </div>
            <input id={'own-key-' + provider.kind} type="password" autoComplete="off"
                   placeholder={attached ? '已录入 · 尾号 ' + attached.tail + ' · 留空则保持不变' : provider.placeholder}
                   value={keys[provider.kind]}
                   onChange={e => setKeys({...keys, [provider.kind]: e.target.value})}/>
            <p className={'account-dock-keystate' + (attached ? ' is-attached' : '')}>
              {attached
                ? `已录入（尾号 ${attached.tail}，共 ${attached.length} 位）`
                : '未录入：用到它的时候会提示'}
            </p>
          </div>;
        })}
        <button className="button account-dock-save" disabled={busy || !Object.values(keys).some(v => v.trim())}>
          {busy ? '正在保存…' : '用这些 Key 继续'}
        </button>
        {usingOwnKeys && <button type="button" className="button secondary" disabled={busy}
          onClick={async () => {
            setBusy(true); setNote(''); setNoteError(false);
            try {
              await clearOwnKeys(); clearModelAccess(); await reload();
              setNote('已移除自己的 Key，之后由账号算力豆支付。');
            } catch (error) {
              setNote((error as Error).message); setNoteError(true);
            } finally { setBusy(false); }
          }}>移除已录入的 Key</button>}
      </form>
      {note && <p className={'account-dock-note' + (noteError ? ' is-error' : '')} role="status">{note}</p>}
    </div>}
  </div>;
}
