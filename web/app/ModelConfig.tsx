'use client';

import {useEffect, useState} from 'react';
import {clearModelAccess, modelAccessHeaders, readModelAccess, saveModelAccess, type ModelAccessMode} from './model-access';

type Provider = {kind: string; provider: string; model: string; purpose: string};
type PoolProvider = {
  kind: string;
  provider: string;
  available: boolean;
  reason: string;
  daily_limit_cny: string;
  used_cny: string;
  remaining_cny: string;
};
type Pool = {
  available: boolean;
  reason: string;
  next_reset_at: number;
  providers: PoolProvider[];
};
type AccessStatus = {
  fixed_providers: Provider[];
  pool: Pool;
  session: {mode: ModelAccessMode; expires_at: number} | null;
};

const emptyKeys = {deepseek: '', siliconflow: '', minimax: ''};

function destination() {
  if (typeof window === 'undefined') return '/author';
  const value = new URLSearchParams(window.location.search).get('next') || '/author';
  return value.startsWith('/') && !value.startsWith('//') ? value : '/author';
}

export default function ModelConfig({onSaved}: {onSaved?: () => Promise<void>}) {
  const [status, setStatus] = useState<AccessStatus | null>(null);
  const [mode, setMode] = useState<ModelAccessMode>('public');
  const [keys, setKeys] = useState(emptyKeys);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState(false);

  async function load() {
    setMessage(''); setError(false);
    const existing = readModelAccess();
    let response = await fetch('/api/model-access/status', {headers: modelAccessHeaders(), cache: 'no-store'});
    if (response.status === 401 && existing) {
      clearModelAccess();
      response = await fetch('/api/model-access/status', {cache: 'no-store'});
    }
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '无法读取共享池状态。');
    setStatus(data);
    if (data.session?.mode === 'own' || (data.session?.mode === 'public' && data.pool.available)) setMode(data.session.mode);
    else if (!data.pool.available) setMode('own');
  }

  useEffect(() => {load().catch(reason => {setMessage(reason.message); setError(true);});}, []);

  async function continueWithCurrent() {
    if (!readModelAccess()) return;
    if (onSaved) await onSaved();
    window.location.href = destination();
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (mode === 'public' && !status?.pool.available) return;
    setBusy(true); setMessage(''); setError(false);
    try {
      const response = await fetch('/api/model-access/sessions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode, keys: mode === 'own' ? keys : undefined}),
      });
      const data = await response.json();
      if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '暂时无法启用该使用方式。');
      saveModelAccess(data);
      setKeys(emptyKeys);
      if (onSaved) await onSaved();
      window.location.href = destination();
    } catch (reason) {
      setMessage((reason as Error).message); setError(true);
    } finally {
      setBusy(false);
    }
  }

  const current = status?.session?.mode;
  const currentUsable = current === 'own' || (current === 'public' && !!status?.pool.available);
  const ownReady = Object.values(keys).every(value => value.trim().length >= 8);
  return <div className="model-access-shell">
    <header className="model-access-brand"><a href="/">叙间<span>STORYLOOM</span></a><small>模型使用方式</small></header>
    <main className="model-access-page">
      <section className="model-access-intro">
        <span className="model-access-kicker">BEFORE THE STORY TAKES SHAPE</span>
        <h1>先选择这次创作<br/>由谁提供模型额度。</h1>
        <p>无需注册账号。你可以抢先使用今日共享体验额度，也可以临时接入自己的 API Key。</p>
      </section>
      <form className="model-access-card" onSubmit={submit}>
        {current && <div className="model-access-current"><span>当前方式</span><strong>{current === 'public' ? '共享体验池' : '使用自己的 Key'}</strong>{currentUsable ? <button type="button" onClick={continueWithCurrent}>继续当前创作 →</button> : <small>额度已用完，请切换</small>}</div>}
        <div className="model-access-section-title"><div><small>01</small><h2>选择使用方式</h2></div><button type="button" className="model-access-refresh" onClick={() => load().catch(reason => {setMessage(reason.message); setError(true);})}>刷新额度</button></div>
        <div className="model-access-options">
          <button type="button" className={'model-access-option ' + (mode === 'public' ? 'is-selected' : '')} disabled={!status?.pool.available} onClick={() => setMode('public')} aria-pressed={mode === 'public'}>
            <span>共享体验池<em>{status?.pool.available ? '可用' : '不可用'}</em></span>
            <strong>{status ? '三个 Key 独立计量' : '读取中…'}</strong>
            <p>{status?.pool.reason || '正在核对今日额度与供应商状态'}</p>
            {status && <div className="model-access-pool-quotas">{status.pool.providers.map(provider => <span key={provider.kind}>
              <b>{provider.provider}<em>{provider.available ? '可用' : provider.reason}</em></b>
              <small>剩余 ¥{provider.remaining_cny} / ¥{provider.daily_limit_cny}</small>
            </span>)}</div>}
          </button>
          <button type="button" className={'model-access-option ' + (mode === 'own' ? 'is-selected' : '')} onClick={() => setMode('own')} aria-pressed={mode === 'own'}>
            <span>使用自己的 Key<em>稳定</em></span>
            <strong>BYOK</strong>
            <p>额度完全来自你的三个供应商账户，不占共享池。</p>
            <small>Key 加密、限时保存；页面不会回显</small>
          </button>
        </div>

        <div className="model-access-section-title"><div><small>02</small><h2>固定模型组合</h2></div><span>为保证作品效果，不开放更换</span></div>
        <div className="model-access-providers">{(status?.fixed_providers || []).map(provider => <article key={provider.kind}>
          <small>{provider.purpose}</small><strong>{provider.provider}</strong><code>{provider.model}</code>
        </article>)}</div>

        {mode === 'own' && <div className="model-access-keys">
          <div className="model-access-key-field"><div><label htmlFor="deepseek-key">DeepSeek API Key</label><a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener noreferrer">前往获取 ↗</a></div><input id="deepseek-key" type="password" autoComplete="off" value={keys.deepseek} onChange={event => setKeys({...keys, deepseek: event.target.value})} placeholder="用于文本理解与创作"/></div>
          <div className="model-access-key-field"><div><label htmlFor="siliconflow-key">硅基流动 API Key</label><a href="https://cloud.siliconflow.cn/account/ak" target="_blank" rel="noopener noreferrer">前往获取 ↗</a></div><input id="siliconflow-key" type="password" autoComplete="off" value={keys.siliconflow} onChange={event => setKeys({...keys, siliconflow: event.target.value})} placeholder="用于人物与场景生图"/></div>
          <div className="model-access-key-field"><div><label htmlFor="minimax-key">MiniMax API Key</label><a href="https://platform.minimax.cn/console/access?tab=api-keys" target="_blank" rel="noopener noreferrer">前往获取 ↗</a></div><input id="minimax-key" type="password" autoComplete="off" value={keys.minimax} onChange={event => setKeys({...keys, minimax: event.target.value})} placeholder="用于镜头视频生成"/></div>
          <p>Key 只发送给叙间后端，并以部署密钥加密保存；后台任务完成前请勿撤销供应商 Key。临时会话最长保留 24 小时。</p>
        </div>}

        <button className="model-access-submit" disabled={busy || !status || (mode === 'public' ? !status.pool.available : !ownReady)}>
          {busy ? '正在建立安全会话…' : mode === 'public' ? '使用共享额度，进入创作' : '使用自己的 Key，进入创作'}
        </button>
        <p className={'model-access-message ' + (error ? 'is-error' : '')} role="status">{message}</p>
      </form>
    </main>
    <footer className="model-access-footer">之后可从创作页底部随时回来切换；切换仅影响新任务。</footer>
  </div>;
}
