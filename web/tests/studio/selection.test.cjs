const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// SOURCE-13 验收：api.ts 追加纯逻辑（anchorToIndex/selectionToRange/
// createFragmentCandidate，vm+transpile）+ 两个组件（字符串断言）。
// 模式与 source-reader.test.cjs 完全一致。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const apiTs = path.join(root, "features", "studio", "source", "api.ts");
const readerTsx = path.join(
  root,
  "features",
  "studio",
  "source",
  "components",
  "SourceReader.tsx",
);
const toolbarTsx = path.join(
  root,
  "features",
  "studio",
  "source",
  "components",
  "SelectionToolbar.tsx",
);
const clientTs = path.join(root, "shared", "api", "client.ts");

function transpile(file) {
  return ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}

// ---------------------------------------------------------------------------
// vm 加载 api.ts（纯逻辑：类型导入全部 type-only，transpile 后无 require）
// ---------------------------------------------------------------------------
const apiContext = { exports: {} };
vm.runInNewContext(transpile(apiTs), apiContext);
const { anchorToIndex, selectionToRange, createFragmentCandidate } =
  apiContext.exports;

// ---------------------------------------------------------------------------
// vm 加载 client.ts（真客户端 + 每测试注入 mock fetch，复刻 source-reader.test.cjs）
// ---------------------------------------------------------------------------
const clientContext = {
  window: { localStorage: null },
  fetch: (...args) => {
    throw new Error("tests must inject fetch via ClientOptions.fetch");
  },
  AbortController: AbortController,
  crypto: { randomUUID: () => "fixed-uuid-1" },
  process: { env: {} },
  setTimeout,
  clearTimeout,
  exports: {},
};
vm.runInNewContext(transpile(clientTs), clientContext);
const { createClient } = clientContext.exports;

function makeClient(fetchImpl) {
  const store = new Map();
  return createClient({
    baseUrl: "http://test.local",
    fetch: fetchImpl,
    storage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => void store.set(k, String(v)),
      removeItem: (k) => void store.delete(k),
    },
  });
}

function okJson(data, status = 200) {
  return {
    ok: true,
    status,
    json: async () => data,
    text: async () => JSON.stringify(data),
  };
}

function errJson(status, body) {
  return {
    ok: false,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

// vm realm 对象与主 realm 原型不同，deepEqual 前一律 JSON 归一化
const norm = (v) => JSON.parse(JSON.stringify(v));

const FRAG_CREATED = {
  object_ref: { kind: "fragment", id: "frag-1", revision: 1 },
  name: "片段甲",
  summary: null,
  state: "candidate",
  range: { start: 0, end: 4 },
  created_at: 1,
};

// ---------------------------------------------------------------------------
// anchorToIndex：单段内偏移 / 跨段前缀和 / 末尾 / 非法入参
// ---------------------------------------------------------------------------

test("anchorToIndex: 单段内偏移（nodeIndex=0）", () => {
  const texts = ["ab", "cd", "ef"];
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: 0 }), 0);
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: 1 }), 1);
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: 2 }), 2);
});

test("anchorToIndex: 跨段前缀和", () => {
  const texts = ["ab", "cd", "ef"];
  // nodeIndex=1 → 前缀 2；nodeIndex=2 → 前缀 4
  assert.equal(anchorToIndex(texts, { nodeIndex: 1, offset: 0 }), 2);
  assert.equal(anchorToIndex(texts, { nodeIndex: 1, offset: 2 }), 4);
  assert.equal(anchorToIndex(texts, { nodeIndex: 2, offset: 0 }), 4);
  assert.equal(anchorToIndex(texts, { nodeIndex: 2, offset: 1 }), 5);
});

test("anchorToIndex: nodeIndex === texts.length（末尾，offset 必须 0）", () => {
  const texts = ["ab", "cd", "ef"];
  // 末尾 = 总长 6
  assert.equal(anchorToIndex(texts, { nodeIndex: 3, offset: 0 }), 6);
  // nodeIndex === texts.length 但 offset !== 0 → null
  assert.equal(anchorToIndex(texts, { nodeIndex: 3, offset: 1 }), null);
});

test("anchorToIndex: 非法 nodeIndex/offset（负数、超界、非整数、NaN）→ null", () => {
  const texts = ["ab", "cd", "ef"];
  // nodeIndex 负数
  assert.equal(anchorToIndex(texts, { nodeIndex: -1, offset: 0 }), null);
  // nodeIndex 超界
  assert.equal(anchorToIndex(texts, { nodeIndex: 4, offset: 0 }), null);
  // nodeIndex 非整数
  assert.equal(anchorToIndex(texts, { nodeIndex: 0.5, offset: 0 }), null);
  // nodeIndex NaN
  assert.equal(anchorToIndex(texts, { nodeIndex: NaN, offset: 0 }), null);
  // offset 负数
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: -1 }), null);
  // offset 超界（> texts[nodeIndex].length）
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: 3 }), null);
  // offset 非整数
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: 1.5 }), null);
  // offset NaN
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: NaN }), null);
});

test("anchorToIndex: 空 texts（nodeIndex 只能 0 且 offset 0）", () => {
  const texts = [];
  assert.equal(anchorToIndex(texts, { nodeIndex: 0, offset: 0 }), 0);
  assert.equal(anchorToIndex(texts, { nodeIndex: 1, offset: 0 }), null);
});

// ---------------------------------------------------------------------------
// selectionToRange：正向 / 反向归一化 / collapsed / 任一端非法 / 跨段换行 / emoji
// ---------------------------------------------------------------------------

test("selectionToRange: 正向选区", () => {
  const texts = ["ab", "cd"];
  const r = selectionToRange(
    texts,
    { nodeIndex: 0, offset: 0 },
    { nodeIndex: 1, offset: 2 },
  );
  assert.deepEqual(norm(r), { start: 0, end: 4 });
});

test("selectionToRange: 反向（reversed）选区归一化为 start < end", () => {
  const texts = ["ab", "cd"];
  // start 在右端，end 在左端 → 交换
  const r = selectionToRange(
    texts,
    { nodeIndex: 1, offset: 2 },
    { nodeIndex: 0, offset: 0 },
  );
  assert.deepEqual(norm(r), { start: 0, end: 4 });
});

test("selectionToRange: collapsed（start === end）→ null", () => {
  const texts = ["ab", "cd"];
  const r = selectionToRange(
    texts,
    { nodeIndex: 0, offset: 1 },
    { nodeIndex: 0, offset: 1 },
  );
  assert.equal(r, null);
});

test("selectionToRange: 任一端非法 → null", () => {
  const texts = ["ab", "cd"];
  // start 非法（nodeIndex 超界）
  assert.equal(
    selectionToRange(
      texts,
      { nodeIndex: 5, offset: 0 },
      { nodeIndex: 1, offset: 2 },
    ),
    null,
  );
  // end 非法（offset 超界）
  assert.equal(
    selectionToRange(
      texts,
      { nodeIndex: 0, offset: 0 },
      { nodeIndex: 1, offset: 3 },
    ),
    null,
  );
});

test("selectionToRange: 跨段（含 \\n 的文本，选区跨换行，slice 含 \\n）", () => {
  // canonical = "ab\\ncd"（长度 5）；段 [0,2)="ab"、[2,5)="\\ncd"
  const texts = ["ab", "\ncd"];
  // 从段 0 偏移 1（全局 1）到段 1 偏移 2（全局 4）
  const r = selectionToRange(
    texts,
    { nodeIndex: 0, offset: 1 },
    { nodeIndex: 1, offset: 2 },
  );
  assert.deepEqual(norm(r), { start: 1, end: 4 });
  const canonical = "ab\ncd";
  // slice 必须含换行
  assert.equal(canonical.slice(r.start, r.end), "b\nc");
  assert.ok(canonical.slice(r.start, r.end).includes("\n"), "跨换行选区须含 \\n");
});

test("selectionToRange: emoji（代理对）选中 emoji 段 → {start:2,end:4}，slice === \"😀\" 且不劈代理对", () => {
  // canonical "前言😀后续"（UTF-16 长 6）按 [0,2)/[2,4)/[4,6) 三段
  const canonical = "前言😀后续";
  assert.equal(canonical.length, 6);
  const texts = ["前言", "😀", "后续"];
  // 选中 emoji 段（nodeIndex=1，offset 0..2）
  const r = selectionToRange(
    texts,
    { nodeIndex: 1, offset: 0 },
    { nodeIndex: 1, offset: 2 },
  );
  assert.deepEqual(norm(r), { start: 2, end: 4 });
  assert.equal(canonical.slice(r.start, r.end), "😀");
  // 不含落单代理半字
  const lone =
    /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  assert.ok(!lone.test(canonical.slice(r.start, r.end)), "切片含落单代理半字");
});

test("selectionToRange: 相邻双 emoji（边界不劈代理对）", () => {
  // canonical "a😀😀b"（UTF-16 长 6）：a(0) 😀(1,2) 😀(3,4) b(5)
  const canonical = "a😀😀b";
  assert.equal(canonical.length, 6);
  const texts = ["a", "😀😀", "b"];
  // 选中双 emoji 段（nodeIndex=1，offset 0..4）
  const r = selectionToRange(
    texts,
    { nodeIndex: 1, offset: 0 },
    { nodeIndex: 1, offset: 4 },
  );
  assert.deepEqual(norm(r), { start: 1, end: 5 });
  assert.equal(canonical.slice(r.start, r.end), "😀😀");
  const lone =
    /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  assert.ok(!lone.test(canonical.slice(r.start, r.end)), "切片含落单代理半字");
});

// ---------------------------------------------------------------------------
// createFragmentCandidate（mock fetch，同 source-reader.test.cjs 的 makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("createFragmentCandidate: 首发 URL/方法/body（expected_range_set_revision=null、command_id）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_CREATED, 201);
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    source_revision_id: "rev1",
    range: { start: 0, end: 4 },
    name: "片段甲",
    expected_range_set_revision: null,
    command_id: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
});

test("createFragmentCandidate: 201 → ok、data.object_ref.id 可用", async () => {
  const client = makeClient(async () => okJson(FRAG_CREATED, 201));
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
});

test("createFragmentCandidate: 200（幂等重放）→ ok", async () => {
  const client = makeClient(async () => okJson(FRAG_CREATED, 200));
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
});

test("createFragmentCandidate: 首发 409 revision_conflict（range_set, actual=5）→ 第二次 body.expected_range_set_revision=5 且 command_id 不变；第二次 201 → ok", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({ url, body });
    if (calls.length === 1) {
      return errJson(409, {
        error: {
          code: "revision_conflict",
          message: "范围集合版本已变化，请刷新片段列表后重试。",
          details: {
            object: { kind: "range_set", id: "s1" },
            expected: null,
            actual: 5,
          },
        },
      });
    }
    return okJson(FRAG_CREATED, 201);
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 2, "恰两次请求");
  assert.equal(calls[0].body.expected_range_set_revision, null, "首发带 null");
  assert.equal(calls[1].body.expected_range_set_revision, 5, "重试带 actual");
  assert.equal(calls[0].body.command_id, "cmd-1");
  assert.equal(calls[1].body.command_id, "cmd-1", "command_id 不变");
  // 其余 body 字段不变
  assert.equal(calls[1].body.source_revision_id, "rev1");
  assert.deepEqual(norm(calls[1].body.range), { start: 0, end: 4 });
  assert.equal(calls[1].body.name, "片段甲");
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
});

test("createFragmentCandidate: 重试也 409 → 返回该 409，请求总数恰 2（不得第三次）", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({ url, body });
    const actual = calls.length === 1 ? 5 : 9;
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "范围集合版本已变化，请刷新片段列表后重试。",
        details: {
          object: { kind: "range_set", id: "s1" },
          expected: body.expected_range_set_revision,
          actual,
        },
      },
    });
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 2, "恰两次请求，不得第三次");
  assert.equal(calls[0].body.expected_range_set_revision, null);
  assert.equal(calls[1].body.expected_range_set_revision, 5);
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.deepEqual(norm(res.details), {
    object: { kind: "range_set", id: "s1" },
    expected: 5,
    actual: 9,
  });
});

test("createFragmentCandidate: 409 revision_conflict 但 object.kind != range_set → 不重试（透传）", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "片段版本已变化，请刷新片段详情后重试。",
        details: {
          object: { kind: "fragment", id: "frag-1" },
          expected: 1,
          actual: 2,
        },
      },
    });
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "object.kind != range_set → 不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "revision_conflict");
  assert.deepEqual(norm(res.details), {
    object: { kind: "fragment", id: "frag-1" },
    expected: 1,
    actual: 2,
  });
});

test("createFragmentCandidate: 409 range_overlap（conflicts 数组）→ 不重试（请求恰 1 次）、code/conflicts 原样透传", async () => {
  const calls = [];
  const conflicts = [
    { fragment_id: "frag-1", name: "片段甲", start: 0, end: 4 },
    { fragment_id: "frag-2", name: "片段乙", start: 2, end: 6 },
  ];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "range_overlap",
        message: "该范围与已有片段「片段甲」（frag-1）重叠。",
        details: { conflicts },
      },
    });
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段丙",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "range_overlap");
  assert.deepEqual(norm(res.details), { conflicts });
  assert.equal(res.details.conflicts.length, 2);
  assert.equal(res.details.conflicts[0].name, "片段甲");
  assert.equal(res.details.conflicts[1].name, "片段乙");
});

test("createFragmentCandidate: 422 validation_failed（name）→ 不重试、violations 透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "validation_failed",
        message: "请求校验失败。",
        details: {
          violations: [
            {
              field: "name",
              rule: "required",
              message: "片段名不能为空。",
            },
          ],
        },
      },
    });
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations.length, 1);
  assert.equal(res.details.violations[0].field, "name");
  assert.equal(res.details.violations[0].rule, "required");
});

test("createFragmentCandidate: 404 not_found → 不重试", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "project", id: "p1" },
      },
    });
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "project", id: "p1" });
});

test("createFragmentCandidate: network → 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await createFragmentCandidate(client, "p1", {
    sourceRevisionId: "rev1",
    start: 0,
    end: 4,
    name: "片段甲",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// SelectionToolbar.tsx / SourceReader.tsx 字符串断言（source-reader.test.cjs 模式）
// ---------------------------------------------------------------------------

const readerSrc = fs.readFileSync(readerTsx, "utf8");
const toolbarSrc = fs.readFileSync(toolbarTsx, "utf8");

test("SelectionToolbar: use client + onCancel/onCreated 接线 + Esc（Escape）处理", () => {
  assert.ok(toolbarSrc.startsWith('"use client"'));
  assert.ok(toolbarSrc.includes("onCancel"), "onCancel 接线缺失");
  assert.ok(toolbarSrc.includes("onCreated"), "onCreated 接线缺失");
  assert.ok(toolbarSrc.includes("Escape"), "Esc（Escape）处理缺失");
  assert.ok(toolbarSrc.includes("addEventListener"), "Esc 监听缺失");
  assert.ok(toolbarSrc.includes("removeEventListener"), "Esc 监听卸载清理缺失");
});

test("SelectionToolbar: range_overlap 冲突展示（conflicts）+ revision_conflict 刷新提示", () => {
  assert.ok(toolbarSrc.includes("range_overlap"), "range_overlap 处理缺失");
  assert.ok(toolbarSrc.includes("conflicts"), "conflicts 展示缺失");
  assert.ok(toolbarSrc.includes("revision_conflict"), "revision_conflict 处理缺失");
  assert.ok(toolbarSrc.includes("刷新"), "revision_conflict 刷新提示缺失");
});

test("SelectionToolbar: 保存按钮 submitting 时 disabled + 不直接 fetch + 无硬编码样例", () => {
  assert.ok(
    toolbarSrc.includes("disabled={submitting}"),
    "保存按钮须 submitting 时 disabled",
  );
  assert.ok(!toolbarSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!toolbarSrc.includes("示例"), "无硬编码样例");
  assert.ok(!toolbarSrc.includes("样例"), "无硬编码样例");
  assert.ok(!toolbarSrc.includes("Lorem"), "无硬编码样例");
});

test("SelectionToolbar: 负向 10（空名提交本地拦截，不发请求）+ 负向 6/7（保存失败保留 pending）", () => {
  // 负向 10：name.trim() === "" → 本地提示，不发请求（不进入 createFragmentCandidate）
  assert.ok(
    toolbarSrc.includes('trimmed === ""'),
    "空名本地拦截缺失",
  );
  // 负向 6/7：保存失败（!ok）→ setError(res)，保留 name 与 pending（不退出、不清选区）
  assert.ok(
    toolbarSrc.includes("setError(res)"),
    "保存失败须 setError(res) 保留 pending",
  );
});

test("SourceReader: selectionchange 监听 + domAnchorToNodeAnchor + validateRange 门", () => {
  assert.ok(readerSrc.includes("selectionchange"), "selectionchange 监听缺失");
  assert.ok(readerSrc.includes("domAnchorToNodeAnchor"), "domAnchorToNodeAnchor 缺失");
  assert.ok(readerSrc.includes("validateRange"), "validateRange 门缺失");
});

test("SourceReader: 负向 8（工具条内焦点保持 pending）+ 负向 9（选区在正文容器外不出现工具条）", () => {
  // 负向 8：点击工具条输入框导致选区消失 → activeElement 在工具条内时保持 pending
  assert.ok(readerSrc.includes("activeElement"), "activeElement 判定缺失");
  assert.ok(readerSrc.includes("toolbarRef"), "toolbarRef 包裹工具条缺失");
  // 负向 9：选区两端必须落在正文容器内（容器级或被容器包含），否则清 pending
  assert.ok(readerSrc.includes("range.startContainer"), "startContainer 容器判定缺失");
  assert.ok(readerSrc.includes("range.endContainer"), "endContainer 容器判定缺失");
  assert.ok(readerSrc.includes("container.contains"), "容器包含判定缺失");
});

test("SourceReader: SelectionToolbar 渲染 + onCreated → setRetryTick（重取）+ 不含导航", () => {
  assert.ok(readerSrc.includes("SelectionToolbar"), "SelectionToolbar 渲染缺失");
  assert.ok(readerSrc.includes("setRetryTick"), "onCreated 须触发重取（setRetryTick）");
  assert.ok(!readerSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(!readerSrc.includes("window.location"), "不得导航（window.location）");
});

test("SourceReader: 既有断言（computeSegments/fetchAllFragments/pre-wrap/mark）保持通过", () => {
  assert.ok(readerSrc.includes("computeSegments"), "须用 computeSegments 切段");
  assert.ok(readerSrc.includes("fetchAllFragments"), "须走 fetchAllFragments 全量翻页");
  assert.ok(readerSrc.includes("pre-wrap"), "正文须 white-space: pre-wrap");
  assert.ok(readerSrc.includes("<mark"), "高亮段须为 <mark>");
  assert.ok(readerSrc.includes("data-fragment-id"), "高亮段须带 data-fragment-id");
  assert.ok(readerSrc.includes("studio-fragment-"), "锚点 id 须为 studio-fragment-${id} 模式");
});
