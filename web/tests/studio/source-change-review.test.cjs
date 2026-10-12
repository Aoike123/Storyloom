const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// SOURCE-14-g 验收：api.ts 追加纯逻辑（previewSourceActivate /
// applySourceActivate / listSourceRevisions，vm+transpile）+
// SourceChangeReview.tsx（字符串断言）。
// 模式与 fragment-actions.test.cjs / selection.test.cjs 完全一致。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const apiTs = path.join(root, "features", "studio", "source", "api.ts");
const reviewTsx = path.join(
  root,
  "features",
  "studio",
  "source",
  "components",
  "SourceChangeReview.tsx",
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
const { previewSourceActivate, applySourceActivate, listSourceRevisions } =
  apiContext.exports;

// ---------------------------------------------------------------------------
// vm 加载 client.ts（真客户端 + 每测试注入 mock fetch）
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

// S4 预览响应（恰 4 字段：preview_id、kind、baseline、impact）
const PREVIEW_DTO = {
  preview_id: "pv-1",
  kind: "source_activate",
  baseline: {
    active_revision_id: "rev-active",
    target_revision_id: "rev1",
    target_canonical_hash: "hash-target",
  },
  impact: {
    affected: [
      { object_ref: { kind: "fragment", id: "frag-1", revision: 2 } },
    ],
    needs_review: [
      { object_ref: { kind: "fragment", id: "frag-2", revision: 1 } },
    ],
    preservable: [],
  },
};

// P3 项目 DTO（七字段；active_source_revision_id == target）
const PROJECT_DTO = {
  id: "p1",
  name: "项目甲",
  description: "",
  visibility: "private",
  active_source_revision_id: "rev1",
  created_at: 1,
  updated_at: 2,
};

// S2 来源版本列表（倒序；fragment_count 为计算值）
const REVISIONS_DTO = {
  items: [
    {
      id: "rev2",
      previous_revision_id: "rev1",
      raw_hash: "raw-2",
      canonical_hash: "canon-2",
      offset_policy: "lf-utf16-v1",
      char_length: 100,
      created_at: 2,
      is_active: false,
      fragment_count: 3,
    },
    {
      id: "rev1",
      previous_revision_id: null,
      raw_hash: "raw-1",
      canonical_hash: "canon-1",
      offset_policy: "lf-utf16-v1",
      char_length: 80,
      created_at: 1,
      is_active: true,
      fragment_count: 1,
    },
  ],
  next_cursor: null,
};

// ---------------------------------------------------------------------------
// previewSourceActivate（mock fetch）
// ---------------------------------------------------------------------------

test("previewSourceActivate: 首发 URL/方法/body（kind=source_activate、target_revision_id、command_id === 传入值）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(PREVIEW_DTO, 200);
  });
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/source-changes/preview",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    kind: "source_activate",
    target_revision_id: "rev1",
    command_id: "cmd-1",
  });
  assert.equal(body.command_id, "cmd-1", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("previewSourceActivate: pid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(PREVIEW_DTO, 200);
  });
  await previewSourceActivate(client, "p/1?a", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/source-changes/preview",
  );
});

test("previewSourceActivate: 200（恰 4 字段 {preview_id, kind, baseline, impact}）→ ok、data.preview_id/data.baseline 可用", async () => {
  const client = makeClient(async () => okJson(PREVIEW_DTO, 200));
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.preview_id, "pv-1");
  assert.equal(res.data.kind, "source_activate");
  assert.equal(res.data.baseline.active_revision_id, "rev-active");
  assert.equal(res.data.baseline.target_revision_id, "rev1");
  assert.equal(res.data.baseline.target_canonical_hash, "hash-target");
  assert.ok(Array.isArray(res.data.impact.affected));
  assert.ok(Array.isArray(res.data.impact.needs_review));
  assert.ok(Array.isArray(res.data.impact.preservable));
});

test("previewSourceActivate: 200（幂等重放）→ ok", async () => {
  const client = makeClient(async () => okJson(PREVIEW_DTO, 200));
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.preview_id, "pv-1");
});

test("previewSourceActivate: 422 validation_failed（violations，target_revision_id rule=not_found）→ 不重试（请求恰 1 次）、透传", async () => {
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
              field: "target_revision_id",
              rule: "not_found",
              message: "指定正文版本不存在。",
            },
          ],
        },
      },
    });
  });
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev-x",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations.length, 1);
  assert.equal(res.details.violations[0].field, "target_revision_id");
  assert.equal(res.details.violations[0].rule, "not_found");
});

test("previewSourceActivate: 422 precondition_failed（details.reason === already_active）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(422, {
      error: {
        code: "precondition_failed",
        message: "该正文版本已是当前活跃版本。",
        details: {
          reason: "already_active",
          blocked_by: {
            kind: "source_revision",
            id: "rev1",
            revision: null,
          },
        },
      },
    });
  });
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.details.reason, "already_active");
  assert.deepEqual(norm(res.details.blocked_by), {
    kind: "source_revision",
    id: "rev1",
    revision: null,
  });
});

test("previewSourceActivate: 422 validation_failed（violations，command_id rule=reused）→ 不重试、透传", async () => {
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
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
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

test("previewSourceActivate: 404 not_found → 不重试、透传", async () => {
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
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "project", id: "p1" });
});

test("previewSourceActivate: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new Error("fetch failed");
  });
  const res = await previewSourceActivate(client, "p1", {
    targetRevisionId: "rev1",
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// applySourceActivate（mock fetch）
// ---------------------------------------------------------------------------

test("applySourceActivate: 首发 URL/方法/body（preview_id、command_id === 传入值、expected_active_revision_id）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(PROJECT_DTO, 200);
  });
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/source-changes/apply",
  );
  assert.equal(captured.opts.method, "POST");
  const body = JSON.parse(captured.opts.body);
  assert.deepEqual(norm(body), {
    preview_id: "pv-1",
    expected_active_revision_id: "rev-active",
    command_id: "cmd-2",
  });
  assert.equal(body.command_id, "cmd-2", "command_id === 传入值");
  assert.equal(res.ok, true);
});

test("applySourceActivate: pid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(PROJECT_DTO, 200);
  });
  await applySourceActivate(client, "p/1?a", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/source-changes/apply",
  );
});

test("applySourceActivate: 200（项目 DTO，active_source_revision_id == target）→ ok、data.active_source_revision_id 可用", async () => {
  const client = makeClient(async () => okJson(PROJECT_DTO, 200));
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(res.ok, true);
  assert.equal(res.data.id, "p1");
  assert.equal(res.data.active_source_revision_id, "rev1");
});

test("applySourceActivate: 409 preview_stale（details.preview_id + details.conflicts）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "preview_stale",
        message: "预览已过期或已失效，请重新预览。",
        details: {
          preview_id: "pv-1",
          conflicts: [
            {
              object: { kind: "project", id: "p1" },
              expected: "rev-active",
              actual: "rev-other",
            },
          ],
        },
      },
    });
  });
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "preview_stale");
  assert.equal(res.details.preview_id, "pv-1");
  assert.ok(Array.isArray(res.details.conflicts));
});

test("applySourceActivate: 409 revision_conflict（details.object.kind === project）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(409, {
      error: {
        code: "revision_conflict",
        message: "项目活跃版本已变化，请刷新后重试。",
        details: {
          object: { kind: "project", id: "p1" },
          expected: "rev-active",
          actual: "rev-other",
        },
      },
    });
  });
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.deepEqual(norm(res.details.object), { kind: "project", id: "p1" });
});

test("applySourceActivate: 422 validation_failed（violations，preview_id rule=mismatch）→ 不重试、透传", async () => {
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
              field: "preview_id",
              rule: "mismatch",
              message: "preview_id 与命令记录不符。",
            },
          ],
        },
      },
    });
  });
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations.length, 1);
  assert.equal(res.details.violations[0].field, "preview_id");
  assert.equal(res.details.violations[0].rule, "mismatch");
});

test("applySourceActivate: 404 not_found（kind=preview）→ 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "preview", id: "pv-1" },
      },
    });
  });
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "preview", id: "pv-1" });
});

test("applySourceActivate: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    throw new Error("fetch failed");
  });
  const res = await applySourceActivate(client, "p1", {
    previewId: "pv-1",
    commandId: "cmd-2",
    expectedActiveRevisionId: "rev-active",
  });
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// listSourceRevisions（mock fetch）
// ---------------------------------------------------------------------------

test("listSourceRevisions: 首发 URL/方法（GET，无 body）+ 200 → ok、data.items 可用", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(REVISIONS_DTO, 200);
  });
  const res = await listSourceRevisions(client, "p1");
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/sources",
  );
  assert.equal(captured.opts.method, "GET");
  assert.equal(captured.opts.body, undefined);
  assert.equal(res.ok, true);
  assert.equal(res.data.items.length, 2);
  assert.equal(res.data.items[0].id, "rev2");
  assert.equal(res.data.items[0].is_active, false);
  assert.equal(res.data.items[1].id, "rev1");
  assert.equal(res.data.items[1].is_active, true);
  assert.equal(res.data.next_cursor, null);
});

test("listSourceRevisions: pid 特殊字符经 encodeURIComponent", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(REVISIONS_DTO, 200);
  });
  await listSourceRevisions(client, "p/1?a");
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/" +
      encodeURIComponent("p/1?a") +
      "/sources",
  );
});

test("listSourceRevisions: cursor 非空时附加 ?cursor=（encodeURIComponent）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(REVISIONS_DTO, 200);
  });
  await listSourceRevisions(client, "p1", "c0/1?");
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/sources" +
      "?cursor=" +
      encodeURIComponent("c0/1?"),
  );
});

test("listSourceRevisions: 404 not_found → 不重试、透传", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, opts });
    return errJson(404, {
      error: {
        code: "not_found",
        message: "对象不存在或不属于当前项目。",
        details: { kind: "project", id: "p1" },
      },
    });
  });
  const res = await listSourceRevisions(client, "p1");
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "project", id: "p1" });
});

test("listSourceRevisions: network → 不重试、code=network", async () => {
  const calls = [];
  const client = makeClient(async (url, opts) => {
    calls.push({ url, opts });
    throw new Error("fetch failed");
  });
  const res = await listSourceRevisions(client, "p1");
  assert.equal(calls.length, 1, "恰 1 次请求，不重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// SourceChangeReview.tsx 字符串断言（fragment-actions.test.cjs 模式）
// ---------------------------------------------------------------------------

const reviewSrc = fs.readFileSync(reviewTsx, "utf8");

test("SourceChangeReview: 正文变更按钮项目级（非行级）", () => {
  assert.ok(
    reviewSrc.includes("source-change-review__open"),
    "「正文变更」按钮缺失",
  );
  assert.ok(reviewSrc.includes("正文变更"), "「正文变更」按钮文案缺失");
  // 项目级：按钮/表单在 source-change-review__ready 容器内（列表/行外），
  // 组件无行级（无 li/行 state 映射）
  assert.ok(
    reviewSrc.includes("source-change-review__ready"),
    "项目级动作区容器缺失",
  );
  assert.ok(!reviewSrc.includes("<li"), "组件不得有行级列表（非行级）");
});

test("SourceChangeReview: 两步显式（阶段 1「预览」→ 阶段 2「确认激活」）+ 确认激活仅 preview 成功后可点击", () => {
  assert.ok(
    reviewSrc.includes("source-change-review__form"),
    "两步表单容器缺失",
  );
  assert.ok(
    reviewSrc.includes("source-change-review__preview"),
    "「预览」按钮缺失",
  );
  assert.ok(
    reviewSrc.includes("source-change-review__apply"),
    "「确认激活」按钮缺失",
  );
  assert.ok(
    reviewSrc.includes("source-change-review__cancel"),
    "「取消」按钮缺失",
  );
  assert.ok(reviewSrc.includes("预览"), "「预览」按钮文案缺失");
  assert.ok(reviewSrc.includes("确认激活"), "「确认激活」按钮文案缺失");
  assert.ok(
    reviewSrc.includes('stage === "confirm"'),
    "阶段 2（确认激活）判定缺失",
  );
  assert.ok(
    reviewSrc.includes('stage === "preview"'),
    "阶段 1（预览）判定缺失",
  );
  const applySrc =
    reviewSrc.match(/async function handleApply[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    applySrc.includes("preview === undefined"),
    "无 preview（未预览成功）时确认激活不得执行（仅 preview 成功后可点击）",
  );
});

test("SourceChangeReview: 目标版本下拉选择器（选项 = is_active === false 的源版本，文本 = 版本 id + created_at 日期）", () => {
  assert.ok(
    reviewSrc.includes("source-change-review__select"),
    "目标版本下拉选择器缺失",
  );
  // 选项 = 当前列表中 is_active === false 的源版本（当前 active 不显示）
  assert.ok(
    reviewSrc.includes("r.is_active === false"),
    "选择器选项须为 is_active === false 的源版本",
  );
  // 选项文本 = 版本 id + created_at 日期
  assert.ok(
    reviewSrc.includes("formatCreatedAt(r.created_at)"),
    "选项文本须含 created_at 日期",
  );
});

test("SourceChangeReview: 本地校验（targetRevisionId !== \"\"，先于 previewSourceActivate 调用）", () => {
  const previewSrc =
    reviewSrc.match(/async function handlePreview[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(previewSrc.length > 0, "handlePreview 缺失");
  assert.ok(
    previewSrc.includes('targetRevisionId === ""'),
    "目标版本未选判定缺失",
  );
  const check = previewSrc.indexOf('targetRevisionId === ""');
  const call = previewSrc.indexOf("previewSourceActivate");
  assert.ok(
    check !== -1 && call !== -1 && check < call,
    "未选判定须先于 previewSourceActivate（本地拦截，不发请求）",
  );
  assert.ok(previewSrc.includes("return;"), "本地拦截须提前返回（保留表单）");
  assert.ok(
    previewSrc.includes("请选择一个目标版本。"),
    "本地提示文案缺失",
  );
});

test("SourceChangeReview: 显式点击接线（onClick + previewSourceActivate/applySourceActivate）+ makeCommandId() 每次点击一次", () => {
  const previewSrc =
    reviewSrc.match(/async function handlePreview[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(previewSrc.length > 0, "handlePreview 缺失");
  assert.ok(
    previewSrc.includes("previewSourceActivate"),
    "预览须调用 previewSourceActivate",
  );
  assert.ok(
    previewSrc.includes("makeCommandId()"),
    "预览须每次点击生成一次 commandId",
  );
  const applySrc =
    reviewSrc.match(/async function handleApply[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(applySrc.length > 0, "handleApply 缺失");
  assert.ok(
    applySrc.includes("applySourceActivate"),
    "确认激活须调用 applySourceActivate",
  );
  assert.ok(
    applySrc.includes("makeCommandId()"),
    "确认激活须每次点击生成一次 commandId",
  );
  assert.ok(
    reviewSrc.includes("onClick={() => void handlePreview()}"),
    "「预览」按钮 onClick 接线缺失",
  );
  assert.ok(
    reviewSrc.includes("onClick={() => void handleApply()}"),
    "「确认激活」按钮 onClick 接线缺失",
  );
  assert.ok(
    reviewSrc.includes("onClick={() => handleOpen()}"),
    "「正文变更」按钮 onClick 接线缺失",
  );
});

test("SourceChangeReview: expected_active_revision_id 取 project.active_source_revision_id（真实服务端值）", () => {
  const applySrc =
    reviewSrc.match(/async function handleApply[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    applySrc.includes("active_source_revision_id"),
    "expected_active_revision_id 须取项目的 active_source_revision_id",
  );
  assert.ok(
    applySrc.includes("state.project") || applySrc.includes("project"),
    "expected_active_revision_id 须取当前项目（真实服务端值）",
  );
});

test("SourceChangeReview: impact 摘要展示（affected/needs_review/preservable 计数）", () => {
  assert.ok(
    reviewSrc.includes("source-change-review__impact"),
    "impact 摘要缺失",
  );
  assert.ok(
    reviewSrc.includes("impact.affected.length"),
    "impact 摘要 affected 计数缺失",
  );
  assert.ok(
    reviewSrc.includes("impact.needs_review.length"),
    "impact 摘要 needs_review 计数缺失",
  );
  assert.ok(
    reviewSrc.includes("impact.preservable.length"),
    "impact 摘要 preservable 计数缺失",
  );
});

test("SourceChangeReview: 取消零 API 调用（onCancel 路径无 previewSourceActivate/applySourceActivate）", () => {
  const cancelSrc =
    reviewSrc.match(/function handleCancel[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(cancelSrc.length > 0, "handleCancel 缺失");
  assert.ok(
    cancelSrc.includes("setReviewing"),
    "取消须以 setReviewing 回到按钮态",
  );
  assert.ok(
    !cancelSrc.includes("previewSourceActivate"),
    "取消不得调用 previewSourceActivate（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("applySourceActivate"),
    "取消不得调用 applySourceActivate（零 API 调用）",
  );
  assert.ok(
    !cancelSrc.includes("clientObj"),
    "取消不得使用 client",
  );
  assert.ok(
    reviewSrc.includes("onClick={() => handleCancel()}"),
    "取消按钮 onClick 接线缺失",
  );
});

test("SourceChangeReview: 失败保留表单（setReviewError 保留、不退出）", () => {
  const previewSrc =
    reviewSrc.match(/async function handlePreview[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    previewSrc.includes("setReviewError"),
    "预览失败须 setReviewError 保留行内错误",
  );
  const applySrc =
    reviewSrc.match(/async function handleApply[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    applySrc.includes("setReviewError"),
    "确认激活失败须 setReviewError 保留行内错误",
  );
  assert.ok(
    applySrc.includes("setReviewSubmitting"),
    "提交态管理缺失",
  );
});

test("SourceChangeReview: 成功 → 内部重取（refreshTick）；新快照清除 reviewing（表单收起）", () => {
  const applySrc =
    reviewSrc.match(/async function handleApply[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    applySrc.includes("setRefreshTick"),
    "确认激活成功须内部重取（refreshTick，项目 active_source_revision_id 更新）",
  );
  const effectBlock = reviewSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    effectBlock[0].includes("setReviewing"),
    "新快照须清除两步表单（防陈旧表单/错误/预览）",
  );
});

test("SourceChangeReview: 行内错误文案（already_active/not_found/preview_stale/revision_conflict/validation_failed 每条 message/其他 message）", () => {
  const errSrc =
    reviewSrc.match(/function reviewErrorText[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(errSrc.length > 0, "reviewErrorText 缺失");
  assert.ok(
    errSrc.includes('reason === "already_active"'),
    "already_active 判定缺失",
  );
  assert.ok(
    errSrc.includes("该版本已是当前活跃版本。"),
    "already_active 文案缺失",
  );
  assert.ok(
    errSrc.includes("指定正文版本不存在。"),
    "not_found 文案缺失",
  );
  assert.ok(
    errSrc.includes("预览已过期，请重新预览。"),
    "preview_stale 文案缺失",
  );
  assert.ok(
    errSrc.includes("版本已变化，请刷新后重试。"),
    "revision_conflict 文案缺失",
  );
  assert.ok(
    errSrc.includes("violations"),
    "validation_failed 须读 details.violations 每条 message",
  );
  assert.ok(
    errSrc.includes("error.message"),
    "其他错误须显示 error.message",
  );
});

test("SourceChangeReview: 双击防护（提交中对应按钮 disabled）", () => {
  assert.ok(
    reviewSrc.includes("disabled={reviewSubmitting}"),
    "提交中对应按钮须 disabled（防双击）",
  );
  const disabledCount = (reviewSrc.match(/disabled={reviewSubmitting}/g) ??
    []).length;
  assert.ok(
    disabledCount >= 2,
    "「预览」与「确认激活」按钮均须 disabled（防双击）",
  );
});

test("SourceChangeReview: 无自动激活（useEffect 内不调 previewSourceActivate/applySourceActivate）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  const effectBlock = reviewSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("previewSourceActivate"),
    "useEffect 内不得调用 previewSourceActivate（无自动激活）",
  );
  assert.ok(
    !effectBlock[0].includes("applySourceActivate"),
    "useEffect 内不得调用 applySourceActivate（无自动激活）",
  );
  assert.ok(!reviewSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!reviewSrc.includes("示例"), "无硬编码样例");
  assert.ok(!reviewSrc.includes("样例"), "无硬编码样例");
  assert.ok(!reviewSrc.includes("Lorem"), "无硬编码样例");
  assert.ok(!reviewSrc.includes("router.push"), "不得导航（router.push）");
  assert.ok(
    !reviewSrc.includes("window.location"),
    "不得导航（window.location）",
  );
});
