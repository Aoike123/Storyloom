'use client';

// The Storyloom project entry (D09).
//
// Replaces the old single-maker story market. The page is the doorway into the workbench:
// a logged-in author sees their own projects at the top and creates new ones; everyone (including
// anonymous visitors) can browse projects that others have published. There is no story import or
// story market here — a project owns its own original text, entered in the workbench.
import {useCallback, useEffect, useState} from 'react';
import {FolderOpen, LogIn, LogOut, Play, Plus, X} from 'lucide-react';
import {authHeaders, clearSession, getUser, login, register, type AuthUser} from './auth';
import {uiErrorMessage} from './ui-errors';
import './project-home.css';

type Creator = {name?: string; avatar_path?: string | null} | null;
type Project = {
  id: string;
  title?: string;
  description?: string;
  stage?: string;
  created?: number;
  release_id?: string;
  creator?: Creator;
};

const STAGE_LABELS: Record<string, string> = {
  story: '故事',
  style: '定调',
  outline: '分集',
  design: '设计',
  producing: '制作',
  film_review: '成片审核',
  episode_review: '单集审核',
  published: '已发布',
};

function stageLabel(stage?: string): string {
  return stage ? STAGE_LABELS[stage] || stage : '';
}

// The projects endpoints return a bare array; tolerate a {projects: [...]} envelope as well.
function asProjectList(value: unknown): Project[] {
  if (Array.isArray(value)) return value as Project[];
  const envelope = value as {projects?: unknown} | null;
  if (envelope && Array.isArray(envelope.projects)) return envelope.projects as Project[];
  return [];
}

export default function ProjectHome() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [mine, setMine] = useState<Project[]>([]);
  const [pub, setPub] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);

  const [authOpen, setAuthOpen] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [loginName, setLoginName] = useState('');
  const [loginPass, setLoginPass] = useState('');
  const [loginErr, setLoginErr] = useState('');
  const [loginBusy, setLoginBusy] = useState(false);

  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newErr, setNewErr] = useState('');
  const [newBusy, setNewBusy] = useState(false);

  const [active, setActive] = useState<Project | null>(null);

  // Restore the session from localStorage on first client render.
  useEffect(() => { setUser(getUser()); }, []);

  const loadProjects = useCallback(async (u: AuthUser | null) => {
    setLoading(true);
    try {
      const [pubRes, myRes] = await Promise.all([
        fetch('/api/author/projects?scope=public').then(r => (r.ok ? r.json() : [])),
        u
          ? fetch('/api/author/projects', {headers: authHeaders()}).then(r => (r.ok ? r.json() : []))
          : Promise.resolve([]),
      ]);
      setPub(asProjectList(pubRes));
      setMine(u ? asProjectList(myRes) : []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadProjects(user); }, [user, loadProjects]);

  function openAuth(mode: 'login' | 'register') {
    setAuthMode(mode); setLoginErr(''); setAuthOpen(true);
  }

  async function submitAuth() {
    const name = loginName.trim();
    if (!name || !loginPass) { setLoginErr('请填写用户名和密码。'); return; }
    setLoginErr(''); setLoginBusy(true);
    try {
      const u = authMode === 'login' ? await login(name, loginPass) : await register(name, loginPass);
      setLoginName(''); setLoginPass(''); setAuthOpen(false);
      setUser(u);
      await loadProjects(u);
    } catch (e) {
      setLoginErr(uiErrorMessage(e, authMode === 'login' ? '登录失败，请重试。' : '注册失败，请重试。'));
    } finally {
      setLoginBusy(false);
    }
  }

  function doLogout() {
    clearSession();
    setUser(null);
    loadProjects(null);
  }

  async function submitNew() {
    setNewErr(''); setNewBusy(true);
    try {
      const r = await fetch('/api/author/projects', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', ...authHeaders()},
        body: JSON.stringify({title: newName.trim() || undefined}),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '创建失败，请重试。');
      window.location.href = '/author/workbench?work=' + d.id;
    } catch (e) {
      setNewErr(uiErrorMessage(e, '创建失败，请重试。'));
      setNewBusy(false);
    }
  }

  function openNew() {
    if (!user) { openAuth('register'); return; }
    setNewName(''); setNewErr(''); setNewOpen(true);
  }

  function openWorkbench(id: string) {
    window.location.href = '/author/workbench?work=' + id;
  }

  return (
    <div className="project-home">
      <header className="ph-topbar">
        <a className="ph-brand" href="/" aria-label="叙间">叙间</a>
        <nav className="ph-nav" aria-label="项目">
          {user ? (
            <button type="button" className="button secondary" onClick={() => document.getElementById('mine')?.scrollIntoView({behavior: 'smooth'})}>
              <FolderOpen size={15}/>我的项目
            </button>
          ) : null}
          <button type="button" className="button primary" onClick={openNew}><Plus size={15}/>新建项目</button>
        </nav>
        <div className="ph-auth">
          {user ? (
            <div className="ph-user">
              <span className="ph-username">{user.username}</span>
              <button type="button" className="button link" onClick={doLogout}><LogOut size={14}/>退出</button>
            </div>
          ) : (
            <>
              <button type="button" className="button secondary" onClick={() => openAuth('login')}><LogIn size={15}/>登录</button>
              <button type="button" className="button primary" onClick={() => openAuth('register')}>注册</button>
            </>
          )}
        </div>
      </header>

      <main className="ph-main">
        {user && (
          <section className="ph-section" id="mine">
            <div className="ph-section-head">
              <h2>我的项目</h2>
              <span className="ph-count">{mine.length > 0 ? mine.length + ' 个' : ''}</span>
            </div>
            {mine.length === 0 ? (
              <p className="ph-empty">还没有项目。点右上角「新建项目」开始。</p>
            ) : (
              <div className="ph-grid">
                {mine.map(p => (
                  <button type="button" key={p.id} className="ph-card" onClick={() => openWorkbench(p.id)}>
                    <span className="ph-card-title">{p.title || '未命名项目'}</span>
                    <span className="ph-card-meta">{stageLabel(p.stage) || '进行中'}</span>
                  </button>
                ))}
              </div>
            )}
          </section>
        )}

        <section className="ph-section">
          <div className="ph-section-head">
            <h2>公开项目</h2>
            <span className="ph-count">{pub.length > 0 ? pub.length + ' 个' : ''}</span>
          </div>
          {loading ? (
            <p className="ph-loading">正在加载公开项目…</p>
          ) : pub.length === 0 ? (
            <p className="ph-empty">还没有公开的漫剧。别人把作品发布后，会出现在这里。</p>
          ) : (
            <div className="ph-grid">
              {pub.map(p => (
                <button type="button" key={p.id} className="ph-card" onClick={() => setActive(p)}>
                  <span className="ph-card-title">{p.title || '未命名项目'}</span>
                  {p.description ? <span className="ph-card-desc">{p.description}</span> : null}
                  <span className="ph-card-meta">{stageLabel(p.stage) || '进行中'}{p.creator?.name ? ' · ' + p.creator.name : ''}</span>
                </button>
              ))}
            </div>
          )}
        </section>
      </main>

      {authOpen && (
        <div className="ph-overlay" role="dialog" aria-modal="true" aria-label={authMode === 'login' ? '登录' : '注册'} onClick={e => { if (e.target === e.currentTarget) setAuthOpen(false); }}>
          <div className="ph-modal">
            <div className="ph-modal-head">
              <strong>{authMode === 'login' ? '登录' : '注册'}</strong>
              <button type="button" className="button secondary icon" aria-label="关闭" onClick={() => setAuthOpen(false)}><X size={15}/></button>
            </div>
            <div className="ph-modal-body">
              <label><span>用户名</span><input value={loginName} onChange={e => setLoginName(e.target.value)} placeholder="你的用户名" autoFocus/></label>
              <label><span>密码</span><input type="password" value={loginPass} onChange={e => setLoginPass(e.target.value)} placeholder="至少 8 位" onKeyDown={e => { if (e.key === 'Enter') submitAuth(); }}/></label>
              {loginErr ? <p className="ph-err" role="alert">{loginErr}</p> : null}
              <button type="button" className="button primary ph-full" disabled={loginBusy} onClick={submitAuth}>{loginBusy ? '请稍候…' : authMode === 'login' ? '登录' : '创建账号'}</button>
              <button type="button" className="button link ph-switch" onClick={() => { setAuthMode(authMode === 'login' ? 'register' : 'login'); setLoginErr(''); }}>
                {authMode === 'login' ? '没有账号？去注册' : '已有账号？去登录'}
              </button>
            </div>
          </div>
        </div>
      )}

      {newOpen && (
        <div className="ph-overlay" role="dialog" aria-modal="true" aria-label="新建项目" onClick={e => { if (e.target === e.currentTarget) setNewOpen(false); }}>
          <div className="ph-modal">
            <div className="ph-modal-head">
              <strong>新建项目</strong>
              <button type="button" className="button secondary icon" aria-label="关闭" onClick={() => setNewOpen(false)}><X size={15}/></button>
            </div>
            <div className="ph-modal-body">
              <label><span>项目名称</span><input value={newName} onChange={e => setNewName(e.target.value)} placeholder="给你的漫剧起个名字" autoFocus onKeyDown={e => { if (e.key === 'Enter') submitNew(); }}/></label>
              <p className="ph-hint">创建后进入工作台，在「故事」里贴上你的原文即可开始。</p>
              {newErr ? <p className="ph-err" role="alert">{newErr}</p> : null}
              <button type="button" className="button primary ph-full" disabled={newBusy} onClick={submitNew}>{newBusy ? '创建中…' : '创建并进入工作台'}</button>
            </div>
          </div>
        </div>
      )}

      {active && (
        <div className="ph-overlay" role="dialog" aria-modal="true" aria-label="项目详情" onClick={e => { if (e.target === e.currentTarget) setActive(null); }}>
          <div className="ph-modal">
            <div className="ph-modal-head">
              <strong>{active.title || '未命名项目'}</strong>
              <button type="button" className="button secondary icon" aria-label="关闭" onClick={() => setActive(null)}><X size={15}/></button>
            </div>
            <div className="ph-modal-body">
              {active.creator?.name ? <p className="ph-detail-creator">创作者 · {active.creator.name}</p> : null}
              <p className="ph-detail-desc">{active.description || '这个项目还没有填写简介。'}</p>
              <p className="ph-detail-stage">当前阶段 · {stageLabel(active.stage) || '进行中'}</p>
              {active.release_id ? (
                <button type="button" className="button primary ph-full"
                  onClick={() => { window.location.href = '/watch?release=' + active.release_id; }}>
                  <Play size={15}/> 观看成片
                </button>
              ) : (
                <p className="ph-hint">这部作品还在制作中；发布后会在这里提供观看。</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
