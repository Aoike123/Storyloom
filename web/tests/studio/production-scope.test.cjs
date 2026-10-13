const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// SOURCE-14-h 验收：api.ts 追加纯逻辑（createProductionScope /
// getCurrentProductionScope，vm+transpile）+ FragmentActions.tsx 制作范围区块
// （字符串断言）。模式与 source-change-review.test.cjs / fragment-actions.test.cjs
// 完全一致。
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
const { createProductionScope, getCurrentProductionScope } = apiContext.exports;

assert.equal(typeof createProductionScope, "function", "api.ts 缺 createProductionScope 导出");
assert.equal(
  typeof getCurrentProductionScope,
  "function",
  "api.ts 缺 getCurrentProductionScope 导出",
);

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

// 范围 DTO（恰 6 字段；revision 恒 null；excluded_ranges UTF-16 半开、start 升序）
const SCOPE_DTO = {
  object_ref: { kind: "production_scope", id: "scope-1", revision: null },
  source_revision_id: "rev-1",
  fragment_ids: ["frag-a", "frag-b"],
  covered: false,
  excluded_ranges: [
    { start: 3, end: 7 },
    { start: 20, end: 42 },
  ],
  created_at: 1728000000000,
};

// 需转义的 pid（验证 encodeURIComponent）
const RAW_PID = "p 甲/1?";
const ENC_PID = encodeURIComponent(RAW_PID);

// ---------------------------------------------------------------------------
// api 层：createProductionScope（F11）
// ---------------------------------------------------------------------------

test("api createProductionScope: URL/方法/body 精确（pid encodeURIComponent；fragment_ids + command_id；恰 1 次请求；201 → DTO 恰 6 字段）", async () => {
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return okJson(SCOPE_DTO, 201);
  });
  const res = await createProductionScope(client, RAW_PID, {
    fragmentIds: ["frag-a", "frag-b"],
    commandId: "cmd-1",
  });
  assert.equal(calls.length, 1, "请求须恰 1 次");
  assert.equal(
    calls[0].url,
    `http://test.local/api/studio/projects/${ENC_PID}/production-scopes`,
    "URL 须为 F11 冻结路由且 pid encodeURIComponent",
  );
  assert.equal(calls[0].init.method, "POST", "方法须为 POST");
  assert.deepEqual(
    JSON.parse(calls[0].init.body),
    { fragment_ids: ["frag-a", "frag-b"], command_id: "cmd-1" },
    "body 须恰为 fragment_ids + command_id",
  );
  assert.equal(res.ok, true);
  assert.deepEqual(Object.keys(res.data).sort(), [
    "covered",
    "created_at",
    "excluded_ranges",
    "fragment_ids",
    "object_ref",
    "source_revision_id",
  ]);
  assert.deepEqual(norm(res.data), SCOPE_DTO);
});

test("api createProductionScope: fragmentIds 省略时 body 不带 fragment_ids 键（= 全部已确认片段）", async () => {
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return okJson(SCOPE_DTO, 201);
  });
  const res = await createProductionScope(client, "p1", { commandId: "cmd-2" });
  assert.equal(calls.length, 1);
  const body = JSON.parse(calls[0].init.body);
  assert.ok(
    !("fragment_ids" in body),
    "fragmentIds === undefined 时 body 不得带 fragment_ids 键",
  );
  assert.deepEqual(body, { command_id: "cmd-2" }, "body 须恰为 command_id 一键");
  assert.equal(res.ok, true);
});

test("api createProductionScope: 200 幂等重放与 201 同等处理（同一范围 DTO）", async () => {
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return okJson(SCOPE_DTO, 200);
  });
  const res = await createProductionScope(client, "p1", {
    fragmentIds: ["frag-a", "frag-b"],
    commandId: "cmd-3",
  });
  assert.equal(calls.length, 1);
  assert.equal(res.ok, true, "200 重放须与 201 同等解析为 DTO");
  assert.deepEqual(norm(res.data), SCOPE_DTO);
});

test("api createProductionScope: 404 not_found（kind=project）逐字段透传，恰 1 次请求", async () => {
  const body = {
    error: {
      code: "not_found",
      message: "项目不存在。",
      details: { kind: "project", id: RAW_PID },
    },
  };
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return errJson(404, body);
  });
  const res = await createProductionScope(client, RAW_PID, {
    commandId: "cmd-4",
  });
  assert.equal(calls.length, 1, "404 不得重试");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.equal(res.message, "项目不存在。");
  assert.deepEqual(norm(res.details), { kind: "project", id: RAW_PID });
});

test("api createProductionScope: 422 validation_failed（fragment_ids 空数组 → required）逐字段透传（空数组照传 body）", async () => {
  const body = {
    error: {
      code: "validation_failed",
      message: "请求参数校验失败。",
      details: {
        violations: [
          { field: "fragment_ids", rule: "required", message: "请至少选择一个已确认片段。" },
        ],
      },
    },
  };
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return errJson(422, body);
  });
  const res = await createProductionScope(client, "p1", {
    fragmentIds: [],
    commandId: "cmd-5",
  });
  assert.equal(calls.length, 1);
  assert.deepEqual(
    JSON.parse(calls[0].init.body),
    { fragment_ids: [], command_id: "cmd-5" },
    "空数组须照传（由后端 422 required，前端不预拦截）",
  );
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.message, "请求参数校验失败。");
  assert.deepEqual(norm(res.details), body.error.details);
});

test("api createProductionScope: 422 validation_failed（command_id reused）逐字段透传，恰 1 次请求", async () => {
  const body = {
    error: {
      code: "validation_failed",
      message: "请求参数校验失败。",
      details: {
        violations: [
          { field: "command_id", rule: "reused", message: "command_id 已被其他请求使用。" },
        ],
      },
    },
  };
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return errJson(422, body);
  });
  const res = await createProductionScope(client, "p1", {
    fragmentIds: ["frag-a"],
    commandId: "cmd-reused",
  });
  assert.equal(calls.length, 1, "422 不得重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "validation_failed");
  assert.deepEqual(norm(res.details), body.error.details);
});

test("api createProductionScope: 422 precondition_failed（not_confirmed，details.blocked_by 为 ObjectRef）逐字段透传", async () => {
  const body = {
    error: {
      code: "precondition_failed",
      message: "片段「场景甲」尚未确认（当前状态：candidate）。",
      details: {
        reason: "not_confirmed",
        blocked_by: { kind: "fragment", id: "frag-a", revision: 1 },
      },
    },
  };
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return errJson(422, body);
  });
  const res = await createProductionScope(client, "p1", {
    fragmentIds: ["frag-a"],
    commandId: "cmd-6",
  });
  assert.equal(calls.length, 1);
  assert.equal(res.ok, false);
  assert.equal(res.code, "precondition_failed");
  assert.equal(res.message, "片段「场景甲」尚未确认（当前状态：candidate）。");
  assert.deepEqual(norm(res.details), body.error.details);
  assert.deepEqual(norm(res.details.blocked_by), {
    kind: "fragment",
    id: "frag-a",
    revision: 1,
  });
});

test("api createProductionScope: 422 duplicate_scope（details.scope_ref）逐字段透传，恰 1 次请求", async () => {
  const body = {
    error: {
      code: "duplicate_scope",
      message: "相同片段覆盖的制作范围已提交过。",
      details: {
        scope_ref: { kind: "production_scope", id: "scope-0", revision: null },
      },
    },
  };
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return errJson(422, body);
  });
  const res = await createProductionScope(client, "p1", {
    commandId: "cmd-7",
  });
  assert.equal(calls.length, 1);
  assert.equal(res.ok, false);
  assert.equal(res.code, "duplicate_scope");
  assert.equal(res.message, "相同片段覆盖的制作范围已提交过。");
  assert.deepEqual(norm(res.details), body.error.details);
  assert.deepEqual(norm(res.details.scope_ref), {
    kind: "production_scope",
    id: "scope-0",
    revision: null,
  });
});

test("api createProductionScope: 网络错误恰 1 次请求不重试（code=network）", async () => {
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    throw new Error("boom");
  });
  const res = await createProductionScope(client, "p1", { commandId: "cmd-8" });
  assert.equal(calls.length, 1, "网络错误不得重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// api 层：getCurrentProductionScope（F12）
// ---------------------------------------------------------------------------

test("api getCurrentProductionScope: URL/方法精确（GET；pid encodeURIComponent；无 body）；200 → DTO", async () => {
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return okJson(SCOPE_DTO, 200);
  });
  const res = await getCurrentProductionScope(client, RAW_PID);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    `http://test.local/api/studio/projects/${ENC_PID}/production-scopes/current`,
    "URL 须为 F12 current 冻结路由且 pid encodeURIComponent",
  );
  assert.equal(calls[0].init.method, "GET", "方法须为 GET");
  assert.equal(calls[0].init.body, undefined, "GET 不带 body");
  assert.equal(res.ok, true);
  assert.deepEqual(norm(res.data), SCOPE_DTO);
});

test("api getCurrentProductionScope: 404 not_found（kind=production_scope，id=pid）与其他错误一样透传（组件层区分空态）", async () => {
  const body = {
    error: {
      code: "not_found",
      message: "尚未提交制作范围。",
      details: { kind: "production_scope", id: RAW_PID },
    },
  };
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    return errJson(404, body);
  });
  const res = await getCurrentProductionScope(client, RAW_PID);
  assert.equal(calls.length, 1);
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.deepEqual(norm(res.details), { kind: "production_scope", id: RAW_PID });
});

test("api getCurrentProductionScope: 网络错误恰 1 次请求不重试", async () => {
  const calls = [];
  const client = makeClient(async (url, init) => {
    calls.push({ url, init });
    throw new Error("boom");
  });
  const res = await getCurrentProductionScope(client, "p1");
  assert.equal(calls.length, 1, "网络错误不得重试");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
});

// ---------------------------------------------------------------------------
// 组件字符串断言（FragmentActions.tsx 制作范围区块）
// ---------------------------------------------------------------------------

const actionsSrc = fs.readFileSync(actionsTsx, "utf8");
const apiSrc = fs.readFileSync(apiTs, "utf8");

test("FragmentActions: 制作范围区块骨架（空态/覆盖/范围外/清单/表单文案齐备）", () => {
  assert.ok(
    actionsSrc.includes('className="fragment-actions__scope"'),
    "制作范围项目级区块缺失",
  );
  assert.ok(actionsSrc.includes("尚未提交制作范围。"), "空态文案缺失");
  assert.ok(actionsSrc.includes("完整覆盖正文"), "covered=true 文案缺失");
  assert.ok(actionsSrc.includes("范围外段落"), "范围外段落清单缺失");
  assert.ok(actionsSrc.includes("覆盖片段清单"), "覆盖片段清单缺失");
  assert.ok(actionsSrc.includes("提交制作范围"), "项目级提交按钮缺失");
  assert.ok(actionsSrc.includes("全部已确认片段"), "「全部已确认片段」开关缺失");
  assert.ok(
    actionsSrc.includes("请至少选择一个已确认片段。"),
    "零勾选本地提示缺失",
  );
  assert.ok(
    actionsSrc.includes("相同片段覆盖的制作范围已提交过，历史只追加不重复。"),
    "duplicate_scope 文案缺失",
  );
  assert.ok(
    actionsSrc.includes("制作范围读取失败："),
    "F12 读取失败行内错误缺失",
  );
});

test("FragmentActions: 自取数据——片段取数链成功后顺带 getCurrentProductionScope；404 not_found → 空态（不算错误态）", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    effectBlock[0].includes("getCurrentProductionScope"),
    "片段取数链成功后须顺带 F12 当前范围",
  );
  const fragCall = effectBlock[0].indexOf("fetchAllFragments");
  const scopeCall = effectBlock[0].indexOf("getCurrentProductionScope");
  assert.ok(
    fragCall !== -1 && scopeCall !== -1 && fragCall < scopeCall,
    "F12 须在片段取数之后顺带",
  );
  assert.ok(
    effectBlock[0].includes('scopeRes.status === 404 && scopeRes.code === "not_found"'),
    "404 not_found 须判空态（不算错误态）",
  );
  assert.ok(
    effectBlock[0].includes("setScoping({ open: false, all: true, selected: {}, submitting: false })"),
    "新快照须清除制作范围表单",
  );
});

test("FragmentActions: 无自动提交（useEffect 内不调 createProductionScope）+ 独立于候选创建（不调用 createFragmentCandidate）", () => {
  const effectBlock = actionsSrc.match(
    /useEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]+\]\)/,
  );
  assert.ok(effectBlock, "useEffect 缺失");
  assert.ok(
    !effectBlock[0].includes("createProductionScope"),
    "useEffect 内不得调用 createProductionScope（无自动提交）",
  );
  assert.ok(
    !actionsSrc.includes("createFragmentCandidate"),
    "制作范围区块独立于候选创建（不出现 createFragmentCandidate）",
  );
});

test("FragmentActions: 复选框数据源过滤 state===\"confirmed\"（候选/退役不出现）", () => {
  assert.ok(
    actionsSrc.includes('(f) => f.state === "confirmed"'),
    "复选框清单选项须仅为当前列表中已确认片段",
  );
  assert.ok(
    actionsSrc.includes("scopableFragments.map"),
    "复选框清单须渲染 scopableFragments",
  );
  assert.ok(
    !actionsSrc.includes('state === "retired"'),
    "组件不得出现 retired 直接比较（回归既有断言）",
  );
});

test("FragmentActions: 提交接线——每次点击 makeCommandId() 恰一次（crypto.randomUUID 恰 1 处；既有 = makeCommandId() 计数不变）", () => {
  const submitSrc =
    actionsSrc.match(/async function handleScopeSubmit[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(submitSrc.length > 0, "handleScopeSubmit 缺失");
  assert.ok(
    submitSrc.includes("createProductionScope"),
    "提交须调用 createProductionScope",
  );
  assert.ok(
    submitSrc.includes("makeCommandId()"),
    "提交须每次点击生成一次 commandId",
  );
  const randomCount = (actionsSrc.match(/crypto\.randomUUID\(\)/g) || []).length;
  assert.equal(
    randomCount,
    1,
    "crypto.randomUUID 须恰出现一次（经 makeCommandId 每次点击一次）",
  );
  const assignCount = (actionsSrc.match(/= makeCommandId\(\)/g) || []).length;
  assert.equal(
    assignCount,
    10,
    "既有 10 处 = makeCommandId() 不得变（确认/保存/预览影响/确认退役/预览/确认调整/预览拆分/确认拆分/合并预览/确认合并）",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => void handleScopeSubmit()}"),
    "「提交」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleScopeOpen()}"),
    "「提交制作范围」按钮 onClick 接线缺失",
  );
  assert.ok(
    actionsSrc.includes("onClick={() => handleScopeCancel()}"),
    "「取消」按钮 onClick 接线缺失",
  );
});

test("FragmentActions: 「全部」开关开启时省略 fragment_ids；本地校验零勾选先于请求（不发请求、保留表单）", () => {
  const submitSrc =
    actionsSrc.match(/async function handleScopeSubmit[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    submitSrc.includes("fragmentIds: scoping.all ? undefined : selectedIds"),
    "all 开启时 fragmentIds 须为 undefined（body 省略 fragment_ids）",
  );
  const localCheck = submitSrc.indexOf("请至少选择一个已确认片段。");
  const call = submitSrc.indexOf("createProductionScope");
  assert.ok(
    localCheck !== -1 && call !== -1 && localCheck < call,
    "零勾选本地校验须先于 createProductionScope（不发请求）",
  );
  assert.ok(submitSrc.includes("return;"), "零勾选须提前返回（保留表单）");
  assert.ok(
    submitSrc.includes("selectedIds.length === 0"),
    "零勾选判定缺失",
  );
});

test("FragmentActions: 提交中 disabled（防双击）；成功清除表单 + 内部重取（片段 + 当前范围）", () => {
  assert.ok(
    actionsSrc.includes("disabled={scoping.submitting}"),
    "提交中「提交」按钮须 disabled",
  );
  const submitSrc =
    actionsSrc.match(/async function handleScopeSubmit[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    submitSrc.includes("setRefreshTick"),
    "成功须内部重取（refreshTick，片段 + 当前范围随取数链重取）",
  );
});

test("FragmentActions: 失败保留表单与勾选（setScoping 仅附 error，不重置 selected/open），不退出", () => {
  const submitSrc =
    actionsSrc.match(/async function handleScopeSubmit[^{]*\{([\s\S]*?)\n  \}/)?.[1] ??
    "";
  assert.ok(
    submitSrc.includes("setScoping((prev) => ({ ...prev, error: res }))"),
    "失败须保留表单与勾选（仅附行内错误）",
  );
  const failIdx = submitSrc.indexOf("error: res");
  const resetIdx = submitSrc.indexOf(
    "setScoping({ open: false, all: true, selected: {}, submitting: false })",
  );
  assert.ok(resetIdx !== -1, "成功路径须清除表单");
  assert.ok(
    resetIdx < failIdx,
    "成功清除表单须只在 res.ok 分支（先于失败分支出现）",
  );
});

test("FragmentActions: 「取消」零 API 调用（handleScopeCancel 仅 setScoping）", () => {
  const cancelSrc =
    actionsSrc.match(/function handleScopeCancel[^{]*\{([\s\S]*?)\n  \}/)?.[1] ?? "";
  assert.ok(cancelSrc.length > 0, "handleScopeCancel 缺失");
  assert.ok(cancelSrc.includes("setScoping"), "取消须回到按钮态");
  assert.ok(
    !cancelSrc.includes("createProductionScope"),
    "取消不得调用 createProductionScope",
  );
  assert.ok(
    !cancelSrc.includes("clientObj"),
    "取消零 API 调用（不触 client）",
  );
});

test("FragmentActions: 行内错误区分支齐备（duplicate_scope 固定文案 / precondition_failed→message / validation_failed→violations / 其他→message）", () => {
  const textSrc =
    actionsSrc.match(/function scopeErrorText[^{]*\{([\s\S]*?)\n  \}/)?.[1] ?? "";
  assert.ok(textSrc.length > 0, "scopeErrorText 缺失");
  assert.ok(
    textSrc.includes('code === "duplicate_scope"'),
    "duplicate_scope 分支缺失",
  );
  assert.ok(
    textSrc.includes("相同片段覆盖的制作范围已提交过，历史只追加不重复。"),
    "duplicate_scope 固定文案缺失",
  );
  assert.ok(
    textSrc.includes('code === "precondition_failed"'),
    "precondition_failed 分支缺失",
  );
  assert.ok(
    textSrc.includes('code === "validation_failed"'),
    "validation_failed 分支缺失",
  );
  assert.ok(textSrc.includes("violations"), "violations 处理缺失");
});

test("FragmentActions: 当前范围展示——covered/范围外 [start,end) 与长度/清单映射片段名（找不到显示 id）", () => {
  assert.ok(actionsSrc.includes("scope.covered"), "covered 分支缺失");
  assert.ok(actionsSrc.includes("scope.excluded_ranges"), "范围外段落数据源缺失");
  assert.ok(
    actionsSrc.includes("{r.end - r.start}"),
    "范围外段落须显示长度 end - start",
  );
  assert.ok(
    actionsSrc.includes("scope.fragment_ids.map"),
    "覆盖片段清单须遍历 fragment_ids",
  );
  assert.ok(
    actionsSrc.includes("named !== undefined ? named.name : id"),
    "id 在当前列表找不到时须直接显示 id",
  );
  assert.ok(
    actionsSrc.includes("scope.fragment_ids.length"),
    "须显示片段数量",
  );
});

test("FragmentActions: 无历史列表接口（GET /production-scopes 列表不在本卡范围）+ 不直接 fetch + 无硬编码样例 + 不导航", () => {
  assert.ok(
    !apiSrc.includes("listProductionScopes"),
    "本卡不实现历史列表（listProductionScopes）",
  );
  assert.ok(
    !actionsSrc.includes("listProductionScopes"),
    "组件不得调用历史列表",
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
