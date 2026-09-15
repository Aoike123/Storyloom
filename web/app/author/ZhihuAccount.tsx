'use client';
import {CircleAlert, Gauge, LogOut} from 'lucide-react';
import type {ZhihuStatus} from '../zhihu-account';
import {isLowBeans, zhihuLoginLink, zhihuLogout} from '../zhihu-account';

/**
 * Sign-in entry and bean balance.
 *
 * Signed-in visitors spend beans; the shared pool and personal keys stay available, so this panel
 * never blocks the rest of the page. When the balance runs low it points at the other two options
 * instead of only reporting a number.
 */
export default function ZhihuAccount({status, busy, onChanged, next}: {
  status: ZhihuStatus | null;
  busy: boolean;
  onChanged: () => void;
  next: string;
}) {
  if (!status) {
    return <section className="zhihu-account"><div className="zhihu-account-heading"><Gauge size={16}/><span>正在读取知乎登录状态…</span></div></section>;
  }

  if (!status.authorized) {
    return <section className="zhihu-account">
      <div className="zhihu-account-heading"><Gauge size={16}/><strong>用知乎账号登录</strong></div>
      <p className="zhihu-account-note">
        登录后由账号赠送的算力豆承担生成费用；不登录也可以继续使用共享额度，或填写自己的 Key。
      </p>
      {status.configured
        ? <a className="button secondary" href={zhihuLoginLink(next)}>用知乎账号登录</a>
        : <p className="studio-note">知乎登录尚未在此部署配置；当前仍可用共享额度或自己的 Key 制作。</p>}
    </section>;
  }

  const account = status.account!;
  const wallet = status.wallet;
  const low = isLowBeans(wallet);
  return <section className="zhihu-account is-signed-in">
    <div className="zhihu-account-heading">
      {account.avatar_path && <img className="zhihu-account-avatar" src={account.avatar_path} alt=""/>}
      <div>
        <strong>{account.fullname || '知乎账号'}</strong>
        <small>{account.headline || '已用知乎账号登录'}</small>
      </div>
      {wallet && <span className={'zhihu-account-beans' + (low ? ' is-low' : '')}>{Number(wallet.beans).toFixed(0)} 算力豆</span>}
    </div>
    {wallet && <p className="zhihu-account-note">
      文本 {wallet.costs.llm} 豆／次 · 生图 {wallet.costs.image} 豆／张 · 视频 {wallet.costs.video} 豆／镜（赠送 {Number(wallet.granted).toFixed(0)} 豆）
    </p>}
    {low && <p className="zhihu-account-warning">
      <CircleAlert size={13}/> 算力豆不足一镜。可以继续做还要扣豆的步骤前，先去「切换模型额度」填写自己的 Key；也可以等新的赠送。
    </p>}
    <div className="zhihu-account-actions">
      <button className="button secondary" disabled={busy} onClick={async () => {await zhihuLogout(); onChanged();}}>
        <LogOut size={13}/> 退出登录
      </button>
    </div>
  </section>;
}
