const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// SOURCE-14 验收：api.ts 追加纯逻辑（confirmFragment，vm+transpile）+
// FragmentActions.tsx（字符串断言）。
// 模式与 selection.test.cjs 完全一致。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const apiTs = path.join(root, "features", "studio", "source", "api.ts");
const actionsTsx = path.join(
  root,
  "features",
  "studio",
  "source",
  "components",
  "FragmentActions.tsx",
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
const { confirmFragment } = apiContext.exports;

// ---------------------------------------------------------------------------
// vm 加载 client.ts（真客户端 + 每测试注入 mock fetch，复刻 selection.test.cjs）
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

// F3 形状片段 DTO（十一键，state=confirmed，真实服务端值）
const FRAG_CONFIRMED = {
  object_ref: { kind: "fragment", id: "frag-1", revision: 2 },
  name: "片段甲",
  summary: null,
  state: "confirmed",
  revision_is_active: true,
  range: { start: 0, end: 4 },
  source_revision_id: "rev1",
  predecessor_ids: [],
  created_at: 1,
  updated_at: 2,
  retired_at: null,
};

// ---------------------------------------------------------------------------
// confirmFragment（mock fetch，同 selection.test.cjs 的 makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("confirmFragment: 首发 URL/方法/body（expected_revision、command_id === 传入值）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_CONFIRMED, 200);
  });
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 1,
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/confirm",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), { expected_revision: 1, command_id: "cmd-1" });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
  assert.equal(res.data.state, "confirmed");
});

test("confirmFragment: pid/fid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_CONFIRMED, 200);
  });
  await confirmFragment(client, "p/1?a", "f 1", {
    expectedRevision: 1,
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/fragments/" +
      encodeURIComponent("f 1") +
      "/confirm",
  );
});

test("confirmFragment: 200（F3 形状，state=confirmed）→ ok、data.object_ref.id/state 可用", async () => {
  const client = makeClient(async () => okJson(FRAG_CONFIRMED, 200));
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
  assert.equal(res.data.object_ref.revision, 2);
  assert.equal(res.data.state, "confirmed");
});

test("confirmFragment: 200（幂等重放）→ ok", async () => {
  const client = makeClient(async () => okJson(FRAG_CONFIRMED, 200));
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
  assert.equal(res.data.state, "confirmed");
});

test("confirmFragment: 409 revision_conflict（object.kind=fragment、expected/actual）→ 不重试（请求恰 1 次）、details 原样透传", async () => {
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
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 1,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.deepEqual(norm(res.details), {
    object: { kind: "fragment", id: "frag-1" },
    expected: 1,
    actual: 2,
  });
});

test("confirmFragment: 422 precondition_failed（details.reason === not_candidate）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "precondition_failed",
        message: "该片段当前状态不是 candidate，不能确认。",
        details: {
          reason: "not_candidate",
          blocked_by: { kind: "fragment", id: "frag-1", revision: 2 },
        },
      },
    });
  });
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.details.reason, "not_candidate");
  assert.deepEqual(norm(res.details.blocked_by), {
    kind: "fragment",
    id: "frag-1",
    revision: 2,
  });
});

test("confirmFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations.length, 1);
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("confirmFragment: 404 not_found（kind=fragment）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "fragment", id: "f1" },
      },
    });
  });
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "fragment", id: "f1" });
});

test("confirmFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await confirmFragment(client, "p1", "f1", {
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// FragmentActions.tsx 字符串断言（selection.test.cjs 模式）
// ---------------------------------------------------------------------------

const actionsSrc = fs.readFileSync(actionsTsx, "utf8");

test("FragmentActions: use client + 三态文案分开 + 走 fetchAllFragments（不直调单页 listFragments）", () => {
  assert.ok(actionsSrc.startsWith('"use client"'), "须以 use client 开头");
  assert.ok(actionsSrc.includes("正在加载片段…"), "加载中文案缺失");
  assert.ok(actionsSrc.includes("片段列表读取失败："), "错误文案缺失");
  assert.ok(actionsSrc.includes("还没有片段。"), "空态文案缺失");
  assert.ok(
    actionsSrc.includes("fetchAllFragments"),
    "须走 fetchAllFragments 全量翻页",
  );
  assert.ok(
    !actionsSrc.includes("listFragments("),
    "不得直调单页 listFragments",
  );
});

test("FragmentActions: 确认按钮仅 candidate（state === \"candidate\" 判定）", () => {
  assert.ok(
    actionsSrc.includes('state === "candidate"'),
    "candidate 判定缺失",
  );
});

test("FragmentActions: 显式点击接线（onClick + confirmFragment）+ crypto.randomUUID 每次点击一次 + expected_revision 取 object_ref.revision", () => {
  assert.ok(actionsSrc.includes("onClick"), "onClick 接线缺失");
  assert.ok(actionsSrc.includes("confirmFragment"), "confirmFragment 调用缺失");
  assert.ok(
    actionsSrc.includes("crypto.randomUUID()"),
    "crypto.randomUUID 缺失",
  );
  const count = (actionsSrc.match(/crypto\.randomUUID\(\)/g) || []).length;
  assert.equal(count, 1, "crypto.randomUUID 须恰出现一次（每次点击生成一次）");
  assert.ok(
    actionsSrc.includes("object_ref.revision"),
    "expected_revision 须取 object_ref.revision（真实服务端值）",
  );
});

test("FragmentActions: 提交中 disabled + 成功内部重取（refreshTick/retryTick）+ 失败 setError 保留行", () => {
  assert.ok(
    actionsSrc.includes("disabled={isSubmitting}"),
    "确认按钮须提交中 disabled（防双击）",
  );
  assert.ok(
    actionsSrc.includes("setRefreshTick"),
    "成功内部重取（refreshTick）缺失",
  );
  assert.ok(actionsSrc.includes("setRetryTick"), "错误重试（retryTick）缺失");
  assert.ok(
    actionsSrc.includes("setErrors"),
    "失败须 setError 保留行与按钮",
  );
});

test("FragmentActions: 无自动确认（useEffect 内不调 confirmFragment）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("confirmFragment"),
    "useEffect 内不得调用 confirmFragment（无自动确认）",
  );
  assert.ok(!actionsSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!actionsSrc.includes("示例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("样例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("Lorem"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(!actionsSrc.includes("window.location"), "不得导航（window.location）");
});

test("FragmentActions: 错误区三分支（revision_conflict/precondition_failed/其他 message）", () => {
  assert.ok(
    actionsSrc.includes('code === "revision_conflict"'),
    "revision_conflict 分支缺失",
  );
  assert.ok(
    actionsSrc.includes('code === "precondition_failed"'),
    "precondition_failed 分支缺失",
  );
  assert.ok(actionsSrc.includes("error.message"), "其他 message 分支缺失");
  assert.ok(
    actionsSrc.includes("片段版本已变化，请刷新后重试。"),
    "revision_conflict 文案缺失",
  );
  assert.ok(
    actionsSrc.includes("片段状态已变化，请刷新后重试。"),
    "precondition_failed 文案缺失",
  );
});

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段改名 UI（契约 v2.9，附录 02 §2 F4）— 新增
// ---------------------------------------------------------------------------

const { renameFragment } = apiContext.exports;

// F3 形状片段 DTO（改名成功返回：name 已 trimmed、ID 不变）
const FRAG_RENAMED = {
  object_ref: { kind: "fragment", id: "frag-1", revision: 2 },
  name: "片段甲",
  summary: null,
  state: "candidate",
  revision_is_active: true,
  range: { start: 0, end: 4 },
  source_revision_id: "rev1",
  predecessor_ids: [],
  created_at: 1,
  updated_at: 2,
  retired_at: null,
};

// ---------------------------------------------------------------------------
// renameFragment（mock fetch，同 confirmFragment 的 makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("renameFragment: 首发 URL/方法/body（name、expected_revision、command_id === 传入值）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_RENAMED, 200);
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/rename",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    name: "片段甲",
    expected_revision: 2,
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
});

test("renameFragment: pid/fid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_RENAMED, 200);
  });
  await renameFragment(client, "p/1?a", "f 1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/fragments/" +
      encodeURIComponent("f 1") +
      "/rename",
  );
});

test("renameFragment: 200（F3 形状 DTO，name 已 trimmed、ID 不变）→ ok、data.name/data.object_ref.id 可用", async () => {
  const client = makeClient(async () => okJson(FRAG_RENAMED, 200));
  const res = await renameFragment(client, "p1", "f1", {
    name: "  片段甲  ",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.name, "片段甲", "name 为服务端 trimmed 值");
  assert.equal(res.data.object_ref.id, "frag-1", "ID 不变");
  assert.equal(res.data.object_ref.revision, 2);
});

test("renameFragment: 200（幂等重放）→ ok", async () => {
  const client = makeClient(async () => okJson(FRAG_RENAMED, 200));
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.object_ref.id, "frag-1");
  assert.equal(res.data.name, "片段甲");
});

test("renameFragment: 409 revision_conflict（object.kind=fragment、expected/actual）→ 不重试（请求恰 1 次）、details 原样透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "片段版本已变化，请刷新片段详情后重试。",
        details: {
          object: { kind: "fragment", id: "frag-1" },
          expected: 2,
          actual: 3,
        },
      },
    });
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "fragment");
  assert.deepEqual(norm(res.details), {
    object: { kind: "fragment", id: "frag-1" },
    expected: 2,
    actual: 3,
  });
});

test("renameFragment: 422 validation_failed（violations，name rule=required）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "validation_failed",
        message: "请求校验失败。",
        details: {
          violations: [
            { field: "name", rule: "required", message: "片段名不能为空。" },
          ],
        },
      },
    });
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations.length, 1);
  assert.equal(res.details.violations[0].field, "name");
  assert.equal(res.details.violations[0].rule, "required");
  assert.equal(res.details.violations[0].message, "片段名不能为空。");
});

test("renameFragment: 422 validation_failed（violations，name rule=max_length）→ 不重试、透传", async () => {
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
              rule: "max_length",
              message: "片段名最长 120 个字符。",
            },
          ],
        },
      },
    });
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "x".repeat(121),
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "name");
  assert.equal(res.details.violations[0].rule, "max_length");
  assert.equal(res.details.violations[0].message, "片段名最长 120 个字符。");
});

test("renameFragment: 422 validation_failed（violations，command_id rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("renameFragment: 422 precondition_failed（details.reason === source_revision_inactive）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "precondition_failed",
        message: "该正文版本不是项目当前活跃版本，不能基于它改名片段。",
        details: {
          reason: "source_revision_inactive",
          blocked_by: { kind: "source", id: "rev-active", revision: null },
        },
      },
    });
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.details.reason, "source_revision_inactive");
});

test("renameFragment: 404 not_found（kind=fragment）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "fragment", id: "f1" },
      },
    });
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "fragment", id: "f1" });
});

test("renameFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await renameFragment(client, "p1", "f1", {
    name: "片段甲",
    expectedRevision: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// FragmentActions.tsx 改名 UI 字符串断言（selection.test.cjs 模式）
// ---------------------------------------------------------------------------

test("FragmentActions: 改名按钮仅 candidate/confirmed（retired 不显示改名）", () => {
  assert.ok(
    actionsSrc.includes(
      'fragment.state === "candidate" || fragment.state === "confirmed"',
    ),
    "改名判定须为 state === \"candidate\" 或 state === \"confirmed\"",
  );
  assert.ok(
    !actionsSrc.includes('state === "retired"'),
    "retired 行不得显示改名（也不显示确认）",
  );
});

test("FragmentActions: 行内改名表单（输入框 + 保存 + 取消）", () => {
  assert.ok(
    actionsSrc.includes('fragment-actions__rename"'),
    "「改名」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__rename-form"),
    "行内改名表单容器缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__rename-input"),
    "改名输入框缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__rename-save"),
    "「保存」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__rename-cancel"),
    "「取消」按钮缺失",
  );
  assert.ok(actionsSrc.includes("改名"), "「改名」按钮文案缺失");
  assert.ok(actionsSrc.includes("保存"), "「保存」按钮文案缺失");
  assert.ok(actionsSrc.includes("取消"), "「取消」按钮文案缺失");
});

test("FragmentActions: 取消零 API 调用（onCancel 路径无 renameFragment）", () => {
  const cancelSrc =
    actionsSrc.match(/function handleRenameCancel[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(cancelSrc.length > 0, "handleRenameCancel 缺失");
  assert.ok(
    cancelSrc.includes("setRenaming"),
    "取消须以 setRenaming 回到按钮态",
  );
  assert.ok(
    !cancelSrc.includes("renameFragment"),
    "取消不得调用 renameFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("clientObj"),
    "取消不得使用 client",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleRenameCancel(fid)}"),
    "取消按钮 onClick 接线缺失",
  );
});

test("FragmentActions: 显式保存接线（onClick + renameFragment）+ 每次点击一次 commandId + expected_revision 取 object_ref.revision", () => {
  const saveSrc =
    actionsSrc.match(/async function handleRenameSave[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(saveSrc.length > 0, "handleRenameSave 缺失");
  assert.ok(saveSrc.includes("renameFragment"), "保存须调用 renameFragment");
  assert.ok(
    saveSrc.includes("makeCommandId()"),
    "保存须每次点击生成一次 commandId",
  );
  assert.ok(
    saveSrc.includes("object_ref.revision"),
    "expected_revision 须取 object_ref.revision（真实服务端值）",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleRenameSave(fragment)}"),
    "保存按钮 onClick 接线缺失",
  );
});

test("FragmentActions: commandId 生成（crypto.randomUUID 每次点击一次；确认/保存/预览影响/确认退役/预览/确认调整/预览拆分/确认拆分各一次）", () => {
  const helperSrc =
    actionsSrc.match(/function makeCommandId[^{]*\{([\s\S]*?)\n\}/)?.[1] ??
    "";
  assert.ok(
    helperSrc.includes("crypto.randomUUID()"),
    "makeCommandId 须用 crypto.randomUUID()",
  );
  const callCount = (actionsSrc.match(/= makeCommandId\(\)/g) || []).length;
  assert.equal(
    callCount,
    8,
    "确认/保存/预览影响/确认退役/预览/确认调整/预览拆分/确认拆分各每次点击生成一次 commandId",
  );
  const confirmSrc =
    actionsSrc.match(/async function handleConfirm[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    confirmSrc.includes("makeCommandId()"),
    "确认须每次点击生成一次 commandId",
  );
  const count = (actionsSrc.match(/crypto\.randomUUID\(\)/g) || []).length;
  assert.equal(
    count,
    1,
    "crypto.randomUUID 须恰出现一次（经 makeCommandId 每次点击一次）",
  );
});

test('FragmentActions: 空名本地拦截（trimmed === "" 不发请求，保留表单）', () => {
  const saveSrc =
    actionsSrc.match(/async function handleRenameSave[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(saveSrc.includes(".trim()"), "保存须 trim 名称");
  assert.ok(saveSrc.includes('=== ""'), "空名判定缺失");
  const emptyCheck = saveSrc.indexOf('=== ""');
  const call = saveSrc.indexOf("renameFragment");
  assert.ok(
    emptyCheck !== -1 && call !== -1 && emptyCheck < call,
    "空名须先于 renameFragment 本地拦截（不发请求）",
  );
  assert.ok(saveSrc.includes("return;"), "空名须提前返回（保留表单）");
  assert.ok(saveSrc.includes("片段名不能为空。"), "本地提示文案缺失");
});

test("FragmentActions: 失败保留输入与表单（setErrors 保留、不关表单、不退出）", () => {
  const saveSrc =
    actionsSrc.match(/async function handleRenameSave[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(saveSrc.includes("setErrors"), "失败须 setErrors 保留行内错误");
  assert.ok(
    !saveSrc.includes("setRenaming"),
    "失败不得关闭改名表单（保留输入与表单）",
  );
  assert.ok(saveSrc.includes("setSubmitting"), "提交态管理缺失");
});

test("FragmentActions: 改名成功 → 清除该行错误 + 内部重取（refreshTick）；新快照清除 renaming（表单收起）", () => {
  const saveSrc =
    actionsSrc.match(/async function handleRenameSave[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    saveSrc.includes("setRefreshTick"),
    "改名成功须内部重取（refreshTick）",
  );
  assert.ok(
    saveSrc.includes("delete next[fid]"),
    "改名成功须清除该行错误",
  );
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(effectBlock[0].includes("setErrors({})"), "新快照须清除行内错误");
  assert.ok(
    effectBlock[0].includes("setRenaming({})"),
    "新快照须清除改名表单（防陈旧表单/错误）",
  );
});

test("FragmentActions: validation_failed 分支显示 violations message（本地空名 + 服务端超长名等）", () => {
  assert.ok(
    actionsSrc.includes('code === "validation_failed"'),
    "validation_failed 分支缺失",
  );
  assert.ok(actionsSrc.includes("violations"), "violations 处理缺失");
  assert.ok(
    actionsSrc.includes("片段名不能为空。"),
    "空名提示文案缺失（与服务端 rule=required 一致）",
  );
});

test("FragmentActions: 无自动提交（useEffect 内不调 confirmFragment/renameFragment）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("confirmFragment"),
    "useEffect 内不得调用 confirmFragment（无自动确认）",
  );
  assert.ok(
    !effectBlock[0].includes("renameFragment"),
    "useEffect 内不得调用 renameFragment（无自动提交）",
  );
  assert.ok(!actionsSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!actionsSrc.includes("示例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("样例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("Lorem"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(
    !actionsSrc.includes("window.location"),
    "不得导航（window.location）",
  );
});

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段退役 UI（契约 v2.9，附录 02 §2 F7）— 新增
// ---------------------------------------------------------------------------

const { previewRetireFragment, applyRetireFragment } = apiContext.exports;

// F7 退役预览响应（恰 4 字段 {preview_id, kind, baseline, impact}）
const RETIRE_PREVIEW_OK = {
  preview_id: "preview-1",
  kind: "fragment_retire",
  baseline: {
    fragment: { kind: "fragment", id: "frag-1", revision: 2 },
    range_set: { id: "rs-1", cas: 5 },
  },
  impact: {
    affected: [
      { object_ref: { kind: "source_relation", id: "sr-1", revision: 2 } },
    ],
    needs_review: [
      { object_ref: { kind: "studio_job", id: "job-1", revision: null } },
    ],
    preservable: [
      { object_ref: { kind: "media_artifact", id: "ma-1", revision: null } },
    ],
  },
};

// F3 形状片段 DTO（退役成功返回：state=retired、retired_at）
const FRAG_RETIRED = {
  object_ref: { kind: "fragment", id: "frag-1", revision: 2 },
  name: "片段甲",
  summary: null,
  state: "retired",
  revision_is_active: true,
  range: { start: 0, end: 4 },
  source_revision_id: "rev1",
  predecessor_ids: [],
  created_at: 1,
  updated_at: 3,
  retired_at: 3,
};

// ---------------------------------------------------------------------------
// previewRetireFragment（mock fetch，同 confirmFragment 的
// makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("previewRetireFragment: 首发 URL/方法/body（target_fragment_id = 路径 fid、command_id === 传入值）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(RETIRE_PREVIEW_OK, 200);
  });
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/retire",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), { target_fragment_id: "f1", command_id: "cmd-1" });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("previewRetireFragment: pid/fid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(RETIRE_PREVIEW_OK, 200);
  });
  await previewRetireFragment(client, "p/1?a", "f 1", {
    targetFragmentId: "f 1",
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/fragments/" +
      encodeURIComponent("f 1") +
      "/retire",
  );
});

test("previewRetireFragment: 200（恰 4 字段 {preview_id, kind:'fragment_retire', baseline, impact}）→ ok、data.preview_id/data.baseline 可用", async () => {
  const client = makeClient(async () => okJson(RETIRE_PREVIEW_OK, 200));
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.preview_id, "preview-1");
  assert.equal(res.data.kind, "fragment_retire");
  assert.equal(res.data.baseline.fragment.id, "frag-1");
  assert.equal(res.data.baseline.fragment.revision, 2);
  assert.equal(res.data.baseline.range_set.cas, 5);
  assert.deepEqual(
    norm(Object.keys(res.data).sort()),
    ["baseline", "impact", "kind", "preview_id"],
    "恰 4 字段",
  );
});

test("previewRetireFragment: 422 precondition_failed（details.reason === already_retired）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "precondition_failed",
        message: "该片段已退役，不能重复退役。",
        details: {
          reason: "already_retired",
          blocked_by: { kind: "fragment", id: "frag-1", revision: 2 },
        },
      },
    });
  });
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.details.reason, "already_retired");
});

test("previewRetireFragment: 422 validation_failed（violations，rule=mismatch）→ 不重试、透传", async () => {
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
              field: "target_fragment_id",
              rule: "mismatch",
              message: "target_fragment_id 必须与路径片段一致。",
            },
          ],
        },
      },
    });
  });
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "target_fragment_id");
  assert.equal(res.details.violations[0].rule, "mismatch");
});

test("previewRetireFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("previewRetireFragment: 404 not_found → 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "fragment", id: "f1" },
      },
    });
  });
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "fragment", id: "f1" });
});

test("previewRetireFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await previewRetireFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// applyRetireFragment（mock fetch，同 confirmFragment 的
// makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("applyRetireFragment: 首发 URL/方法/body（preview_id、command_id === 传入值、expected_revision、expected_range_set_revision）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_RETIRED, 200);
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/retire/apply",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    preview_id: "preview-1",
    expected_revision: 2,
    expected_range_set_revision: 5,
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("applyRetireFragment: 200（F3 形状 DTO，state=retired、retired_at）→ ok、data.state 可用", async () => {
  const client = makeClient(async () => okJson(FRAG_RETIRED, 200));
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.state, "retired");
  assert.equal(res.data.retired_at, 3);
  assert.equal(res.data.object_ref.id, "frag-1");
});

test("applyRetireFragment: 409 preview_stale（details.preview_id + details.conflicts）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "preview_stale",
        message: "预览已过期或已被处理，请重新预览。",
        details: {
          preview_id: "preview-1",
          conflicts: [
            {
              object: { kind: "preview", id: "preview-1" },
              expected: "pending",
              actual: "expired",
            },
          ],
        },
      },
    });
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "preview_stale");
  assert.equal(res.details.preview_id, "preview-1");
  assert.equal(res.details.conflicts.length, 1);
});

test("applyRetireFragment: 409 revision_conflict（details.object.kind === range_set）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "版本已变化，请刷新后重试。",
        details: {
          object: { kind: "range_set", id: "rs-1" },
          expected: 5,
          actual: 6,
        },
      },
    });
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "range_set");
});

test("applyRetireFragment: 409 revision_conflict（details.object.kind === fragment）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "版本已变化，请刷新后重试。",
        details: {
          object: { kind: "fragment", id: "frag-1" },
          expected: 2,
          actual: 3,
        },
      },
    });
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "fragment");
});

test("applyRetireFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("applyRetireFragment: 404 not_found（kind=preview）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "preview", id: "preview-1" },
      },
    });
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "preview", id: "preview-1" });
});

test("applyRetireFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await applyRetireFragment(client, "p1", "f1", {
    previewId: "preview-1",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// FragmentActions.tsx 退役 UI 字符串断言（selection.test.cjs 模式）
// ---------------------------------------------------------------------------

test("FragmentActions: 退役按钮仅 candidate/confirmed（retired 不显示退役）", () => {
  assert.ok(
    actionsSrc.includes(
      'fragment.state === "candidate" || fragment.state === "confirmed"',
    ),
    "退役判定须为 state === \"candidate\" 或 state === \"confirmed\"",
  );
  assert.ok(
    !actionsSrc.includes('state === "retired"'),
    "retired 行不得显示退役（也不显示确认）",
  );
});

test("FragmentActions: 两步显式（阶段 1「预览影响」→ 阶段 2「确认退役」）+ 确认退役仅 preview 成功后可点击", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__retire-form"),
    "行内退役表单容器缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__retire-preview"),
    "「预览影响」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__retire-apply"),
    "「确认退役」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__retire-cancel"),
    "「取消」按钮缺失",
  );
  assert.ok(actionsSrc.includes("预览影响"), "「预览影响」按钮文案缺失");
  assert.ok(actionsSrc.includes("确认退役"), "「确认退役」按钮文案缺失");
  assert.ok(actionsSrc.includes("退役"), "「退役」按钮文案缺失");
  assert.ok(
    actionsSrc.includes('stage === "confirm"'),
    "阶段 2（确认退役）判定缺失",
  );
  assert.ok(
    actionsSrc.includes('stage === "preview"'),
    "阶段 1（预览影响）判定缺失",
  );
});

test("FragmentActions: 显式点击接线（onClick + previewRetireFragment/applyRetireFragment）+ makeCommandId() 每次点击一次", () => {
  const previewSrc =
    actionsSrc.match(/async function handleRetirePreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(previewSrc.length > 0, "handleRetirePreview 缺失");
  assert.ok(
    previewSrc.includes("previewRetireFragment"),
    "预览影响须调用 previewRetireFragment",
  );
  assert.ok(
    previewSrc.includes("makeCommandId()"),
    "预览影响须每次点击生成一次 commandId",
  );
  const applySrc =
    actionsSrc.match(/async function handleRetireApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(applySrc.length > 0, "handleRetireApply 缺失");
  assert.ok(
    applySrc.includes("applyRetireFragment"),
    "确认退役须调用 applyRetireFragment",
  );
  assert.ok(
    applySrc.includes("makeCommandId()"),
    "确认退役须每次点击生成一次 commandId",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleRetirePreview(fragment)}"),
    "「预览影响」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleRetireApply(fragment)}"),
    "「确认退役」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleRetireOpen(fragment)}"),
    "「退役」按钮 onClick 接线缺失",
  );
});

test("FragmentActions: expected_revision 取 object_ref.revision；expected_range_set_revision 取 preview.baseline.range_set.cas", () => {
  const applySrc =
    actionsSrc.match(/async function handleRetireApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("object_ref.revision"),
    "expected_revision 须取 object_ref.revision（真实服务端值）",
  );
  assert.ok(
    applySrc.includes("baseline.range_set.cas"),
    "expected_range_set_revision 须取 preview.baseline.range_set.cas（冻结基准）",
  );
});

test("FragmentActions: impact 摘要展示（affected/needs_review/preservable 计数）", () => {
  assert.ok(
    actionsSrc.includes("impact.affected.length"),
    "impact 摘要 affected 计数缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.needs_review.length"),
    "impact 摘要 needs_review 计数缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.preservable.length"),
    "impact 摘要 preservable 计数缺失",
  );
  assert.ok(actionsSrc.includes("影响："), "impact 摘要文案缺失");
  assert.ok(actionsSrc.includes("待复核："), "待复核文案缺失");
  assert.ok(actionsSrc.includes("可保留："), "可保留文案缺失");
});

test("FragmentActions: 取消零 API 调用（onCancel 路径无 previewRetireFragment/applyRetireFragment）", () => {
  const cancelSrc =
    actionsSrc.match(/function handleRetireCancel[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(cancelSrc.length > 0, "handleRetireCancel 缺失");
  assert.ok(
    cancelSrc.includes("setRetiring"),
    "取消须以 setRetiring 回到按钮态",
  );
  assert.ok(
    !cancelSrc.includes("previewRetireFragment"),
    "取消不得调用 previewRetireFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("applyRetireFragment"),
    "取消不得调用 applyRetireFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("clientObj"),
    "取消不得使用 client",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleRetireCancel(fid)}"),
    "取消按钮 onClick 接线缺失",
  );
});

test("FragmentActions: 失败保留表单（setErrors 保留、不退出）", () => {
  const previewSrc =
    actionsSrc.match(/async function handleRetirePreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    previewSrc.includes("setErrors"),
    "预览失败须 setErrors 保留行内错误",
  );
  const applySrc =
    actionsSrc.match(/async function handleRetireApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("setErrors"),
    "确认退役失败须 setErrors 保留行内错误",
  );
  assert.ok(
    applySrc.includes("setSubmitting"),
    "提交态管理缺失",
  );
});

test("FragmentActions: preview_stale 分支文案「预览已过期，请重新预览。」", () => {
  assert.ok(
    actionsSrc.includes('code === "preview_stale"'),
    "preview_stale 分支缺失",
  );
  assert.ok(
    actionsSrc.includes("预览已过期，请重新预览。"),
    "preview_stale 文案缺失",
  );
});

test("FragmentActions: 无自动退役（useEffect 内不调 previewRetireFragment/applyRetireFragment）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("previewRetireFragment"),
    "useEffect 内不得调用 previewRetireFragment（无自动退役）",
  );
  assert.ok(
    !effectBlock[0].includes("applyRetireFragment"),
    "useEffect 内不得调用 applyRetireFragment（无自动退役）",
  );
  assert.ok(!actionsSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!actionsSrc.includes("示例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("样例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("Lorem"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(
    !actionsSrc.includes("window.location"),
    "不得导航（window.location）",
  );
});

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段边界调整 UI（契约 v2.9，附录 02 §2 F8）— 新增
// ---------------------------------------------------------------------------

const { previewBoundaryFragment, applyBoundaryFragment } = apiContext.exports;

// F8 边界预览响应（恰 4 字段 {preview_id, kind, baseline, impact}）
const BOUNDARY_PREVIEW_OK = {
  preview_id: "preview-2",
  kind: "fragment_boundary",
  baseline: {
    fragment: { kind: "fragment", id: "frag-1", revision: 2 },
    range_set: { id: "rs-1", cas: 5 },
  },
  impact: {
    affected: [
      { object_ref: { kind: "source_relation", id: "sr-1", revision: 2 } },
    ],
    needs_review: [
      { object_ref: { kind: "studio_job", id: "job-1", revision: null } },
    ],
    preservable: [
      { object_ref: { kind: "media_artifact", id: "ma-1", revision: null } },
    ],
  },
};

// F3 形状片段 DTO（边界 apply 成功返回：range=新范围、object_ref.revision=原+1）
const FRAG_BOUNDARY = {
  object_ref: { kind: "fragment", id: "frag-1", revision: 3 },
  name: "片段甲",
  summary: null,
  state: "confirmed",
  revision_is_active: true,
  range: { start: 2, end: 6 },
  source_revision_id: "rev1",
  predecessor_ids: [],
  created_at: 1,
  updated_at: 2,
  retired_at: null,
};

// ---------------------------------------------------------------------------
// previewBoundaryFragment（mock fetch，同 confirmFragment 的
// makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("previewBoundaryFragment: 首发 URL/方法/body（target_fragment_id、new_range、command_id === 传入值）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(BOUNDARY_PREVIEW_OK, 200);
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/boundary",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    target_fragment_id: "f1",
    new_range: { start: 0, end: 4 },
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("previewBoundaryFragment: pid/fid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(BOUNDARY_PREVIEW_OK, 200);
  });
  await previewBoundaryFragment(client, "p/1?a", "f 1", {
    targetFragmentId: "f 1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/fragments/" +
      encodeURIComponent("f 1") +
      "/boundary",
  );
});

test("previewBoundaryFragment: 200（恰 4 字段 {preview_id, kind:'fragment_boundary', baseline, impact}）→ ok、data.preview_id/data.baseline 可用", async () => {
  const client = makeClient(async () => okJson(BOUNDARY_PREVIEW_OK, 200));
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.preview_id, "preview-2");
  assert.equal(res.data.kind, "fragment_boundary");
  assert.equal(res.data.baseline.fragment.id, "frag-1");
  assert.equal(res.data.baseline.fragment.revision, 2);
  assert.equal(res.data.baseline.range_set.cas, 5);
  assert.deepEqual(
    norm(Object.keys(res.data).sort()),
    ["baseline", "impact", "kind", "preview_id"],
    "恰 4 字段",
  );
});

test("previewBoundaryFragment: 422 range_invalid（details.rule === \"empty\"）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "range_invalid",
        message: "范围为空：end 必须大于 start（正文长度 100 个 UTF-16 单位）。",
        details: { rule: "empty" },
      },
    });
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 4, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "range_invalid");
  assert.equal(res.details.rule, "empty");
  assert.ok(
    res.message.includes("正文长度"),
    "message 须含 canonical 长度",
  );
});

test("previewBoundaryFragment: 422 range_invalid（details.rule === \"out_of_bounds\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "range_invalid",
        message: "范围越界：正文长度 100 个 UTF-16 单位。",
        details: { rule: "out_of_bounds" },
      },
    });
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 101 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "range_invalid");
  assert.equal(res.details.rule, "out_of_bounds");
});

test("previewBoundaryFragment: 409 range_overlap（details.conflicts 数组）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "range_overlap",
        message: "该范围与已有片段「片段乙」（frag-2）重叠。",
        details: {
          conflicts: [
            { fragment_id: "frag-2", name: "片段乙", start: 3, end: 8 },
            { fragment_id: "frag-3", name: "片段丙", start: 9, end: 12 },
          ],
        },
      },
    });
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 2, end: 10 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "range_overlap");
  assert.equal(res.details.conflicts.length, 2);
  assert.equal(res.details.conflicts[0].name, "片段乙");
  assert.equal(res.details.conflicts[0].start, 3);
  assert.equal(res.details.conflicts[0].end, 8);
  assert.equal(res.details.conflicts[1].name, "片段丙");
});

test("previewBoundaryFragment: 422 precondition_failed（details.reason === source_revision_inactive）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "precondition_failed",
        message: "该正文版本不是项目当前活跃版本，不能基于它修改片段边界。",
        details: {
          reason: "source_revision_inactive",
          blocked_by: { kind: "source", id: "rev-active", revision: null },
        },
      },
    });
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.details.reason, "source_revision_inactive");
});

test("previewBoundaryFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("previewBoundaryFragment: 404 not_found（kind=fragment）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "fragment", id: "f1" },
      },
    });
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "fragment", id: "f1" });
});

test("previewBoundaryFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await previewBoundaryFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    newRange: { start: 0, end: 4 },
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// applyBoundaryFragment（mock fetch，同 confirmFragment 的
// makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("applyBoundaryFragment: 首发 URL/方法/body（preview_id、command_id === 传入值、expected_revision、expected_range_set_revision）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(FRAG_BOUNDARY, 200);
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/boundary/apply",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    preview_id: "preview-2",
    expected_revision: 2,
    expected_range_set_revision: 5,
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("applyBoundaryFragment: 200（F3 形状 DTO，range=新范围、object_ref.revision=原+1）→ ok、data.range/data.object_ref.revision 可用", async () => {
  const client = makeClient(async () => okJson(FRAG_BOUNDARY, 200));
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.range.start, 2, "range 为新范围 start");
  assert.equal(res.data.range.end, 6, "range 为新范围 end");
  assert.equal(res.data.object_ref.revision, 3, "revision+1");
  assert.equal(res.data.object_ref.id, "frag-1", "ID 不变");
});

test("applyBoundaryFragment: 409 preview_stale（details.preview_id + details.conflicts）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "preview_stale",
        message: "预览已过期或已被处理，请重新预览。",
        details: {
          preview_id: "preview-2",
          conflicts: [
            {
              object: { kind: "preview", id: "preview-2" },
              expected: "pending",
              actual: "expired",
            },
          ],
        },
      },
    });
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "preview_stale");
  assert.equal(res.details.preview_id, "preview-2");
  assert.equal(res.details.conflicts.length, 1);
});

test("applyBoundaryFragment: 409 revision_conflict（details.object.kind === \"range_set\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "版本已变化，请刷新后重试。",
        details: {
          object: { kind: "range_set", id: "rs-1" },
          expected: 5,
          actual: 6,
        },
      },
    });
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "range_set");
});

test("applyBoundaryFragment: 409 revision_conflict（details.object.kind === \"fragment\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "版本已变化，请刷新后重试。",
        details: {
          object: { kind: "fragment", id: "frag-1" },
          expected: 2,
          actual: 3,
        },
      },
    });
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "fragment");
});

test("applyBoundaryFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("applyBoundaryFragment: 404 not_found（kind=preview）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "preview", id: "preview-2" },
      },
    });
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "preview", id: "preview-2" });
});

test("applyBoundaryFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await applyBoundaryFragment(client, "p1", "f1", {
    previewId: "preview-2",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// FragmentActions.tsx 边界 UI 字符串断言（selection.test.cjs 模式）
// ---------------------------------------------------------------------------

test("FragmentActions: 边界按钮仅 candidate/confirmed（retired 不显示边界）", () => {
  assert.ok(
    actionsSrc.includes(
      'fragment.state === "candidate" || fragment.state === "confirmed"',
    ),
    "边界判定须为 state === \"candidate\" 或 state === \"confirmed\"",
  );
  assert.ok(
    actionsSrc.includes('fragment-actions__boundary"'),
    "「边界」按钮缺失",
  );
  assert.ok(actionsSrc.includes("边界"), "「边界」按钮文案缺失");
  assert.ok(
    !actionsSrc.includes('state === "retired"'),
    "retired 行不得显示边界（也不显示确认）",
  );
});

test("FragmentActions: 两步显式（阶段 1「预览」→ 阶段 2「确认调整」）+ 确认调整仅 preview 成功后可点击", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-form"),
    "行内边界表单容器缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-preview"),
    "「预览」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-apply"),
    "「确认调整」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-cancel"),
    "「取消」按钮缺失",
  );
  assert.ok(actionsSrc.includes("预览"), "「预览」按钮文案缺失");
  assert.ok(actionsSrc.includes("确认调整"), "「确认调整」按钮文案缺失");
  assert.ok(
    actionsSrc.includes('stage === "confirm"'),
    "阶段 2（确认调整）判定缺失",
  );
  assert.ok(
    actionsSrc.includes('stage === "preview"'),
    "阶段 1（预览）判定缺失",
  );
});

test("FragmentActions: 两个数字输入框（新 start/新 end，初始值 = fragment.range.start/end）", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-start"),
    "新 start 输入框缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-end"),
    "新 end 输入框缺失",
  );
  assert.ok(
    actionsSrc.includes('type="number"'),
    "数字输入框 type=number 缺失",
  );
  const openSrc =
    actionsSrc.match(/function handleBoundaryOpen[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(openSrc.length > 0, "handleBoundaryOpen 缺失");
  assert.ok(
    openSrc.includes("fragment.range.start"),
    "初始 start 须取 fragment.range.start",
  );
  assert.ok(
    openSrc.includes("fragment.range.end"),
    "初始 end 须取 fragment.range.end",
  );
});

test("FragmentActions: 本地整数校验（Number.isInteger 判定先于 previewBoundaryFragment 调用，非整数不发请求）", () => {
  const previewSrc =
    actionsSrc.match(/async function handleBoundaryPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(previewSrc.length > 0, "handleBoundaryPreview 缺失");
  assert.ok(
    previewSrc.includes("Number.isInteger"),
    "Number.isInteger 判定缺失",
  );
  const check = previewSrc.indexOf("Number.isInteger");
  const call = previewSrc.indexOf("previewBoundaryFragment");
  assert.ok(
    check !== -1 && call !== -1 && check < call,
    "整数判定须先于 previewBoundaryFragment（本地拦截，不发请求）",
  );
  assert.ok(previewSrc.includes("return;"), "非整数须提前返回（保留表单）");
  assert.ok(
    previewSrc.includes("范围端点必须为整数。"),
    "本地提示文案缺失",
  );
});

test("FragmentActions: 显式点击接线（onClick + previewBoundaryFragment/applyBoundaryFragment）+ makeCommandId() 每次点击一次", () => {
  const previewSrc =
    actionsSrc.match(/async function handleBoundaryPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    previewSrc.includes("previewBoundaryFragment"),
    "预览须调用 previewBoundaryFragment",
  );
  assert.ok(
    previewSrc.includes("makeCommandId()"),
    "预览须每次点击生成一次 commandId",
  );
  const applySrc =
    actionsSrc.match(/async function handleBoundaryApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(applySrc.length > 0, "handleBoundaryApply 缺失");
  assert.ok(
    applySrc.includes("applyBoundaryFragment"),
    "确认调整须调用 applyBoundaryFragment",
  );
  assert.ok(
    applySrc.includes("makeCommandId()"),
    "确认调整须每次点击生成一次 commandId",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleBoundaryPreview(fragment)}"),
    "「预览」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleBoundaryApply(fragment)}"),
    "「确认调整」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleBoundaryOpen(fragment)}"),
    "「边界」按钮 onClick 接线缺失",
  );
});

test("FragmentActions: expected_revision 取 object_ref.revision；expected_range_set_revision 取 preview.baseline.range_set.cas", () => {
  const applySrc =
    actionsSrc.match(/async function handleBoundaryApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("object_ref.revision"),
    "expected_revision 须取 object_ref.revision（真实服务端值）",
  );
  assert.ok(
    applySrc.includes("baseline.range_set.cas"),
    "expected_range_set_revision 须取 preview.baseline.range_set.cas（冻结基准）",
  );
});

test("FragmentActions: 边界 impact 摘要展示（affected/needs_review/preservable 计数）", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__boundary-impact"),
    "边界 impact 摘要缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.affected.length"),
    "impact 摘要 affected 计数缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.needs_review.length"),
    "impact 摘要 needs_review 计数缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.preservable.length"),
    "impact 摘要 preservable 计数缺失",
  );
});

test("FragmentActions: range_overlap 分支列出 conflicts（name + 范围）；range_invalid 分支显示 message", () => {
  assert.ok(
    actionsSrc.includes('code === "range_overlap"'),
    "range_overlap 分支缺失",
  );
  assert.ok(
    actionsSrc.includes("该范围与已有片段重叠（不自动裁切）："),
    "range_overlap 文案缺失",
  );
  assert.ok(
    actionsSrc.includes('code === "range_invalid"'),
    "range_invalid 分支缺失",
  );
});

test("FragmentActions: 取消零 API 调用（onCancel 路径无 previewBoundaryFragment/applyBoundaryFragment）", () => {
  const cancelSrc =
    actionsSrc.match(/function handleBoundaryCancel[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(cancelSrc.length > 0, "handleBoundaryCancel 缺失");
  assert.ok(
    cancelSrc.includes("setBoundary"),
    "取消须以 setBoundary 回到按钮态",
  );
  assert.ok(
    !cancelSrc.includes("previewBoundaryFragment"),
    "取消不得调用 previewBoundaryFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("applyBoundaryFragment"),
    "取消不得调用 applyBoundaryFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("clientObj"),
    "取消不得使用 client",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleBoundaryCancel(fid)}"),
    "取消按钮 onClick 接线缺失",
  );
});

test("FragmentActions: 失败保留表单（setErrors 保留、不退出）", () => {
  const previewSrc =
    actionsSrc.match(/async function handleBoundaryPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    previewSrc.includes("setErrors"),
    "预览失败须 setErrors 保留行内错误",
  );
  const applySrc =
    actionsSrc.match(/async function handleBoundaryApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("setErrors"),
    "确认调整失败须 setErrors 保留行内错误",
  );
  assert.ok(applySrc.includes("setSubmitting"), "提交态管理缺失");
});

test("FragmentActions: 无自动调整（useEffect 内不调 previewBoundaryFragment/applyBoundaryFragment）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("previewBoundaryFragment"),
    "useEffect 内不得调用 previewBoundaryFragment（无自动调整）",
  );
  assert.ok(
    !effectBlock[0].includes("applyBoundaryFragment"),
    "useEffect 内不得调用 applyBoundaryFragment（无自动调整）",
  );
  assert.ok(!actionsSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!actionsSrc.includes("示例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("样例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("Lorem"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(
    !actionsSrc.includes("window.location"),
    "不得导航（window.location）",
  );
});

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段拆分 UI（契约 v2.9，附录 02 §2 F9）— 新增
// ---------------------------------------------------------------------------

const { previewSplitFragment, applySplitFragment } = apiContext.exports;

// F9 拆分预览响应（恰 4 字段 {preview_id, kind:'fragment_split', baseline, impact}）
const SPLIT_PREVIEW_OK = {
  preview_id: "preview-3",
  kind: "fragment_split",
  baseline: {
    fragment: { kind: "fragment", id: "frag-1", revision: 2 },
    range_set: { id: "rs-1", cas: 5 },
  },
  impact: {
    affected: [
      { object_ref: { kind: "source_relation", id: "sr-1", revision: 2 } },
    ],
    needs_review: [
      { object_ref: { kind: "studio_job", id: "job-1", revision: null } },
    ],
    preservable: [
      { object_ref: { kind: "media_artifact", id: "ma-1", revision: null } },
    ],
  },
};

// F9 拆分 apply 成功响应（F9 冻结形状 {left, right, original}：
// left/right 各 object_ref(revision=1)+range、original object_ref(revision=current)+state='retired'）
const SPLIT_APPLY_OK = {
  left: {
    object_ref: { kind: "fragment", id: "frag-left", revision: 1 },
    range: { start: 0, end: 2 },
  },
  right: {
    object_ref: { kind: "fragment", id: "frag-right", revision: 1 },
    range: { start: 2, end: 4 },
  },
  original: {
    object_ref: { kind: "fragment", id: "frag-1", revision: 2 },
    state: "retired",
  },
};

// ---------------------------------------------------------------------------
// previewSplitFragment（mock fetch，同 confirmFragment 的
// makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("previewSplitFragment: 首发 URL/方法/body（target_fragment_id、split_point、command_id === 传入值；left_name/right_name 缺省时 body 不含该键）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(SPLIT_PREVIEW_OK, 200);
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/split",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    target_fragment_id: "f1",
    split_point: 2,
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(body.left_name, undefined, "left_name 缺省时 body 不含该键");
  assert.equal(body.right_name, undefined, "right_name 缺省时 body 不含该键");
  assert.equal(res.ok, true);
});

test("previewSplitFragment: pid/fid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(SPLIT_PREVIEW_OK, 200);
  });
  await previewSplitFragment(client, "p/1?a", "f 1", {
    targetFragmentId: "f 1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/fragments/" +
      encodeURIComponent("f 1") +
      "/split",
  );
});

test("previewSplitFragment: left_name/right_name 附加（非空时 body.left_name/right_name === 传入值）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(SPLIT_PREVIEW_OK, 200);
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    leftName: "片段甲（左）",
    rightName: "片段甲（右）",
    commandId: "cmd-1",
  });
  const body = JSON.parse(captured.opts.body);
  assert.equal(body.left_name, "片段甲（左）");
  assert.equal(body.right_name, "片段甲（右）");
  assert.equal(res.ok, true);
});

test("previewSplitFragment: 200（恰 4 字段 {preview_id, kind:'fragment_split', baseline, impact}）→ ok、data.preview_id/data.baseline 可用", async () => {
  const client = makeClient(async () => okJson(SPLIT_PREVIEW_OK, 200));
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.preview_id, "preview-3");
  assert.equal(res.data.kind, "fragment_split");
  assert.equal(res.data.baseline.fragment.id, "frag-1");
  assert.equal(res.data.baseline.fragment.revision, 2);
  assert.equal(res.data.baseline.range_set.cas, 5);
  assert.deepEqual(
    norm(Object.keys(res.data).sort()),
    ["baseline", "impact", "kind", "preview_id"],
    "恰 4 字段",
  );
});

test("previewSplitFragment: 422 range_invalid（details.rule === \"empty\"）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "range_invalid",
        message:
          "split_point 等于片段范围端点，无法形成非空半段：split_point=0，当前范围 [0, 4)（正文长度 100 个 UTF-16 单位）。",
        details: { rule: "empty" },
      },
    });
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 0,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "range_invalid");
  assert.equal(res.details.rule, "empty");
  assert.ok(
    res.message.includes("正文长度") &&
      res.message.includes("当前范围") &&
      res.message.includes("split_point"),
    "message 须含 canonical 长度、当前 range 与 split_point",
  );
});

test("previewSplitFragment: 422 range_invalid（details.rule === \"proxy_split\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "range_invalid",
        message:
          "左半段边界劈开了代理对：split_point=2，当前范围 [0, 4)（正文长度 100 个 UTF-16 单位）。",
        details: { rule: "proxy_split" },
      },
    });
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "range_invalid");
  assert.equal(res.details.rule, "proxy_split");
});

test("previewSplitFragment: 422 precondition_failed（details.reason === \"already_retired\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "precondition_failed",
        message: "该片段已退役，不能再次拆分。",
        details: {
          reason: "already_retired",
          blocked_by: { kind: "fragment", id: "frag-1", revision: 2 },
        },
      },
    });
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.details.reason, "already_retired");
});

test("previewSplitFragment: 422 validation_failed（violations，left_name rule=max_length）→ 不重试、透传", async () => {
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
              field: "left_name",
              rule: "max_length",
              message: "left_name 长度不得超过 120 个字符（当前 121）。",
            },
          ],
        },
      },
    });
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    leftName: "x".repeat(121),
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "left_name");
  assert.equal(res.details.violations[0].rule, "max_length");
});

test("previewSplitFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("previewSplitFragment: 404 not_found → 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "fragment", id: "f1" },
      },
    });
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "fragment", id: "f1" });
});

test("previewSplitFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await previewSplitFragment(client, "p1", "f1", {
    targetFragmentId: "f1",
    splitPoint: 2,
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// applySplitFragment（mock fetch，同 confirmFragment 的
// makeClient/okJson/errJson 模式）
// ---------------------------------------------------------------------------

test("applySplitFragment: 首发 URL/方法/body（preview_id、command_id === 传入值、expected_revision、expected_range_set_revision）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(SPLIT_APPLY_OK, 200);
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/fragments/f1/split/apply",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    preview_id: "preview-3",
    expected_revision: 2,
    expected_range_set_revision: 5,
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("applySplitFragment: 200（F9 冻结形状 {left, right, original}：left/right 各 object_ref(revision=1)+range、original object_ref(revision=current)+state='retired'）→ ok、data.left/data.right/data.original 可用", async () => {
  const client = makeClient(async () => okJson(SPLIT_APPLY_OK, 200));
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.left.object_ref.revision, 1, "左新片段 revision=1");
  assert.equal(res.data.right.object_ref.revision, 1, "右新片段 revision=1");
  assert.equal(res.data.left.range.start, 0);
  assert.equal(res.data.left.range.end, 2);
  assert.equal(res.data.right.range.start, 2);
  assert.equal(res.data.right.range.end, 4);
  assert.equal(res.data.original.object_ref.id, "frag-1", "原片段 ID");
  assert.equal(res.data.original.object_ref.revision, 2, "原片段 revision=current");
  assert.equal(res.data.original.state, "retired", "原片段 state='retired'");
});

test("applySplitFragment: 409 preview_stale（details.preview_id + details.conflicts）→ 不重试（请求恰 1 次）、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "preview_stale",
        message: "预览已过期或状态已变化，请重新预览。",
        details: {
          preview_id: "preview-3",
          conflicts: [
            {
              object: { kind: "preview", id: "preview-3" },
              expected: "pending",
              actual: "expired",
            },
          ],
        },
      },
    });
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "preview_stale");
  assert.equal(res.details.preview_id, "preview-3");
  assert.equal(res.details.conflicts.length, 1);
});

test("applySplitFragment: 409 revision_conflict（details.object.kind === \"range_set\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "范围集合版本已变化，请刷新片段列表后重试。",
        details: {
          object: { kind: "range_set", id: "rs-1" },
          expected: 5,
          actual: 6,
        },
      },
    });
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "range_set");
});

test("applySplitFragment: 409 revision_conflict（details.object.kind === \"fragment\"）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "片段版本已变化，请刷新片段详情后重试。",
        details: {
          object: { kind: "fragment", id: "frag-1" },
          expected: 2,
          actual: 3,
        },
      },
    });
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.equal(res.details.object.kind, "fragment");
});

test("applySplitFragment: 422 validation_failed（violations，rule=reused）→ 不重试、透传", async () => {
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
              field: "command_id",
              rule: "reused",
              message: "command_id 已被其他命令使用，请生成新的。",
            },
          ],
        },
      },
    });
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations[0].field, "command_id");
  assert.equal(res.details.violations[0].rule, "reused");
});

test("applySplitFragment: 404 not_found（kind=preview）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "preview", id: "preview-3" },
      },
    });
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "preview", id: "preview-3" });
});

test("applySplitFragment: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new TypeError("Failed to fetch");
  });
  const res = await applySplitFragment(client, "p1", "f1", {
    previewId: "preview-3",
    commandId: "cmd-1",
    expectedRevision: 2,
    expectedRangeSetRevision: 5,
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// FragmentActions.tsx 拆分 UI 字符串断言（selection.test.cjs 模式）
// ---------------------------------------------------------------------------

test("FragmentActions: 拆分按钮仅 candidate/confirmed（retired 不显示拆分）", () => {
  assert.ok(
    actionsSrc.includes(
      'fragment.state === "candidate" || fragment.state === "confirmed"',
    ),
    "拆分判定须为 state === \"candidate\" 或 state === \"confirmed\"",
  );
  assert.ok(
    actionsSrc.includes('fragment-actions__split"'),
    "「拆分」按钮缺失",
  );
  assert.ok(actionsSrc.includes("拆分"), "「拆分」按钮文案缺失");
  assert.ok(
    !actionsSrc.includes('state === "retired"'),
    "retired 行不得显示拆分（也不显示确认）",
  );
});

test("FragmentActions: 两步显式（阶段 1「预览」→ 阶段 2「确认拆分」）+ 确认拆分仅 preview 成功后可点击", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__split-form"),
    "行内拆分表单容器缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__split-preview"),
    "「预览」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__split-apply"),
    "「确认拆分」按钮缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__split-cancel"),
    "「取消」按钮缺失",
  );
  assert.ok(actionsSrc.includes("确认拆分"), "「确认拆分」按钮文案缺失");
  assert.ok(
    actionsSrc.includes('stage === "confirm"'),
    "阶段 2（确认拆分）判定缺失",
  );
  assert.ok(
    actionsSrc.includes('stage === "preview"'),
    "阶段 1（预览）判定缺失",
  );
  const applySrc =
    actionsSrc.match(/async function handleSplitApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("preview === undefined"),
    "无 preview（未预览成功）时确认拆分不得执行（仅 preview 成功后可点击）",
  );
});

test("FragmentActions: 拆分点数字输入框（初始值 = Math.floor((start+end)/2) 中点）+ left_name/right_name 可选输入框", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__split-point"),
    "拆分点输入框缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__split-left"),
    "左名输入框缺失",
  );
  assert.ok(
    actionsSrc.includes("fragment-actions__split-right"),
    "右名输入框缺失",
  );
  const openSrc =
    actionsSrc.match(/function handleSplitOpen[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(openSrc.length > 0, "handleSplitOpen 缺失");
  assert.ok(
    openSrc.includes("Math.floor((fragment.range.start + fragment.range.end) / 2)"),
    "初始拆分点须取 Math.floor((start+end)/2)（当前范围中点）",
  );
});

test("FragmentActions: 本地校验（Number.isInteger + 严格介于端点之间，先于 previewSplitFragment 调用）", () => {
  const previewSrc =
    actionsSrc.match(/async function handleSplitPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(previewSrc.length > 0, "handleSplitPreview 缺失");
  assert.ok(
    previewSrc.includes("Number.isInteger"),
    "Number.isInteger 判定缺失",
  );
  const intCheck = previewSrc.indexOf("Number.isInteger");
  const call = previewSrc.indexOf("previewSplitFragment");
  assert.ok(
    intCheck !== -1 && call !== -1 && intCheck < call,
    "整数判定须先于 previewSplitFragment（本地拦截，不发请求）",
  );
  assert.ok(
    previewSrc.includes("fragment.range.start < splitPoint && splitPoint < fragment.range.end"),
    "严格介于端点之间判定缺失",
  );
  const rangeCheck = previewSrc.indexOf("fragment.range.start < splitPoint");
  assert.ok(
    rangeCheck !== -1 && call !== -1 && rangeCheck < call,
    "端点判定须先于 previewSplitFragment（本地拦截，不发请求）",
  );
  assert.ok(previewSrc.includes("return;"), "本地拦截须提前返回（保留表单）");
  assert.ok(
    previewSrc.includes("拆分点必须为整数。"),
    "非整数本地提示文案缺失",
  );
  assert.ok(
    previewSrc.includes("拆分点必须严格介于当前范围端点之间。"),
    "端点本地提示文案缺失",
  );
});

test("FragmentActions: left_name/right_name 空串 → undefined（不附加进 body）", () => {
  const previewSrc =
    actionsSrc.match(/async function handleSplitPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    previewSrc.includes('leftName.trim() === "" ? undefined : leftName.trim()'),
    "left_name 空串须转 undefined（不附加进 body）",
  );
  assert.ok(
    previewSrc.includes('rightName.trim() === "" ? undefined : rightName.trim()'),
    "right_name 空串须转 undefined（不附加进 body）",
  );
});

test("FragmentActions: 显式点击接线（onClick + previewSplitFragment/applySplitFragment）+ makeCommandId() 每次点击一次", () => {
  const previewSrc =
    actionsSrc.match(/async function handleSplitPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(previewSrc.length > 0, "handleSplitPreview 缺失");
  assert.ok(
    previewSrc.includes("previewSplitFragment"),
    "预览须调用 previewSplitFragment",
  );
  assert.ok(
    previewSrc.includes("makeCommandId()"),
    "预览须每次点击生成一次 commandId",
  );
  const applySrc =
    actionsSrc.match(/async function handleSplitApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(applySrc.length > 0, "handleSplitApply 缺失");
  assert.ok(
    applySrc.includes("applySplitFragment"),
    "确认拆分须调用 applySplitFragment",
  );
  assert.ok(
    applySrc.includes("makeCommandId()"),
    "确认拆分须每次点击生成一次 commandId",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleSplitPreview(fragment)}"),
    "「预览」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleSplitApply(fragment)}"),
    "「确认拆分」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleSplitOpen(fragment)}"),
    "「拆分」按钮 onClick 接线缺失",
  );
});

test("FragmentActions: 拆分 expected_revision 取 object_ref.revision；expected_range_set_revision 取 preview.baseline.range_set.cas", () => {
  const applySrc =
    actionsSrc.match(/async function handleSplitApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("object_ref.revision"),
    "expected_revision 须取 object_ref.revision（真实服务端值）",
  );
  assert.ok(
    applySrc.includes("baseline.range_set.cas"),
    "expected_range_set_revision 须取 preview.baseline.range_set.cas（冻结基准）",
  );
});

test("FragmentActions: 拆分 impact 摘要展示（affected/needs_review/preservable 计数）", () => {
  assert.ok(
    actionsSrc.includes("fragment-actions__split-impact"),
    "拆分 impact 摘要缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.affected.length"),
    "impact 摘要 affected 计数缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.needs_review.length"),
    "impact 摘要 needs_review 计数缺失",
  );
  assert.ok(
    actionsSrc.includes("impact.preservable.length"),
    "impact 摘要 preservable 计数缺失",
  );
});

test("FragmentActions: 拆分取消零 API 调用（onCancel 路径无 previewSplitFragment/applySplitFragment）", () => {
  const cancelSrc =
    actionsSrc.match(/function handleSplitCancel[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(cancelSrc.length > 0, "handleSplitCancel 缺失");
  assert.ok(
    cancelSrc.includes("setSplitting"),
    "取消须以 setSplitting 回到按钮态",
  );
  assert.ok(
    !cancelSrc.includes("previewSplitFragment"),
    "取消不得调用 previewSplitFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("applySplitFragment"),
    "取消不得调用 applySplitFragment（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("clientObj"),
    "取消不得使用 client",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleSplitCancel(fid)}"),
    "取消按钮 onClick 接线缺失",
  );
});

test("FragmentActions: 拆分失败保留表单（setErrors 保留、不关表单、不退出）", () => {
  const previewSrc =
    actionsSrc.match(/async function handleSplitPreview[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    previewSrc.includes("setErrors"),
    "预览失败须 setErrors 保留行内错误",
  );
  assert.ok(
    !previewSrc.includes("open: false"),
    "预览失败不得关闭拆分表单（保留表单）",
  );
  const applySrc =
    actionsSrc.match(/async function handleSplitApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("setErrors"),
    "确认拆分失败须 setErrors 保留行内错误",
  );
  assert.ok(
    !applySrc.includes("open: false"),
    "确认拆分失败不得关闭拆分表单（保留表单与 impact 摘要）",
  );
  assert.ok(applySrc.includes("setSubmitting"), "提交态管理缺失");
});

test("FragmentActions: 拆分成功 → 清除该行错误 + 内部重取（refreshTick）；新快照清除 splitting（表单收起）", () => {
  const applySrc =
    actionsSrc.match(/async function handleSplitApply[^{]*\{([\s\S]*?)\n  \}/)
      ?.[1] ?? "";
  assert.ok(
    applySrc.includes("setRefreshTick"),
    "拆分成功须内部重取（refreshTick，原片段退役、左/右新片段出现）",
  );
  assert.ok(
    applySrc.includes("delete next[fid]"),
    "拆分成功须清除该行错误",
  );
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(effectBlock[0].includes("setSplitting({})"), "新快照须清除拆分表单（防陈旧表单/错误/预览）");
});

test("FragmentActions: 无自动拆分（useEffect 内不调 previewSplitFragment/applySplitFragment）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("previewSplitFragment"),
    "useEffect 内不得调用 previewSplitFragment（无自动拆分）",
  );
  assert.ok(
    !effectBlock[0].includes("applySplitFragment"),
    "useEffect 内不得调用 applySplitFragment（无自动拆分）",
  );
  assert.ok(!actionsSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!actionsSrc.includes("示例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("样例"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("Lorem"), "无硬编码样例");
  assert.ok(!actionsSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(
    !actionsSrc.includes("window.location"),
    "不得导航（window.location）",
  );
});
