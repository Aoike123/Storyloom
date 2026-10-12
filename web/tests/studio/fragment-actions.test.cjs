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
