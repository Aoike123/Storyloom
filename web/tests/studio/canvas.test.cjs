const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// VIEW-02 验收：受控无限画布
//   · canvas-nodes.ts 纯逻辑（vm + transpile，模式同 state.test.cjs/selection.test.cjs）
//   · InfiniteCanvas.tsx 受控组件字符串断言（禁止项照 studio-app.test.cjs）
// vm realm 对象与主 realm 原型不同，deepEqual 前一律 JSON 归一化。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const nodesTs = path.join(root, "features", "studio", "canvas", "canvas-nodes.ts");
const canvasTsx = path.join(
  root,
  "features",
  "studio",
  "canvas",
  "components",
  "InfiniteCanvas.tsx",
);

function transpile(file) {
  return ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}

// canvas-nodes.ts 的 ObjectRef 为 type-only import，转译后必须无运行时 require。
const nodesSource = fs.readFileSync(nodesTs, "utf8");
const nodesOutput = transpile(nodesTs);
const nodesContext = { exports: {} };
vm.runInNewContext(nodesOutput, nodesContext);
const {
  DEFAULT_POSITION,
  MIN_ZOOM,
  MAX_ZOOM,
  NODE_WIDTH,
  NODE_HEIGHT,
  FIT_PADDING,
  nodeKey,
  deriveNodes,
  upsertNode,
  removeNode,
  updateNodePosition,
  updateLayoutPosition,
  removeLayoutPosition,
  identityViewport,
  contentToScreen,
  screenToContent,
  panBy,
  zoomAt,
  fitToNodes,
} = nodesContext.exports;

const norm = (v) => JSON.parse(JSON.stringify(v));

function approx(actual, expected, eps = 1e-9, label = "") {
  assert.ok(
    Number.isFinite(actual) && Math.abs(actual - expected) <= eps,
    `${label} 期望 ≈ ${expected}，实际 ${actual}`,
  );
}

function assertFiniteNumbers(value, label) {
  for (const [k, v] of Object.entries(value)) {
    assert.ok(Number.isFinite(v), `${label}.${k} 必须为有限数字，实际 ${v}`);
  }
}

// ── 真实来源对象 fixture（fragment/shot/asset + 一个场景） ───────────────────

const FRAG_A = {
  object_ref: { kind: "fragment", id: "frag-a", revision: 1 },
  kind: "fragment",
  label: "片段甲",
};
// kind 省略 → 取 object_ref.kind
const SHOT_B = {
  object_ref: { kind: "shot", id: "shot-b", revision: 2 },
  label: "镜头乙",
};
const ASSET_C = {
  object_ref: { kind: "asset", id: "asset-c", revision: 5 },
  label: null,
};
const SCENE_D = {
  object_ref: { kind: "scene", id: "scene-d", revision: 1 },
  kind: "scene",
  label: "场景丁",
};

const SOURCES = [FRAG_A, SHOT_B, ASSET_C, SCENE_D];

const LAYOUT = updateLayoutPosition(
  updateLayoutPosition(
    updateLayoutPosition({}, FRAG_A.object_ref, 100, 50),
    SHOT_B.object_ref,
    -20,
    300,
  ),
  ASSET_C.object_ref,
  640,
  -12,
);

const byKey = (nodes, ref) => nodes.find((n) => nodeKey(n.object_ref) === nodeKey(ref));

// ── 空输入 / 不伪造样例数据 ────────────────────────────────────────────────

test("deriveNodes：没有来源对象就没有节点（空输入 → 空数组，不伪造样例数据）", () => {
  assert.deepEqual(norm(deriveNodes([], {})), []);
  assert.deepEqual(norm(deriveNodes([], LAYOUT)), []);
  assert.deepEqual(norm(deriveNodes(null, null)), []);
  assert.deepEqual(norm(deriveNodes(undefined, undefined)), []);
  // 有来源才有节点：数量恰等于来源数量
  assert.equal(deriveNodes(SOURCES, LAYOUT).length, SOURCES.length);
});

test("deriveNodes：字段投影（label 缺失为空串、kind 缺失取 ref.kind、revision 原样透传）", () => {
  const nodes = deriveNodes(SOURCES, LAYOUT);
  const frag = byKey(nodes, FRAG_A.object_ref);
  const shot = byKey(nodes, SHOT_B.object_ref);
  const asset = byKey(nodes, ASSET_C.object_ref);
  assert.deepEqual(norm(frag), {
    object_ref: { kind: "fragment", id: "frag-a", revision: 1 },
    kind: "fragment",
    label: "片段甲",
    x: 100,
    y: 50,
  });
  assert.equal(shot.kind, "shot");
  assert.equal(asset.label, "");
});

test("deriveNodes：身份非法（缺 id / revision 非有限数字）的来源被跳过，不伪造节点", () => {
  assert.deepEqual(norm(deriveNodes([{ object_ref: { kind: "fragment", id: "", revision: 1 } }], {})), []);
  assert.deepEqual(norm(deriveNodes([{ object_ref: { kind: "", id: "x", revision: 1 } }], {})), []);
  assert.deepEqual(
    norm(deriveNodes([{ object_ref: { kind: "fragment", id: "x", revision: NaN } }], {})),
    [],
  );
  assert.equal(deriveNodes([null, FRAG_A], {}).length, 1);
});

// ── 纯函数性 ───────────────────────────────────────────────────────────────

test("deriveNodes：同输入两次调用结果相等，且互不共享引用（纯函数、无缓存）", () => {
  const first = deriveNodes(SOURCES, LAYOUT);
  const second = deriveNodes(SOURCES, LAYOUT);
  assert.deepEqual(norm(first), norm(second));
  assert.notEqual(first, second, "两次派生不得返回同一数组");
  assert.notEqual(first[0], second[0], "节点对象不得共享引用");
  assert.notEqual(first[0].object_ref, second[0].object_ref, "object_ref 不得共享引用");

  // 改动第一次结果不影响第二次结果，也不影响来源对象
  first[0].x = 99999;
  first[0].label = "被改了";
  assert.deepEqual(norm(second), norm(deriveNodes(SOURCES, LAYOUT)));
  assert.equal(SOURCES[0].label, "片段甲");
  assert.equal(SOURCES[0].object_ref.revision, 1);
});

test("deriveNodes：不改来源对象、不改布局状态、不改来源顺序", () => {
  const sourcesBefore = norm(SOURCES);
  const layoutBefore = norm(LAYOUT);
  deriveNodes(SOURCES, LAYOUT);
  assert.deepEqual(norm(SOURCES), sourcesBefore, "来源对象逐字段不得变化（含 revision）");
  assert.deepEqual(norm(LAYOUT), layoutBefore, "布局状态不得被派生修改");
  assert.deepEqual(
    SOURCES.map((s) => s.object_ref.id),
    ["frag-a", "shot-b", "asset-c", "scene-d"],
    "来源顺序不得变化",
  );
});

// ── 异步读入：新对象立即出现 ───────────────────────────────────────────────

test("异步读入：来源追加一个对象后再派生 → 恰好多一个对应节点，且既有节点不受影响", () => {
  const fresh = {
    object_ref: { kind: "shot", id: "shot-new", revision: 1 },
    kind: "shot",
    label: "新镜头",
  };
  const before = deriveNodes(SOURCES, LAYOUT);
  const after = deriveNodes([...SOURCES, fresh], LAYOUT);

  assert.equal(after.length, before.length + 1, "追加一个来源须恰好多一个节点");
  assert.deepEqual(norm(byKey(after, fresh.object_ref)), {
    object_ref: { kind: "shot", id: "shot-new", revision: 1 },
    kind: "shot",
    label: "新镜头",
    x: DEFAULT_POSITION.x,
    y: DEFAULT_POSITION.y,
  });
  // 既有节点坐标/字段不回退
  for (const node of before) {
    assert.deepEqual(norm(byKey(after, node.object_ref)), norm(node));
  }
  // 无缓存陈旧：再次对原输入派生，结果仍是原样（新对象不属于它）
  assert.deepEqual(norm(deriveNodes(SOURCES, LAYOUT)), norm(before));
});

test("来源移除：消失的对象不再产出节点，其余节点坐标不变", () => {
  const remaining = SOURCES.filter((s) => s.object_ref.id !== "shot-b");
  const nodes = deriveNodes(remaining, LAYOUT);
  assert.equal(nodes.length, SOURCES.length - 1);
  assert.equal(byKey(nodes, SHOT_B.object_ref), undefined);
  assert.equal(byKey(nodes, FRAG_A.object_ref).x, 100);
});

// ── 展示字段同步，但不覆盖已持久化坐标 ─────────────────────────────────────

test("字段同步：label/kind/revision 变化同步到节点，但已持久化 x/y 不被覆盖", () => {
  const renamed = {
    object_ref: { kind: "fragment", id: "frag-a", revision: 7 },
    kind: "fragment",
    label: "片段甲（改）",
  };
  const node = byKey(deriveNodes([renamed, SHOT_B], LAYOUT), renamed.object_ref);
  assert.equal(node.label, "片段甲（改）");
  assert.equal(node.object_ref.revision, 7, "revision 随来源同步");
  assert.deepEqual(
    { x: node.x, y: node.y },
    { x: 100, y: 50 },
    "升版/改名不得重置坐标",
  );
});

test("坐标键不含 revision：仅 revision 变化时坐标保持", () => {
  const bumped = { ...FRAG_A, object_ref: { ...FRAG_A.object_ref, revision: 42 } };
  assert.equal(nodeKey(FRAG_A.object_ref), nodeKey(bumped.object_ref));
  const node = byKey(deriveNodes([bumped], LAYOUT), FRAG_A.object_ref);
  assert.deepEqual({ x: node.x, y: node.y }, { x: 100, y: 50 });
});

// ── 位置独立于父级/顺序/下标 ───────────────────────────────────────────────

test("位置独立：同一 object_ref 的坐标只由布局状态决定，不随来源下标/顺序变化", () => {
  const forward = deriveNodes([FRAG_A, SHOT_B, ASSET_C], LAYOUT);
  const backward = deriveNodes([ASSET_C, SHOT_B, FRAG_A], LAYOUT);

  for (const source of [FRAG_A, SHOT_B, ASSET_C]) {
    const a = byKey(forward, source.object_ref);
    const b = byKey(backward, source.object_ref);
    assert.deepEqual({ x: a.x, y: a.y }, { x: b.x, y: b.y }, "坐标不得随顺序变化");
  }
  // 下标不同但坐标相同（frag-a 在下标 0 与 2）
  assert.equal(forward[0].object_ref.id, "frag-a");
  assert.equal(backward[2].object_ref.id, "frag-a");
  assert.deepEqual({ x: forward[0].x, y: forward[0].y }, { x: backward[2].x, y: backward[2].y });

  // 无布局记录时一律落默认原点（不由下标推导出递增/网格坐标）
  const noLayout = deriveNodes([FRAG_A, SHOT_B, ASSET_C], {});
  noLayout.forEach((node, index) => {
    assert.deepEqual(
      { x: node.x, y: node.y },
      { x: DEFAULT_POSITION.x, y: DEFAULT_POSITION.y },
      `下标 ${index} 的节点坐标须为默认原点（不得按下标布局）`,
    );
  });
});

// ── 受控增删改（纯函数） ───────────────────────────────────────────────────

test("updateNodePosition：返回新数组、只改命中节点、其余节点保持原引用、输入不被 mutate", () => {
  const nodes = deriveNodes(SOURCES, LAYOUT);
  const nodesBefore = norm(nodes);
  const moved = updateNodePosition(nodes, FRAG_A.object_ref, 111, 222);

  assert.notEqual(moved, nodes, "须返回新数组");
  assert.deepEqual(norm(byKey(moved, FRAG_A.object_ref)), {
    object_ref: { kind: "fragment", id: "frag-a", revision: 1 },
    kind: "fragment",
    label: "片段甲",
    x: 111,
    y: 222,
  });
  assert.equal(moved[1], nodes[1], "未命中节点须保持原引用");
  assert.equal(moved[2], nodes[2], "未命中节点须保持原引用");
  assert.deepEqual(norm(nodes), nodesBefore, "输入数组不得被原地修改");

  // 坐标未变化 / 未命中 / 非法坐标 → 原数组引用
  assert.equal(updateNodePosition(nodes, FRAG_A.object_ref, 100, 50), nodes);
  assert.equal(updateNodePosition(nodes, { kind: "scene", id: "nope", revision: 1 }, 1, 1), nodes);
  assert.equal(updateNodePosition(nodes, FRAG_A.object_ref, NaN, 1), nodes);
  assert.equal(updateNodePosition(nodes, FRAG_A.object_ref, 1, Infinity), nodes);
});

test("拖后更新不复位：连续更新后再次派生/再次更新，坐标保持最新值不回退", () => {
  // 组件受控闭环：拖动 → onMoveNode → 调用方更新布局 → 再派生
  let layout = updateLayoutPosition({}, FRAG_A.object_ref, 0, 0);
  const first = deriveNodes(SOURCES, layout);
  assert.deepEqual({ x: byKey(first, FRAG_A.object_ref).x, y: byKey(first, FRAG_A.object_ref).y }, { x: 0, y: 0 });

  layout = updateLayoutPosition(layout, FRAG_A.object_ref, 180, 90);
  const second = deriveNodes(SOURCES, layout);
  assert.deepEqual({ x: byKey(second, FRAG_A.object_ref).x, y: byKey(second, FRAG_A.object_ref).y }, { x: 180, y: 90 });

  // 再次传入同一 props（含字段同步/升版）也不回退
  const bumped = SOURCES.map((s) =>
    s.object_ref.id === "frag-a"
      ? { ...s, label: "改", object_ref: { ...s.object_ref, revision: 9 } }
      : s,
  );
  const third = deriveNodes(bumped, layout);
  assert.deepEqual({ x: byKey(third, FRAG_A.object_ref).x, y: byKey(third, FRAG_A.object_ref).y }, { x: 180, y: 90 });
  assert.equal(byKey(third, FRAG_A.object_ref).label, "改");

  // 纯函数路径同样保持：拖后再次传入同一 nodes props → 不回退到旧坐标
  const nodes = deriveNodes(SOURCES, layout);
  const dragged = updateNodePosition(nodes, SHOT_B.object_ref, 7, 8);
  const after = updateNodePosition(dragged, ASSET_C.object_ref, 9, 10);
  assert.deepEqual({ x: byKey(after, SHOT_B.object_ref).x, y: byKey(after, SHOT_B.object_ref).y }, { x: 7, y: 8 });
  assert.deepEqual({ x: byKey(after, FRAG_A.object_ref).x, y: byKey(after, FRAG_A.object_ref).y }, { x: 180, y: 90 });
});

test("坐标不改父级/顺序/revision：更新前后来源对象数组逐字段深相等且原输入未被 mutate", () => {
  const sourcesBefore = norm(SOURCES);
  const layoutBefore = norm(LAYOUT);
  const nodes = deriveNodes(SOURCES, LAYOUT);
  const nodesBefore = norm(nodes);

  const moved = updateNodePosition(nodes, FRAG_A.object_ref, 55, 66);
  updateNodePosition(moved, SHOT_B.object_ref, -1, -2);
  updateLayoutPosition(LAYOUT, FRAG_A.object_ref, 55, 66);
  removeLayoutPosition(LAYOUT, SHOT_B.object_ref);

  assert.deepEqual(norm(SOURCES), sourcesBefore, "来源对象须逐字段深相等（含 revision 与顺序）");
  assert.deepEqual(
    SOURCES.map((s) => [s.object_ref.kind, s.object_ref.id, s.object_ref.revision]),
    [
      ["fragment", "frag-a", 1],
      ["shot", "shot-b", 2],
      ["asset", "asset-c", 5],
      ["scene", "scene-d", 1],
    ],
    "顺序与 revision 不得被触碰",
  );
  assert.deepEqual(norm(LAYOUT), layoutBefore, "布局状态不得被原地修改");
  assert.deepEqual(norm(nodes), nodesBefore, "节点输入不得被原地修改");
});

test("冻结输入下同样工作（实现不得依赖原地写入）", () => {
  const frozenSources = SOURCES.map((s) =>
    Object.freeze({ ...s, object_ref: Object.freeze({ ...s.object_ref }) }),
  );
  const frozenLayout = Object.freeze(
    Object.fromEntries(Object.entries(LAYOUT).map(([k, v]) => [k, Object.freeze({ ...v })])),
  );
  const nodes = deriveNodes(frozenSources, frozenLayout);
  assert.equal(nodes.length, 4);
  assert.equal(
    updateNodePosition(nodes, FRAG_A.object_ref, 3, 4).length,
    4,
  );
  assert.equal(upsertNode(nodes, SHOT_B).length, 4);
  assert.equal(removeNode(nodes, SHOT_B.object_ref).length, 3);
  assert.equal(Object.keys(updateLayoutPosition(frozenLayout, FRAG_A.object_ref, 1, 2)).length, 3);
  assert.equal(Object.keys(removeLayoutPosition(frozenLayout, FRAG_A.object_ref)).length, 2);
});

test("upsertNode：新增落 DEFAULT_POSITION（或显式 position）、已有则同步字段并保留坐标、不重复", () => {
  const base = deriveNodes([FRAG_A], updateLayoutPosition({}, FRAG_A.object_ref, 5, 6));
  const added = upsertNode(base, SHOT_B);
  assert.equal(added.length, 2);
  assert.deepEqual(
    { x: byKey(added, SHOT_B.object_ref).x, y: byKey(added, SHOT_B.object_ref).y },
    { x: DEFAULT_POSITION.x, y: DEFAULT_POSITION.y },
  );

  const placed = upsertNode(base, SHOT_B, { x: 30, y: 40 });
  assert.deepEqual(
    { x: byKey(placed, SHOT_B.object_ref).x, y: byKey(placed, SHOT_B.object_ref).y },
    { x: 30, y: 40 },
  );

  const again = upsertNode(added, SHOT_B);
  assert.equal(again.length, 2, "同一 object_ref 不得重复插入");

  const synced = upsertNode(added, { ...FRAG_A, label: "新标签" });
  assert.equal(byKey(synced, FRAG_A.object_ref).label, "新标签");
  assert.deepEqual(
    { x: byKey(synced, FRAG_A.object_ref).x, y: byKey(synced, FRAG_A.object_ref).y },
    { x: 5, y: 6 },
    "字段同步不得覆盖已持久化坐标",
  );
  // 显式 position 也不覆盖已有坐标
  const syncedWithPosition = upsertNode(added, FRAG_A, { x: 999, y: 999 });
  assert.deepEqual(
    { x: byKey(syncedWithPosition, FRAG_A.object_ref).x, y: byKey(syncedWithPosition, FRAG_A.object_ref).y },
    { x: 5, y: 6 },
  );

  assert.equal(base.length, 1, "原数组不得被修改");
  assert.equal(upsertNode(base, { object_ref: { kind: "fragment", id: "", revision: 1 } }).length, 1);
});

test("removeNode：按 object_ref 删除；不存在时返回原数组引用", () => {
  const nodes = deriveNodes(SOURCES, LAYOUT);
  const nodesBefore = norm(nodes);
  const removed = removeNode(nodes, FRAG_A.object_ref);
  assert.equal(removed.length, 3);
  assert.equal(byKey(removed, FRAG_A.object_ref), undefined);
  assert.deepEqual(norm(nodes), nodesBefore, "输入数组不得被原地修改");
  assert.equal(removeNode(nodes, { kind: "scene", id: "nope", revision: 1 }), nodes);
});

test("布局更新/删除：纯函数返回新对象，键按 kind+id（不含 revision）", () => {
  const layout = updateLayoutPosition({}, FRAG_A.object_ref, 1, 2);
  assert.deepEqual(norm(layout), { [nodeKey(FRAG_A.object_ref)]: { x: 1, y: 2 } });
  assert.deepEqual(norm(updateLayoutPosition({}, FRAG_A.object_ref, 1, 2)), norm(layout));
  assert.equal(updateLayoutPosition(layout, FRAG_A.object_ref, NaN, 2), layout, "非法坐标返回原布局");
  const bumped = updateLayoutPosition(layout, { ...FRAG_A.object_ref, revision: 99 }, 7, 8);
  assert.equal(Object.keys(bumped).length, 1, "同身份（忽略 revision）覆盖同一键");
  assert.deepEqual(norm(bumped), { [nodeKey(FRAG_A.object_ref)]: { x: 7, y: 8 } });

  const cleared = removeLayoutPosition(bumped, FRAG_A.object_ref);
  assert.deepEqual(norm(cleared), {});
  assert.equal(removeLayoutPosition(cleared, FRAG_A.object_ref), cleared, "键不存在返回原引用");
  assert.deepEqual(norm(layout), { [nodeKey(FRAG_A.object_ref)]: { x: 1, y: 2 } }, "原布局不得被修改");
});

// ── 视口：平移 / 缩放 / 适配 ───────────────────────────────────────────────

test("zoomAt：缩放围绕锚点，锚点下的内容坐标不变（含多次累积与上下限夹取）", () => {
  const viewport = { x: 10, y: 20, zoom: 1.5 };
  const anchor = { x: 120, y: 90 };
  const contentBefore = screenToContent(viewport, anchor);

  const zoomed = zoomAt(viewport, 2, anchor);
  assert.equal(zoomed.zoom, 3);
  approx(screenToContent(zoomed, anchor).x, contentBefore.x, 1e-9, "锚点内容 x");
  approx(screenToContent(zoomed, anchor).y, contentBefore.y, 1e-9, "锚点内容 y");
  assertFiniteNumbers(zoomed, "zoomed");

  let accumulated = viewport;
  for (let i = 0; i < 12; i += 1) accumulated = zoomAt(accumulated, 1.15, anchor);
  approx(screenToContent(accumulated, anchor).x, contentBefore.x, 1e-6, "累积缩放锚点内容 x");
  approx(screenToContent(accumulated, anchor).y, contentBefore.y, 1e-6, "累积缩放锚点内容 y");

  // 触顶/触底夹取后锚点仍不漂移
  const maxed = zoomAt(viewport, 1e6, anchor);
  assert.equal(maxed.zoom, MAX_ZOOM);
  approx(screenToContent(maxed, anchor).x, contentBefore.x, 1e-9, "夹取后锚点内容 x");
  const minned = zoomAt(viewport, 1e-9, anchor);
  assert.equal(minned.zoom, MIN_ZOOM);
  approx(screenToContent(minned, anchor).x, contentBefore.x, 1e-9, "夹取后锚点内容 x（下限）");
});

test("panBy：屏幕位移纯平移；非法入参返回原视口引用且不产生 NaN", () => {
  const viewport = { x: 10, y: 20, zoom: 2 };
  assert.deepEqual(norm(panBy(viewport, 5, -7)), { x: 15, y: 13, zoom: 2 });
  assert.deepEqual(norm(panBy(viewport, 0, 0)), { x: 10, y: 20, zoom: 2 });

  for (const bad of [NaN, Infinity, -Infinity, undefined, null, "5"]) {
    assert.equal(panBy(viewport, bad, 0), viewport, `dx=${String(bad)} 须返回原视口`);
    assert.equal(panBy(viewport, 0, bad), viewport, `dy=${String(bad)} 须返回原视口`);
  }
  assert.deepEqual(norm(viewport), { x: 10, y: 20, zoom: 2 }, "原视口不得被修改");
});

test("zoomAt / contentToScreen / screenToContent：非法输入返回原视口或 null，不产生 NaN", () => {
  const viewport = { x: 10, y: 20, zoom: 1.5 };
  const anchor = { x: 120, y: 90 };

  for (const bad of [0, -1, NaN, Infinity, -Infinity, undefined, null, "2"]) {
    assert.equal(zoomAt(viewport, bad, anchor), viewport, `factor=${String(bad)} 须返回原视口`);
  }
  assert.equal(zoomAt(viewport, 2, { x: NaN, y: 0 }), viewport);
  assert.equal(zoomAt(viewport, 2, null), viewport);
  assert.equal(zoomAt({ x: 0, y: 0, zoom: 0 }, 2, anchor).zoom, 0, "非法视口原样返回（不产生 NaN）");

  assert.deepEqual(norm(contentToScreen(viewport, { x: 2, y: 3 })), { x: 13, y: 24.5 });
  assert.deepEqual(norm(screenToContent(viewport, { x: 13, y: 24.5 })), { x: 2, y: 3 });
  assert.equal(contentToScreen(viewport, { x: NaN, y: 0 }), null);
  assert.equal(screenToContent({ x: 0, y: 0, zoom: NaN }, { x: 1, y: 1 }), null);
  assert.equal(screenToContent(viewport, null), null);
});

test("fitToNodes：内容居中、全部落在视口内、zoom 有限为正；空节点/非法尺寸返回原视口", () => {
  const nodes = [
    { object_ref: { kind: "fragment", id: "a", revision: 1 }, kind: "fragment", label: "A", x: 0, y: 0 },
    { object_ref: { kind: "shot", id: "b", revision: 1 }, kind: "shot", label: "B", x: 400, y: 200 },
  ];
  const size = { width: 800, height: 600 };
  const viewport = identityViewport();
  const fitted = fitToNodes(viewport, nodes, size);

  assert.ok(fitted.zoom > 0 && Number.isFinite(fitted.zoom), "zoom 须有限为正");
  assertFiniteNumbers(fitted, "fitted");
  for (const node of nodes) {
    const topLeft = contentToScreen(fitted, { x: node.x, y: node.y });
    const bottomRight = contentToScreen(fitted, {
      x: node.x + NODE_WIDTH,
      y: node.y + NODE_HEIGHT,
    });
    assert.ok(topLeft.x >= -1e-9 && topLeft.y >= -1e-9, "内容不得溢出左上边界");
    assert.ok(
      bottomRight.x <= size.width + 1e-9 && bottomRight.y <= size.height + 1e-9,
      "内容不得溢出右下边界",
    );
  }
  const centerX = (0 + 400 + NODE_WIDTH) / 2;
  const centerY = (0 + 200 + NODE_HEIGHT) / 2;
  approx(contentToScreen(fitted, { x: centerX, y: centerY }).x, size.width / 2, 1e-9, "水平居中");
  approx(contentToScreen(fitted, { x: centerX, y: centerY }).y, size.height / 2, 1e-9, "垂直居中");

  // padding 生效：内容边界与视口边界之间留有留白
  const padded = fitToNodes(viewport, nodes, size, FIT_PADDING);
  assert.deepEqual(norm(padded), norm(fitted));

  assert.equal(fitToNodes(viewport, [], size), viewport, "空节点返回原视口");
  assert.equal(fitToNodes(viewport, null, size), viewport);
  assert.equal(fitToNodes(viewport, nodes, { width: 0, height: 0 }), viewport);
  assert.equal(fitToNodes(viewport, nodes, { width: NaN, height: 600 }), viewport);
  assert.equal(fitToNodes(viewport, nodes, size, NaN), viewport);
  assert.equal(fitToNodes(viewport, nodes, size, -1), viewport);
});

test("identityViewport / 常量：默认视口与新节点默认位置稳定", () => {
  assert.deepEqual(norm(identityViewport()), { x: 0, y: 0, zoom: 1 });
  assert.deepEqual(norm(DEFAULT_POSITION), { x: 0, y: 0 });
  assert.ok(MIN_ZOOM > 0 && MIN_ZOOM < 1 && MAX_ZOOM > 1);
  assert.ok(NODE_WIDTH > 0 && NODE_HEIGHT > 0);
});

// ── canvas-nodes.ts：纯逻辑模块边界 ────────────────────────────────────────

test("canvas-nodes.ts：无 React/DOM/网络/localStorage 依赖，转译后无运行时 require", () => {
  assert.ok(!nodesOutput.includes("require("), "type-only import 必须被转译擦除（vm 可直接加载）");
  assert.ok(!/from "react"/.test(nodesSource));
  assert.ok(!nodesSource.includes("fetch("));
  assert.ok(!/localStorage\s*[.[]/.test(nodesSource));
  assert.ok(!/\b(document|window|navigator)\b/.test(nodesSource));
});

// ── InfiniteCanvas.tsx：受控组件字符串断言 ─────────────────────────────────

const canvasSrc = fs.readFileSync(canvasTsx, "utf8");

test("InfiniteCanvas：受控（nodes/viewport 经 props 传入、变更经回调上抛、无内部状态/持久化）", () => {
  assert.ok(canvasSrc.startsWith('"use client"'), "须保留 use client 头");
  assert.ok(canvasSrc.includes("nodes"), "须消费 nodes props");
  assert.ok(canvasSrc.includes("viewport"), "须消费 viewport props");
  assert.ok(canvasSrc.includes("onViewportChange"), "视口变更须回调上抛");
  assert.ok(canvasSrc.includes("onMoveNode"), "节点拖动结果须回调上抛");
  assert.ok(!canvasSrc.includes("useState"), "不得镜像/内部重置布局状态（无 useState）");
  assert.ok(!canvasSrc.includes("useReducer"), "不得内部持有布局状态（无 useReducer）");
  assert.ok(!canvasSrc.includes("localStorage"), "不得读写 localStorage");
  assert.ok(!canvasSrc.includes("sessionStorage"), "不得读写 sessionStorage");
  assert.ok(!canvasSrc.includes("fetch("), "不得发网络请求");
});

test("InfiniteCanvas：节点数量与 DOM 一一对应（nodes.map + object_ref 作 key，无过滤丢弃）", () => {
  assert.ok(canvasSrc.includes("nodes.map"), "须直接遍历 nodes 渲染");
  assert.ok(
    canvasSrc.includes("key={nodeKey(node.object_ref)}"),
    "须以 object_ref 身份作 key",
  );
  assert.ok(canvasSrc.includes("data-node-count={nodes.length}"), "须暴露节点数量");
  assert.ok(
    !/nodes\s*\.\s*(filter|slice|sort|splice|find)/.test(canvasSrc),
    "不得有任何过滤/截断/重排而丢弃节点的分支",
  );
  assert.ok(!canvasSrc.includes(".filter("), "不得过滤节点");
});

test("InfiniteCanvas：平移/滚轮缩放/按钮缩放/适配/拖动节点接线齐备且结果上抛", () => {
  assert.ok(canvasSrc.includes("panBy("), "平移须走 panBy");
  assert.ok(canvasSrc.includes("zoomAt("), "缩放须走 zoomAt（围绕锚点）");
  assert.ok(canvasSrc.includes("fitToNodes("), "适配须走 fitToNodes");
  assert.ok(canvasSrc.includes("onMoveNode("), "拖动节点须上抛坐标");
  assert.ok(canvasSrc.includes("setPointerCapture"), "拖拽须捕获指针");
  assert.ok(canvasSrc.includes("addEventListener") && canvasSrc.includes("removeEventListener"), "滚轮监听须挂载并清理");
  assert.ok(canvasSrc.includes('"wheel"') || canvasSrc.includes("onWheel"), "须支持滚轮缩放");
  assert.ok(canvasSrc.includes("{ passive: false }"), "滚轮须非 passive 以阻止页面滚动");
  assert.ok(canvasSrc.includes("minHeight") || canvasSrc.includes("min-height"), "画布须可用尺寸");
});

test("InfiniteCanvas：节点坐标只来自传入值（transform 渲染 props 坐标，缩放原点在内容左上角）", () => {
  assert.ok(canvasSrc.includes("translate(${node.x}px, ${node.y}px)"), "节点须按 props 坐标渲染");
  assert.ok(canvasSrc.includes("translate(${viewport.x}px, ${viewport.y}px)"), "内容层须按 props 视口渲染");
  assert.ok(canvasSrc.includes("scale(${viewport.zoom})"), "内容层须按 props 缩放");
  assert.ok(canvasSrc.includes("transform-origin: 0 0"), "缩放原点须为内容左上角");
  assert.ok(canvasSrc.includes("<style jsx"), "保留既有 <style jsx> 风格");
});

test("InfiniteCanvas：禁止项（无 fetch( / \"/api/ / 不 import app/ / 不引用旧产品 / 不导航 / 无硬编码样例）", () => {
  assert.ok(!canvasSrc.includes('"/api/'), "不得引用 /api/");
  assert.ok(!canvasSrc.includes("fetch("), "不得调用 fetch(");
  assert.ok(!canvasSrc.includes('from "app/'), "不得 import app/");
  assert.ok(!canvasSrc.includes("from '../app/"), "不得 import ../app/");
  assert.ok(!canvasSrc.includes('from "../../app/'), "不得 import ../../app/");
  assert.ok(!/ProjectHome|TaskActivity|reader/.test(canvasSrc), "不得引用旧产品组件");
  assert.ok(!canvasSrc.includes("router.push"), "不得导航");
  assert.ok(!canvasSrc.includes("window.location"), "不得导航");
  assert.ok(!canvasSrc.includes("示例") && !canvasSrc.includes("样例") && !canvasSrc.includes("Lorem"), "无硬编码样例");
});
