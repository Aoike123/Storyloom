const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// VIEW-01 验收：状态容器（shell/studio-state.ts，vm+transpile 纯逻辑）
// + StudioApp.tsx 接线（字符串断言；禁止项照 studio-app.test.cjs）。
// 模式与 selection.test.cjs 完全一致。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const stateTs = path.join(root, "features", "studio", "shell", "studio-state.ts");
const appTsx = path.join(root, "features", "studio", "StudioApp.tsx");

function transpile(file) {
  return ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}

// studio-state.ts 为纯 TS（无 import、无 React 依赖），transpile 后可直接执行。
const stateContext = { exports: {} };
vm.runInNewContext(transpile(stateTs), stateContext);
const {
  DEFAULT_VIEW,
  DEFAULT_SOURCE_SWITCH_MODE,
  createInitialState,
  normalizeSelectionRef,
  studioReducer,
} = stateContext.exports;

// vm realm 对象与主 realm 原型不同，deepEqual 前一律 JSON 归一化（同 selection.test.cjs）
const norm = (v) => JSON.parse(JSON.stringify(v));

function readApp() {
  return fs.readFileSync(appTsx, "utf8");
}

// ── 冻结常量与初态 ─────────────────────────────────────────────────────────

test("DEFAULT_VIEW 为 source、DEFAULT_SOURCE_SWITCH_MODE 为 reader", () => {
  assert.equal(DEFAULT_VIEW, "source");
  assert.equal(DEFAULT_SOURCE_SWITCH_MODE, "reader");
});

test("createInitialState：项目/选择为空、view=source、开关=reader、jobFeed 空", () => {
  const s = createInitialState();
  assert.deepEqual(norm(s.project), { project: null, projectId: null });
  assert.deepEqual(norm(s.selection), { ref: null });
  assert.equal(s.view.current, "source");
  assert.equal(s.sourceSwitch.mode, "reader");
  assert.equal(s.sourceSwitch.activeSourceRevisionId, null);
  assert.deepEqual(norm(s.jobFeed.entries), []);
});

test("createInitialState 每次返回新对象（无共享引用）", () => {
  const a = createInitialState();
  const b = createInitialState();
  assert.notEqual(a, b);
  assert.notEqual(a.project, b.project);
  assert.notEqual(a.jobFeed.entries, b.jobFeed.entries);
});

// ── 归一化 normalizeSelectionRef ───────────────────────────────────────────

test("normalizeSelectionRef：合法 ref 返回等值新对象", () => {
  const ref = { kind: "fragment", id: "f1", revision: 3 };
  const out = normalizeSelectionRef(ref);
  assert.deepEqual(norm(out), ref);
  assert.notEqual(out, ref);
});

test("normalizeSelectionRef：null 原样返回 null", () => {
  assert.equal(normalizeSelectionRef(null), null);
});

test("normalizeSelectionRef：空 kind / 空 id / revision 非正整数 → null", () => {
  assert.equal(normalizeSelectionRef({ kind: "", id: "f1", revision: 1 }), null);
  assert.equal(normalizeSelectionRef({ kind: "fragment", id: "", revision: 1 }), null);
  assert.equal(normalizeSelectionRef({ kind: "fragment", id: "f1", revision: 0 }), null);
  assert.equal(normalizeSelectionRef({ kind: "fragment", id: "f1", revision: -1 }), null);
  assert.equal(normalizeSelectionRef({ kind: "fragment", id: "f1", revision: 1.5 }), null);
  assert.equal(
    normalizeSelectionRef({ kind: "fragment", id: "f1", revision: null }),
    null,
  );
  assert.equal(
    normalizeSelectionRef({ kind: "fragment", id: "f1", revision: "3" }),
    null,
  );
});

// ── 项目隔离：setProject / switchProject / hydrate ─────────────────────────

function dirtyState() {
  // 构造"旧项目残留"状态：已选对象、切到 film、jobFeed 有条目
  let s = createInitialState();
  s = studioReducer(s, {
    type: "setSelection",
    ref: { kind: "fragment", id: "old", revision: 2 },
  });
  s = studioReducer(s, { type: "setView", view: "film" });
  s = studioReducer(s, { type: "setSourceSwitch", mode: "fragments" });
  s = studioReducer(s, {
    type: "pushJob",
    job: { id: "j1", status: "running", label: "旧项目任务" },
  });
  return s;
}

test("setProject：换项目后 selection/view/jobFeed 全部归初态，旧状态不残留", () => {
  const next = studioReducer(dirtyState(), { type: "setProject", projectId: "p2" });
  assert.equal(next.project.projectId, "p2");
  assert.equal(next.project.project, null);
  assert.deepEqual(norm(next.selection), { ref: null });
  assert.equal(next.view.current, DEFAULT_VIEW);
  assert.equal(next.sourceSwitch.mode, DEFAULT_SOURCE_SWITCH_MODE);
  assert.deepEqual(norm(next.jobFeed.entries), []);
});

test("setProject：projectId 传 null（invalid/清空）同样重置", () => {
  const next = studioReducer(dirtyState(), { type: "setProject", projectId: null });
  assert.equal(next.project.projectId, null);
  assert.deepEqual(norm(next.selection), { ref: null });
  assert.equal(next.view.current, DEFAULT_VIEW);
  assert.deepEqual(norm(next.jobFeed.entries), []);
});

test("switchProject：载入 DTO 且 activeSourceRevisionId 取项目活跃版本", () => {
  const project = { id: "p9", name: "九号", active_source_revision_id: "rev-9" };
  const next = studioReducer(dirtyState(), { type: "switchProject", project });
  assert.deepEqual(norm(next.project), { project, projectId: "p9" });
  assert.equal(next.sourceSwitch.activeSourceRevisionId, "rev-9");
  assert.deepEqual(norm(next.selection), { ref: null });
  assert.equal(next.view.current, DEFAULT_VIEW);
  assert.deepEqual(norm(next.jobFeed.entries), []);
});

test("switchProject：active_source_revision_id 为 null 时同步为 null", () => {
  const project = { id: "p9", name: "九号", active_source_revision_id: null };
  const next = studioReducer(createInitialState(), { type: "switchProject", project });
  assert.equal(next.sourceSwitch.activeSourceRevisionId, null);
});

test("hydrate：从服务端 DTO 重建，selectionRef 归一后载入", () => {
  const project = { id: "p7", name: "七号", active_source_revision_id: "rev-7" };
  const next = studioReducer(dirtyState(), {
    type: "hydrate",
    project,
    selectionRef: { kind: "asset", id: "a1", revision: 4 },
  });
  assert.deepEqual(norm(next.project), { project, projectId: "p7" });
  assert.equal(next.sourceSwitch.activeSourceRevisionId, "rev-7");
  assert.deepEqual(norm(next.selection), { ref: { kind: "asset", id: "a1", revision: 4 } });
  assert.equal(next.view.current, DEFAULT_VIEW);
  assert.deepEqual(norm(next.jobFeed.entries), []);
});

test("hydrate：非法 selectionRef 归 null，且不读 localStorage（源码无 localStorage）", () => {
  const project = { id: "p7", name: "七号", active_source_revision_id: null };
  const next = studioReducer(dirtyState(), {
    type: "hydrate",
    project,
    selectionRef: { kind: "", id: "", revision: 0 },
  });
  assert.deepEqual(norm(next.selection), { ref: null });
});

test("状态容器不读 localStorage（恢复一律经 hydrate 从服务端重建）", () => {
  const src = fs.readFileSync(stateTs, "utf8");
  // 注释里可能说明"不读 localStorage"，这里断言的是**真实调用**不存在：
  // 不得出现 localStorage / sessionStorage 的任何读写调用。
  assert.ok(
    !/localStorage\s*\./.test(src),
    "studio-state.ts 不得调用 localStorage 的读写方法",
  );
  assert.ok(
    !/sessionStorage\s*\./.test(src),
    "studio-state.ts 不得调用 sessionStorage 的读写方法",
  );
  assert.ok(
    !/window\.localStorage/.test(src),
    "studio-state.ts 不得访问 window.localStorage",
  );
});

// ── selection / view / 来源开关 ────────────────────────────────────────────

test("setSelection：载入合法 ref；非法 ref 归 null", () => {
  let s = studioReducer(createInitialState(), {
    type: "setSelection",
    ref: { kind: "shot", id: "s1", revision: 1 },
  });
  assert.deepEqual(norm(s.selection.ref), { kind: "shot", id: "s1", revision: 1 });
  s = studioReducer(s, { type: "setSelection", ref: { kind: "", id: "s1", revision: 1 } });
  assert.equal(s.selection.ref, null);
});

test("setView：六种视图可切换", () => {
  for (const view of ["source", "script", "storyboard", "assets", "film", "chat"]) {
    const s = studioReducer(createInitialState(), { type: "setView", view });
    assert.equal(s.view.current, view);
  }
});

test("setSourceSwitch / setActiveSourceRevision 只改来源开关态", () => {
  let s = studioReducer(createInitialState(), {
    type: "setSourceSwitch",
    mode: "fragments",
  });
  assert.equal(s.sourceSwitch.mode, "fragments");
  s = studioReducer(s, { type: "setActiveSourceRevision", revisionId: "rev-2" });
  assert.equal(s.sourceSwitch.activeSourceRevisionId, "rev-2");
  assert.equal(s.sourceSwitch.mode, "fragments");
  assert.equal(s.view.current, DEFAULT_VIEW);
});

// ── jobFeed：永不驱动视图切换 + cap ────────────────────────────────────────

test("pushJob：只进 jobFeed，永不修改 view", () => {
  let s = studioReducer(createInitialState(), { type: "setView", view: "script" });
  s = studioReducer(s, {
    type: "pushJob",
    job: { id: "j1", status: "queued", label: null },
  });
  assert.equal(s.view.current, "script", "pushJob 不得跳标签");
  assert.equal(s.jobFeed.entries.length, 1);
  assert.deepEqual(norm(s.jobFeed.entries[0]), { id: "j1", status: "queued", label: null });
});

test("updateJob：只改对应条目状态，永不修改 view", () => {
  let s = studioReducer(createInitialState(), { type: "setView", view: "assets" });
  s = studioReducer(s, { type: "pushJob", job: { id: "j1", status: "queued", label: "A" } });
  s = studioReducer(s, { type: "pushJob", job: { id: "j2", status: "queued", label: "B" } });
  s = studioReducer(s, { type: "updateJob", jobId: "j2", status: "running" });
  assert.equal(s.view.current, "assets", "updateJob 不得跳标签");
  assert.equal(s.jobFeed.entries[0].status, "queued");
  assert.equal(s.jobFeed.entries[0].label, "A");
  assert.equal(s.jobFeed.entries[1].status, "running");
  assert.equal(s.jobFeed.entries[1].label, "B");
});

test("updateJob：jobId 不存在时列表不变（无新增、无删除）", () => {
  let s = studioReducer(createInitialState(), {
    type: "pushJob",
    job: { id: "j1", status: "queued", label: null },
  });
  s = studioReducer(s, { type: "updateJob", jobId: "nope", status: "failed" });
  assert.equal(s.jobFeed.entries.length, 1);
  assert.equal(s.jobFeed.entries[0].status, "queued");
});

test("pushJob：cap=10，只保留最近 10 条", () => {
  let s = createInitialState();
  for (let i = 0; i < 13; i += 1) {
    s = studioReducer(s, {
      type: "pushJob",
      job: { id: `j${i}`, status: "queued", label: null },
    });
  }
  assert.equal(s.jobFeed.entries.length, 10);
  assert.equal(s.jobFeed.entries[0].id, "j3");
  assert.equal(s.jobFeed.entries[9].id, "j12");
});

test("clearJobFeed：清空条目，其余状态不变", () => {
  let s = studioReducer(createInitialState(), { type: "setView", view: "film" });
  s = studioReducer(s, { type: "pushJob", job: { id: "j1", status: "queued", label: null } });
  s = studioReducer(s, { type: "clearJobFeed" });
  assert.deepEqual(norm(s.jobFeed.entries), []);
  assert.equal(s.view.current, "film");
});

// ── 未知 action：返回原引用 ────────────────────────────────────────────────

test("未知 action：状态不变且返回原引用（不抛错、不新建对象）", () => {
  const before = dirtyState();
  const after = studioReducer(before, { type: "no_such_action" });
  assert.equal(after, before, "未知 action 必须返回原引用");
});

// ── StudioApp.tsx 接线与禁止项 ─────────────────────────────────────────────

test("StudioApp.tsx 用 useReducer 接管 studioReducer 与 createInitialState", () => {
  const src = readApp();
  assert.ok(src.includes("useReducer"), "必须使用 useReducer");
  assert.ok(src.includes("studioReducer"), "必须接入 studioReducer");
  assert.ok(src.includes("createInitialState"), "必须用 createInitialState 作初态");
  assert.ok(
    src.includes('from "./shell/studio-state"') ||
      src.includes("from './shell/studio-state'"),
    "必须从 shell/studio-state 导入",
  );
});

test("StudioApp.tsx：projectId prop 变化 → dispatch setProject", () => {
  const src = readApp();
  assert.ok(src.includes('type: "setProject"'), "必须 dispatch setProject");
  assert.ok(src.includes("projectId"), "必须消费 projectId prop");
  assert.ok(src.includes("useEffect"), "必须在 prop 变化时同步");
});

test("StudioApp.tsx：保留 invalid / 未选择 / 空态三分支文案", () => {
  const src = readApp();
  assert.ok(src.includes("invalid"), "保留 invalid 分支");
  assert.ok(src.includes("无效或为空"), "保留 invalid 文案");
  assert.ok(src.includes("未选择项目"), "保留未选择文案");
  assert.ok(src.includes("尚未建立内容"), "保留空态文案");
});

test("StudioApp.tsx：保留既有 DOM 结构（studio-shell）与 style jsx", () => {
  const src = readApp();
  assert.ok(src.includes('className="studio-shell"'), "保留 studio-shell 容器");
  assert.ok(src.includes("studio-shell__msg"), "保留消息样式类");
  assert.ok(src.includes("<style jsx"), "保留 <style jsx>");
});

test("StudioApp.tsx：不做网络水合（无 /api/、无 fetch(、不 import app/）", () => {
  const src = readApp();
  assert.ok(!src.includes('"/api/'), "不得引用 /api/");
  assert.ok(!src.includes("fetch("), "不得调用 fetch(");
  assert.ok(!src.includes('from "app/'), "不得 import app/");
  assert.ok(!src.includes("from '../app/"), "不得 import ../app/");
  assert.ok(!src.includes('from "../../app/'), "不得 import ../../app/");
});

test("StudioApp.tsx：不引用旧产品（ProjectHome / TaskActivity / reader）", () => {
  const src = readApp();
  assert.ok(
    !/ProjectHome|TaskActivity|reader/.test(src),
    "不得引用 ProjectHome、TaskActivity 或 reader",
  );
});
