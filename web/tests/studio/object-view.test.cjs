const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// VIEW-03 验收：统一选择与对象属性（shell/object-view.ts，vm+transpile 纯逻辑）
// + ObjectInspector.tsx 字符串断言（受控 / 空态 / 禁止项，照 studio-app.test.cjs）。
// 模式与 state.test.cjs、selection.test.cjs 完全一致。
//
// 主模型裁定：不放宽 VIEW-01 冻结的 ObjectRef / normalizeSelectionRef；
// 宽松选择（revision 可为 null）全部落在 object-view.ts，studio-state.ts 零改动。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const objectViewTs = path.join(root, "features", "studio", "shell", "object-view.ts");
const stateTs = path.join(root, "features", "studio", "shell", "studio-state.ts");
const inspectorTsx = path.join(
  root,
  "features",
  "studio",
  "shell",
  "components",
  "ObjectInspector.tsx",
);

function transpile(file) {
  return ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}

// object-view.ts 为纯 TS（连 type-only import 都没有），transpile 后可直接执行。
const viewContext = { exports: {} };
vm.runInNewContext(transpile(objectViewTs), viewContext);
const {
  EDIT_INSTANCE_KIND,
  deriveObjectView,
  instanceToSelection,
  isSelectableObjectRef,
  kindLabel,
  nodeToSelection,
  normalizeSelection,
  objectKey,
  sameObject,
  sameObjectRevision,
} = viewContext.exports;

// StudioState 冻结导出（回归护栏用）。
const stateContext = { exports: {} };
vm.runInNewContext(transpile(stateTs), stateContext);
const { normalizeSelectionRef } = stateContext.exports;

// vm realm 对象与主 realm 原型不同，deepEqual 前一律 JSON 归一化
// （同 state.test.cjs / selection.test.cjs）。
const norm = (v) => JSON.parse(JSON.stringify(v));

// ── normalizeSelection：宽松但严格的形状 ──────────────────────────────────

test("normalizeSelection：合法（正整数 revision / revision:null）保留，返回等值新对象", () => {
  const ref = { kind: "fragment", id: "f1", revision: 3 };
  const out = normalizeSelection(ref);
  assert.deepEqual(norm(out), ref);
  assert.notEqual(out, ref, "必须返回新对象，不共享引用");

  const noRevision = { kind: "scene", id: "sc1", revision: null };
  const outNoRevision = normalizeSelection(noRevision);
  assert.deepEqual(norm(outNoRevision), noRevision, "revision:null 必须保留（场无版本）");
  assert.notEqual(outNoRevision, noRevision);
});

test("normalizeSelection：null / 非对象形状 → null（不抛错）", () => {
  assert.equal(normalizeSelection(null), null);
  assert.equal(normalizeSelection(undefined), null);
  assert.equal(normalizeSelection("fragment"), null);
  assert.equal(normalizeSelection([]), null);
});

test("normalizeSelection：空 kind / 空 id / revision 0 / -1 / 1.5 / \"1\" / undefined → null", () => {
  assert.equal(normalizeSelection({ kind: "", id: "f1", revision: 1 }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "", revision: 1 }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "f1", revision: 0 }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "f1", revision: -1 }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "f1", revision: 1.5 }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "f1", revision: "1" }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "f1", revision: undefined }), null);
  assert.equal(normalizeSelection({ kind: "fragment", id: "f1", revision: NaN }), null);
});

test("isSelectableObjectRef：形状门禁（revision:null 可选中；缺 revision / 非法 revision 不可）", () => {
  assert.equal(isSelectableObjectRef({ kind: "scene", id: "sc1", revision: null }), true);
  assert.equal(isSelectableObjectRef({ kind: "shot", id: "sh1", revision: 2 }), true);
  // 冻结 ObjectRef（正整数 revision）是 SelectionRef 的子集，同样可选中
  assert.equal(isSelectableObjectRef({ kind: "fragment", id: "f1", revision: 1 }), true);

  assert.equal(isSelectableObjectRef(null), false);
  assert.equal(isSelectableObjectRef(undefined), false);
  assert.equal(isSelectableObjectRef({ kind: "", id: "sc1", revision: null }), false);
  assert.equal(isSelectableObjectRef({ kind: "scene", id: "", revision: null }), false);
  assert.equal(isSelectableObjectRef({ kind: "scene", id: "sc1" }), false);
  assert.equal(isSelectableObjectRef({ kind: "scene", id: "sc1", revision: 0 }), false);
  assert.equal(isSelectableObjectRef({ kind: "scene", id: "sc1", revision: 1.5 }), false);
  assert.equal(isSelectableObjectRef("scene"), false);
});

// ── 高亮 / 属性 / 出处指同一 revision ─────────────────────────────────────

test("deriveObjectView：object_ref.revision 恒等于入参 revision（不升级到来源最新版）", () => {
  const view = deriveObjectView({ kind: "shot", id: "sh-1", revision: 3 }, [
    {
      object_ref: { kind: "shot", id: "sh-1", revision: 3 },
      kind_label: "镜头",
      title: "镜头甲",
      parent: { kind: "scene", id: "sc-1", revision: null },
      provenance: [
        { kind: "script_adoption", id: "ad-1", revision: 2 },
        { kind: "fragment", id: "f-1", revision: 1 },
      ],
      version_label: "草稿",
      updated_at: "2026-10-15T00:00:00Z",
      state: "candidate",
      fields: [
        { key: "visual", label: "画面", value: "雨夜街头" },
        { key: "dialogue", value: null },
      ],
    },
  ]);

  assert.equal(view.object_ref.revision, 3, "object_ref.revision 必须等于入参 revision");
  assert.deepEqual(norm(view.object_ref), { kind: "shot", id: "sh-1", revision: 3 });
  assert.equal(sameObjectRevision(view.object_ref, { kind: "shot", id: "sh-1", revision: 3 }), true);
  assert.equal(sameObject(view.object_ref, { kind: "shot", id: "sh-1", revision: 9 }), true);
  assert.equal(
    sameObjectRevision(view.object_ref, { kind: "shot", id: "sh-1", revision: 9 }),
    false,
  );

  assert.equal(view.kind_label, "镜头");
  assert.equal(view.title, "镜头甲");
  assert.equal(view.available, true);
  assert.deepEqual(norm(view.version), {
    revision: 3,
    label: "草稿",
    updated_at: "2026-10-15T00:00:00Z",
  });
  assert.equal(view.state, "candidate");
  // 归属 / 出处都描述该 revision 的真实结构
  assert.deepEqual(norm(view.attribution), { kind: "scene", id: "sc-1", revision: null });
  assert.deepEqual(
    norm(view.provenance.map((entry) => entry.id)),
    ["ad-1", "f-1"],
  );
  assert.deepEqual(
    norm(view.fields),
    [
      { key: "visual", label: "画面", value: "雨夜街头" },
      { key: "dialogue", label: "dialogue", value: null },
    ],
  );
});

test("deriveObjectView：来源 revision 与入参不一致 → 不采用其字段（不冒充别的 revision）", () => {
  const view = deriveObjectView({ kind: "shot", id: "sh-1", revision: 3 }, [
    {
      object_ref: { kind: "shot", id: "sh-1", revision: 9 },
      kind_label: "镜头",
      title: "第九版标题",
      parent: { kind: "scene", id: "sc-1" },
      provenance: [{ kind: "script_adoption", id: "ad-9" }],
      state: "confirmed",
      fields: [{ key: "visual", value: "第九版画面" }],
    },
  ]);

  assert.equal(view.object_ref.revision, 3);
  assert.equal(view.available, false, "来源 revision 不一致必须标为不可用");
  assert.equal(view.title, "", "不得拿别的 revision 的标题冒充");
  assert.equal(view.attribution, null);
  assert.deepEqual(norm(view.provenance), []);
  assert.deepEqual(norm(view.fields), []);
  assert.equal(view.state, null);
  assert.equal(view.version.label, null);
});

test("deriveObjectView：来源缺失时给保守占位而非编造（kind_label 退化、title 空、字段空）", () => {
  const view = deriveObjectView({ kind: "scene", id: "sc-1", revision: null }, []);

  assert.deepEqual(norm(view.object_ref), { kind: "scene", id: "sc-1", revision: null });
  assert.equal(view.available, false);
  assert.equal(view.kind_label, kindLabel("scene"));
  assert.equal(view.title, "", "无来源时不得编造标题");
  assert.equal(view.attribution, null);
  assert.deepEqual(norm(view.provenance), []);
  assert.deepEqual(norm(view.fields), []);
  assert.equal(view.state, null);
  assert.deepEqual(norm(view.version), { revision: null, label: null, updated_at: null });
  // 未知 kind 退化为 kind 原文，不编造业务含义
  assert.equal(kindLabel("unknown_kind"), "unknown_kind");
});

test("deriveObjectView：空白/非法选择 → null（清空，不抛错）", () => {
  assert.equal(deriveObjectView(null, []), null);
  assert.equal(deriveObjectView({ kind: "", id: "sc-1", revision: null }, []), null);
  assert.equal(deriveObjectView({ kind: "scene", id: "", revision: null }, []), null);
  assert.equal(deriveObjectView({ kind: "shot", id: "sh-1", revision: 0 }, []), null);
  assert.equal(deriveObjectView({ kind: "shot", id: "sh-1", revision: 1.5 }, []), null);
});

// ── 不把 group 标题当归属 ──────────────────────────────────────────────────

test("attribution：只有 group_title / 展示分组、没有真实父级 id → null（不编造）", () => {
  const view = deriveObjectView({ kind: "shot", id: "sh-1", revision: 1 }, [
    {
      object_ref: { kind: "shot", id: "sh-1", revision: 1 },
      title: "镜头甲",
      // 展示用分组标题（画布分组名）：绝不能当作归属
      group_title: "第一场",
      group: "场组 A",
      parent: null,
    },
  ]);
  assert.equal(view.attribution, null, "分组标题不是归属");

  // 父级对象存在但 id 为空 → 同样不构成真实归属
  const emptyParent = deriveObjectView({ kind: "shot", id: "sh-1", revision: 1 }, [
    {
      object_ref: { kind: "shot", id: "sh-1", revision: 1 },
      group_title: "第一场",
      parent: { kind: "scene", id: "" },
    },
  ]);
  assert.equal(emptyParent.attribution, null);
});

test("attribution：真实父级 id 优先，且不等于 group 标题", () => {
  const view = deriveObjectView({ kind: "shot", id: "sh-1", revision: 1 }, [
    {
      object_ref: { kind: "shot", id: "sh-1", revision: 1 },
      title: "镜头甲",
      group_title: "第一场",
      parent: { kind: "scene", id: "sc-7" },
    },
  ]);
  assert.deepEqual(norm(view.attribution), { kind: "scene", id: "sc-7", revision: null });
  assert.equal(view.attribution.id, "sc-7");
  assert.notEqual(view.attribution.id, "第一场");
  assert.notEqual(view.attribution.kind, "group");
});

// ── 资源与实例不混淆 ───────────────────────────────────────────────────────

test("nodeToSelection：画布节点保留资源身份（含 revision:null），无真实身份 → null", () => {
  assert.deepEqual(
    norm(nodeToSelection({ object_ref: { kind: "asset", id: "a-1", revision: 2 } })),
    { kind: "asset", id: "a-1", revision: 2 },
  );
  assert.deepEqual(
    norm(nodeToSelection({ object_ref: { kind: "scene", id: "sc-1", revision: null } })),
    { kind: "scene", id: "sc-1", revision: null },
  );
  // 只有展示 kind/label、没有 object_ref → 不得伪造选择
  assert.equal(nodeToSelection({ kind: "asset", label: "资产甲" }), null);
  assert.equal(nodeToSelection(null), null);
  assert.equal(nodeToSelection(undefined), null);
});

test("instanceToSelection：kind 恒为 edit_instance，媒体资源只算出处不作选择身份", () => {
  const sel = instanceToSelection({
    id: "i-1",
    instance_id: "i-1",
    media_id: "m-1",
    object_ref: { kind: "media_artifact", id: "m-1", revision: 5 },
  });
  assert.deepEqual(norm(sel), { kind: EDIT_INSTANCE_KIND, id: "i-1", revision: null });
  assert.equal(sel.kind, "edit_instance");
  assert.notEqual(sel.kind, "media_artifact");
  assert.notEqual(sel.id, "m-1", "不得选成实例引用的资源");

  // 实例自身 ref 明确是 edit_instance 时采信之
  assert.deepEqual(
    norm(instanceToSelection({ object_ref: { kind: "edit_instance", id: "i-9", revision: 4 } })),
    { kind: "edit_instance", id: "i-9", revision: 4 },
  );

  assert.equal(instanceToSelection(null), null);
  assert.equal(instanceToSelection({ media_id: "m-1" }), null, "只有资源 id 时不得伪造实例选择");
});

test("nodeToSelection 与 instanceToSelection：同类输入可区分且都满足 normalizeSelection", () => {
  const nodeSel = nodeToSelection({ object_ref: { kind: "asset", id: "a-1", revision: 2 } });
  const instanceSel = instanceToSelection({ id: "i-1", media_id: "a-1" });

  assert.notEqual(nodeSel.kind, instanceSel.kind, "资源与实例的 kind 必须可区分");
  assert.equal(instanceSel.kind, EDIT_INSTANCE_KIND);
  assert.notDeepEqual(norm(nodeSel), norm(instanceSel));
  // 两者都是合法选择（归一后逐字不变、非 null）
  assert.deepEqual(norm(normalizeSelection(nodeSel)), norm(nodeSel));
  assert.deepEqual(norm(normalizeSelection(instanceSel)), norm(instanceSel));
  assert.equal(objectKey(nodeSel), "asset\u0000a-1");
  assert.equal(objectKey(instanceSel), "edit_instance\u0000i-1");
});

// ── object-view.ts 纯洁性 ──────────────────────────────────────────────────

test("object-view.ts：纯逻辑（无 React/DOM/网络/存储），vm 可直接加载", () => {
  const src = fs.readFileSync(objectViewTs, "utf8");
  assert.ok(!src.includes("fetch("), "纯逻辑不得调用 fetch(");
  assert.ok(!src.includes("/api/"), "纯逻辑不得引用 /api/");
  assert.ok(!src.includes("localStorage"), "纯逻辑不得读写 localStorage");
  assert.ok(!src.includes("sessionStorage"), "纯逻辑不得读写 sessionStorage");
  assert.ok(!/\bdocument\./.test(src), "纯逻辑不得触碰 DOM");
  assert.ok(!/\bwindow\./.test(src), "纯逻辑不得触碰 window");
  assert.ok(!/from\s+["']react["']/.test(src), "纯逻辑不得依赖 react");
  assert.ok(!/^\s*import\s/m.test(src), "纯逻辑不得有运行时 import");
});

// ── ObjectInspector.tsx：受控 / 空态 / 禁止项 ─────────────────────────────

const inspectorSrc = fs.readFileSync(inspectorTsx, "utf8");

test("ObjectInspector：受控（use client，无 useState/useEffect/存储/网络）", () => {
  assert.ok(inspectorSrc.startsWith('"use client"'), "必须有 use client 头");
  assert.ok(inspectorSrc.includes("ObjectView"), "必须消费 ObjectView 视图模型");
  assert.ok(inspectorSrc.includes("view: ObjectView | null"), "view 必须是受控 prop");
  assert.ok(inspectorSrc.includes("onClear"), "清除选择必须经 onClear 上抛");
  assert.ok(!inspectorSrc.includes("useState"), "不得用 useState 存业务数据");
  assert.ok(!inspectorSrc.includes("useEffect"), "受控组件不得自行取数");
  assert.ok(!inspectorSrc.includes("localStorage"), "不得读写 localStorage");
  assert.ok(!inspectorSrc.includes("sessionStorage"), "不得读写 sessionStorage");
});

test("ObjectInspector：view === null 的空态分支 + 无伪造字段常量", () => {
  assert.ok(inspectorSrc.includes("if (view === null)"), "必须有 view === null 空态分支");
  assert.ok(inspectorSrc.includes("object-inspector--empty"), "空态须有独立样式类");
  assert.ok(inspectorSrc.includes("未选择对象"), "空态须有明确文案");
  assert.ok(!/示例|样例|Lorem/.test(inspectorSrc), "不得内置伪造样例字段");
  // 空态分支必须在业务渲染之前 return（不会落入字段渲染）
  const emptyIndex = inspectorSrc.indexOf("if (view === null)");
  const refIndex = inspectorSrc.indexOf("const ref = view.object_ref");
  assert.ok(emptyIndex !== -1 && refIndex !== -1 && emptyIndex < refIndex, "空态必须早退");
});

test("ObjectInspector：禁止项（无 fetch、无 /api/、不 import app/、不引用旧产品）", () => {
  assert.ok(!inspectorSrc.includes('"/api/'), "不得引用 /api/");
  assert.ok(!inspectorSrc.includes("fetch("), "不得调用 fetch(");
  assert.ok(!inspectorSrc.includes('from "app/'), "不得 import app/");
  assert.ok(!inspectorSrc.includes("from '../app/"), "不得 import ../app/");
  assert.ok(!inspectorSrc.includes('from "../../app/'), "不得 import ../../app/");
  assert.ok(
    !/ProjectHome|TaskActivity|reader/.test(inspectorSrc),
    "不得引用旧产品（ProjectHome / TaskActivity / reader）",
  );
  assert.ok(inspectorSrc.includes("<style jsx"), "保留 <style jsx 局部样式");
  assert.ok(inspectorSrc.includes("归属"), "必须渲染归属");
  assert.ok(inspectorSrc.includes("出处"), "必须渲染出处");
  assert.ok(inspectorSrc.includes("版本"), "必须渲染版本");
  assert.ok(inspectorSrc.includes("状态"), "必须渲染状态");
});

// ── 回归护栏：VIEW-01 冻结语义零放宽 ──────────────────────────────────────

test("回归护栏：studio-state.ts 的 ObjectRef 语义未被放宽（revision:null 仍归 null）", () => {
  // 冻结：revision 必须是正整数，否则整条归 null
  assert.equal(normalizeSelectionRef({ kind: "scene", id: "sc-1", revision: null }), null);
  assert.equal(normalizeSelectionRef({ kind: "shot", id: "sh-1", revision: 0 }), null);
  assert.equal(normalizeSelectionRef({ kind: "shot", id: "sh-1", revision: 1.5 }), null);
  assert.deepEqual(norm(normalizeSelectionRef({ kind: "shot", id: "sh-1", revision: 2 })), {
    kind: "shot",
    id: "sh-1",
    revision: 2,
  });
  // 本卡的宽松选择并存且互不污染（同一 revision:null 输入，两套语义各自成立）
  assert.deepEqual(norm(normalizeSelection({ kind: "scene", id: "sc-1", revision: null })), {
    kind: "scene",
    id: "sc-1",
    revision: null,
  });
});
