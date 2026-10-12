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

test("FragmentActions: commandId 生成（crypto.randomUUID 每次点击一次；确认/保存各一次）", () => {
  const helperSrc =
    actionsSrc.match(/function makeCommandId[^{]*\{([\s\S]*?)\n\}/)?.[1] ??
    "";
  assert.ok(
    helperSrc.includes("crypto.randomUUID()"),
    "makeCommandId 须用 crypto.randomUUID()",
  );
  const callCount = (actionsSrc.match(/= makeCommandId\(\)/g) || []).length;
  assert.equal(callCount, 2, "确认与保存各每次点击生成一次 commandId");
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
