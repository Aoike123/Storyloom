const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// ASSET-02-b 验收：资产上传 UI（features/studio/assets/components/AssetUpload.tsx）
//
// 模式与 selection.test.cjs / state.test.cjs 完全一致：
// - 纯逻辑部分（路径 / FormData / command_id / 结果状态机 / 真实 multipart
//   传输）用 vm + transpile 真跑（react 与 shared/api/client 以 stub 注入，
//   假 fetch 注入，绝不打真实网络）；
// - .tsx 组件本体做字符串断言（存在性、接口形状、失败保留原版本、不隐式
//   采纳、不自动跳转、可注入、禁止项）。
//
// 覆盖卡面全部验收点（含 MEDIA-01 图片扩展名边界：png → 后端 422
// unsupported_type，UI 原样显示真实错误且保留原版本）。
// ---------------------------------------------------------------------------

const root = path.join(__dirname, "..", "..");
const componentTsx = path.join(
  root,
  "features",
  "studio",
  "assets",
  "components",
  "AssetUpload.tsx",
);

function readComponent() {
  return fs.readFileSync(componentTsx, "utf8");
}

function transpile(file) {
  return ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
}

// ---------------------------------------------------------------------------
// vm 加载 AssetUpload.tsx：react / react-jsx-runtime / @/shared/api/client 以
// stub 注入；fetch 默认抛错（测试内逐条替换为假实现）
// ---------------------------------------------------------------------------

let uuidSeq = 0;
const clientStub = { token: null, getToken: () => clientStub.token };

const componentContext = {
  exports: {},
  require: (id) => {
    if (id === "react") {
      return {
        useReducer: (reducer, initialArg, init) => [
          init ? init(initialArg) : initialArg,
          () => {},
        ],
        useState: (initial) => [
          typeof initial === "function" ? initial() : initial,
          () => {},
        ],
      };
    }
    if (id === "react/jsx-runtime") {
      return { jsx: () => null, jsxs: () => null, Fragment: null };
    }
    if (id === "@/shared/api/client") {
      return { __esModule: true, default: clientStub };
    }
    throw new Error(`AssetUpload.tsx 出现意外的 import：${id}`);
  },
  FormData,
  Blob,
  File,
  crypto: { randomUUID: () => `uuid-${++uuidSeq}` },
  fetch: () => {
    throw new Error("tests must inject fetch");
  },
  process: { env: {} },
  setTimeout,
  clearTimeout,
};

vm.runInNewContext(transpile(componentTsx), componentContext);
const mod = componentContext.exports;

const {
  mediaUploadPath,
  buildUploadFormData,
  newUploadCommandId,
  resolveUploader,
  submitUpload,
  normalizeUploadError,
  formatUploadError,
  createUploadState,
  beginUpload,
  applyUploadResult,
  uploadReducer,
  uploadStatusText,
  defaultUpload,
} = mod;

for (const [name, value] of Object.entries({
  mediaUploadPath,
  buildUploadFormData,
  newUploadCommandId,
  resolveUploader,
  submitUpload,
  normalizeUploadError,
  formatUploadError,
  createUploadState,
  beginUpload,
  applyUploadResult,
  uploadReducer,
  uploadStatusText,
  defaultUpload,
})) {
  assert.equal(typeof value, "function", `AssetUpload.tsx 缺 ${name} 导出`);
}
assert.equal(typeof mod.default, "function", "AssetUpload.tsx 缺默认导出组件");

// ---------------------------------------------------------------------------
// 夹具与工具
// ---------------------------------------------------------------------------

function mp4(name = "clip.mp4") {
  return new File(["video-bytes"], name, { type: "video/mp4" });
}

function png(name = "pic.png") {
  return new File(["png-bytes"], name, { type: "image/png" });
}

function okDto(revision = 4, reviewStatus = "unreviewed") {
  const current = {
    revision,
    spec: {},
    media_artifact_id: `media-${revision}`,
    review_status: reviewStatus,
  };
  return {
    object_ref: { kind: "asset", id: "a1", revision },
    type: "character",
    name: "阿黎",
    character: null,
    current_revision: current,
    versions: [{ ...current, created_at: 1700000000 }],
    occurrences: [],
    bindings: [],
  };
}

const PNG_VIOLATION = {
  field: "filename",
  rule: "unsupported_type",
  message: "不支持的媒体文件扩展名：.png；允许：mp4, webm, mov, m4a, wav, mp3。",
};

function fail422() {
  return {
    ok: false,
    status: 422,
    code: "validation_failed",
    message: "请求校验失败。",
    details: { violations: [PNG_VIOLATION] },
  };
}

function fail409() {
  return {
    ok: false,
    status: 409,
    code: "revision_conflict",
    message: "对象版本已变化，请刷新后重试。",
    details: {
      object: { kind: "asset", id: "a1" },
      expected: 3,
      actual: 5,
    },
  };
}

function failNetwork() {
  return { ok: false, code: "network", message: "连接失败，请重试。" };
}

/** 组件函数体（默认导出起）：证明组件本体不直接 fetch、只经注入实现。 */
function componentBody(src) {
  const idx = src.indexOf("export default function AssetUpload");
  assert.ok(idx > 0, "未找到 AssetUpload 默认导出组件");
  return src.slice(idx);
}

// ===========================================================================
// 1. 存在性与骨架
// ===========================================================================

test("AssetUpload.tsx 存在、非空、use client 头、含 <style jsx>", () => {
  assert.ok(fs.existsSync(componentTsx), "AssetUpload.tsx 不存在");
  const src = readComponent();
  assert.ok(src.length > 0, "AssetUpload.tsx 为空");
  assert.ok(src.startsWith('"use client";'), '缺少 "use client" 头');
  assert.ok(src.includes("<style jsx>"), "缺 <style jsx> 局部样式（参照 StudioApp.tsx）");
  assert.ok(src.includes("</style>"), "style jsx 未闭合");
});

test("组件为 props 受控：projectId/assetId/expectedRevision/onUploaded/upload", () => {
  const src = readComponent();
  for (const key of [
    "projectId",
    "assetId",
    "expectedRevision",
    "onUploaded",
    "upload",
  ]) {
    assert.ok(
      new RegExp(`\\b${key}\\b`).test(src),
      `缺少 props.${key}（卡面冻结的注入面）`,
    );
  }
  assert.ok(
    src.includes("upload?: UploadFn"),
    "upload 必须是可注入的上传函数（upload?: UploadFn）",
  );
});

test("组件不自取数据（无副作用钩子、不 import 既有 source api）", () => {
  const src = readComponent();
  assert.ok(!src.includes("useEffect"), "组件不应有副作用钩子（不自动取数/不自动提交）");
  assert.ok(!src.includes("features/studio/source"), "组件不应依赖既有 source api");
  assert.ok(!/\bgetProject\(/.test(src), "组件不应自取项目数据");
  assert.ok(!/\blistFragments\(/.test(src), "组件不应自取片段数据");
});

// ===========================================================================
// 2. 接口形状：FormData / 字段名 / 路径
// ===========================================================================

test("接正式上传：源码用 FormData，字段名 file/command_id/expected_revision", () => {
  const src = readComponent();
  assert.ok(src.includes("new FormData()"), "必须用 FormData 提交 multipart");
  assert.ok(src.includes('form.append("file", file)'), "multipart 字段名必须为 file");
  assert.ok(
    src.includes('form.append("command_id", commandId)'),
    "multipart 字段名必须为 command_id",
  );
  assert.ok(
    src.includes('form.append("expected_revision", String(expectedRevision))'),
    "multipart 字段名必须为 expected_revision",
  );
});

test("字段只进 multipart：不 JSON 序列化、不设 JSON 内容类型、command_id 不作对象键", () => {
  const src = readComponent();
  assert.ok(!src.includes("JSON.stringify"), "multipart 不应被 JSON 序列化");
  assert.ok(!src.includes("application/json"), "multipart 不应携带 JSON 内容类型头");
  assert.ok(
    !/\bcommand_id\s*:/.test(src),
    "command_id 不得出现在 JSON 对象字面量里（只能在 FormData）",
  );
  assert.ok(!src.includes("Content-Type"), "不应手工设置内容类型头（boundary 由运行时生成）");
});

test("路径 = POST /api/studio/projects/{pid}/assets/{aid}/media（encodeURIComponent）", () => {
  const src = readComponent();
  assert.ok(src.includes("/api/studio/projects/"), "路径缺 /api/studio/projects/ 前缀");
  assert.ok(src.includes("/media"), "路径缺 /media 后缀");
  assert.ok(src.includes("assets"), "路径缺 assets 段");
  assert.ok(src.includes('method: "POST"'), "必须是 POST");

  assert.equal(
    mediaUploadPath("p 1", "a/2"),
    "/api/studio/projects/p%201/assets/a%2F2/media",
  );
  assert.equal(
    mediaUploadPath("proj-1", "asset-1"),
    "/api/studio/projects/proj-1/assets/asset-1/media",
  );
});

test("buildUploadFormData：真跑 FormData 字段（expected_revision 仅正整数时附加）", () => {
  const form = buildUploadFormData(mp4(), "cmd-1", 3);
  assert.ok(form instanceof FormData, "应返回 FormData");
  assert.equal(form.get("file").name, "clip.mp4");
  assert.equal(form.get("command_id"), "cmd-1");
  assert.equal(form.get("expected_revision"), "3");

  const noCas = buildUploadFormData(mp4(), "cmd-2", null);
  assert.equal(noCas.get("command_id"), "cmd-2");
  assert.equal(
    noCas.get("expected_revision"),
    null,
    "未提供 basline 时不得伪造 expected_revision",
  );
});

// ===========================================================================
// 3. 默认真实实现（假 fetch 注入，绝不打真实网络）
// ===========================================================================

test("defaultUpload：真发 multipart 到 /media，字段齐备、不设 JSON 内容类型、带 Bearer 令牌", async () => {
  const calls = [];
  componentContext.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 201, json: async () => okDto(4) };
  };
  clientStub.token = "tok-1";
  try {
    const outcome = await defaultUpload({
      projectId: "p 1",
      assetId: "a/2",
      file: mp4(),
      commandId: "cmd-1",
      expectedRevision: 3,
    });
    assert.equal(calls.length, 1, "应恰好发一次请求");
    assert.equal(calls[0].url, "/api/studio/projects/p%201/assets/a%2F2/media");
    assert.equal(calls[0].opts.method, "POST");
    assert.ok(calls[0].opts.body instanceof FormData, "body 必须是 FormData");
    assert.equal(calls[0].opts.body.get("file").name, "clip.mp4");
    assert.equal(calls[0].opts.body.get("command_id"), "cmd-1");
    assert.equal(calls[0].opts.body.get("expected_revision"), "3");
    assert.equal(
      calls[0].opts.headers["Content-Type"],
      undefined,
      "不得手工设置 Content-Type（multipart boundary 由运行时生成）",
    );
    assert.equal(calls[0].opts.headers["Authorization"], "Bearer tok-1");
    assert.equal(outcome.ok, true);
    assert.equal(outcome.data.current_revision.revision, 4);
  } finally {
    clientStub.token = null;
  }
});

test("defaultUpload：png → 后端 422 unsupported_type 原样透传（不伪造成功）", async () => {
  componentContext.fetch = async () => ({
    ok: false,
    status: 422,
    json: async () => ({ error: fail422() }),
  });
  const outcome = await defaultUpload({
    projectId: "p1",
    assetId: "a1",
    file: png(),
    commandId: "cmd-png",
    expectedRevision: null,
  });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.status, 422);
  assert.equal(outcome.code, "validation_failed");
  const text = formatUploadError(normalizeUploadError(outcome));
  assert.ok(text.includes("HTTP 422"), "应显示真实 HTTP 状态");
  assert.ok(text.includes("validation_failed"), "应显示真实错误码");
  assert.ok(text.includes("unsupported_type"), "应显示 violation 的 rule");
  assert.ok(text.includes(".png"), "应显示真实扩展名");
  assert.ok(text.includes("允许：mp4"), "应显示服务端允许列表（如实告知边界）");
});

test("defaultUpload：409 revision_conflict 与非 JSON 错误体 / 网络异常各自如实透传", async () => {
  // 409 CAS 冲突
  componentContext.fetch = async () => ({
    ok: false,
    status: 409,
    json: async () => ({ error: fail409() }),
  });
  const conflict = await defaultUpload({
    projectId: "p1",
    assetId: "a1",
    file: mp4(),
    commandId: "cmd-409",
    expectedRevision: 3,
  });
  assert.equal(conflict.ok, false);
  assert.equal(conflict.status, 409);
  assert.equal(conflict.code, "revision_conflict");

  // 非 JSON 错误体：按状态码兜底，不吞状态码
  componentContext.fetch = async () => ({
    ok: false,
    status: 404,
    json: async () => {
      throw new Error("not json");
    },
  });
  const notFound = await defaultUpload({
    projectId: "p1",
    assetId: "a1",
    file: mp4(),
    commandId: "cmd-404",
    expectedRevision: null,
  });
  assert.equal(notFound.ok, false);
  assert.equal(notFound.status, 404);
  assert.equal(notFound.code, "not_found");
  assert.ok(notFound.message.includes("404"));

  // fetch 拒绝 → network
  componentContext.fetch = async () => {
    throw new Error("offline");
  };
  const offline = await defaultUpload({
    projectId: "p1",
    assetId: "a1",
    file: mp4(),
    commandId: "cmd-net",
    expectedRevision: null,
  });
  assert.equal(offline.ok, false);
  assert.equal(offline.code, "network");

  // 复位：任何真实 fetch 都不允许
  componentContext.fetch = () => {
    throw new Error("tests must inject fetch");
  };
});

// ===========================================================================
// 4. 失败保留原版本（核心验收）
// ===========================================================================

test("失败保留原版本：422 失败只置错误，baseline/pending 不变、错误可读可重试", () => {
  const before = beginUpload(createUploadState(3));
  assert.equal(before.submitting, true);
  assert.equal(before.baselineRevision, 3);
  assert.equal(before.pendingRevision, null);

  const after = applyUploadResult(before, fail422());
  assert.equal(after.baselineRevision, 3, "失败不得改动原版本");
  assert.equal(after.pendingRevision, null, "失败不得伪造待审核新版本");
  assert.equal(after.submitting, false, "失败后必须解除提交中（可重试）");
  assert.notEqual(after.error, null, "失败必须可见错误");

  const text = formatUploadError(after.error);
  assert.ok(text.includes("HTTP 422"));
  assert.ok(text.includes("validation_failed"));
  assert.ok(text.includes("filename"), "应显示 violation 字段");
  assert.ok(text.includes("unsupported_type"), "应显示 violation rule");
  assert.ok(text.includes(".png"));
  assert.ok(text.includes("请求校验失败。"), "应显示错误体 message");
});

test("失败保留原版本：409 与网络错误同样不清空/不替换展示中的版本信息", () => {
  // 先有一次成功（展示待审核 revision 4），再来一次失败：4 必须仍在
  const success = applyUploadResult(
    beginUpload(createUploadState(3)),
    { ok: true, data: okDto(4) },
  );
  assert.equal(success.pendingRevision, 4);
  assert.equal(success.error, null);

  for (const failure of [fail409(), failNetwork()]) {
    const after = applyUploadResult(beginUpload(success), failure);
    assert.equal(after.baselineRevision, 3, "原版本必须保留");
    assert.equal(
      after.pendingRevision,
      4,
      "失败不得清空已展示的版本信息（pending 原样保留）",
    );
    assert.equal(after.submitting, false);
    assert.notEqual(after.error, null);
  }

  const conflictText = formatUploadError(
    normalizeUploadError(applyUploadResult(beginUpload(success), fail409()).error),
  );
  assert.ok(conflictText.includes("HTTP 409"));
  assert.ok(conflictText.includes("revision_conflict"));
  const networkText = formatUploadError(
    normalizeUploadError(applyUploadResult(beginUpload(success), failNetwork()).error),
  );
  assert.ok(networkText.includes("network"));
});

test("允许重试：同一失败可再次提交（begin 清旧错误但保留原版本）", () => {
  const failed = applyUploadResult(beginUpload(createUploadState(3)), fail422());
  const retrying = beginUpload(failed);
  assert.equal(retrying.submitting, true);
  assert.equal(retrying.error, null, "重试开始时应清掉旧错误提示");
  assert.equal(retrying.baselineRevision, 3);
  // 重试仍失败 → 再次可见真实错误
  const failedAgain = applyUploadResult(retrying, fail422());
  assert.equal(failedAgain.baselineRevision, 3);
  assert.notEqual(failedAgain.error, null);
});

test("成功体形状异常：不臆造新版本号、不清空已展示的版本信息", () => {
  const shown = applyUploadResult(beginUpload(createUploadState(3)), {
    ok: true,
    data: okDto(4),
  });
  assert.equal(shown.pendingRevision, 4);

  const malformed = applyUploadResult(beginUpload(shown), {
    ok: true,
    data: { object_ref: { kind: "asset", id: "a1", revision: 5 } },
  });
  assert.equal(malformed.submitting, false);
  assert.equal(malformed.error, null);
  assert.equal(malformed.baselineRevision, 3);
  assert.equal(malformed.pendingRevision, 4, "形状异常不得清空已展示版本");
  assert.equal(malformed.pendingReviewStatus, "unreviewed");
});

test("状态机：reset 清旧错误/待审核但保留原版本；setBaseline 换基线才重挂", () => {
  const failed = applyUploadResult(beginUpload(createUploadState(3)), fail422());
  const reset = uploadReducer(failed, { type: "reset" });
  assert.equal(reset.error, null);
  assert.equal(reset.pendingRevision, null);
  assert.equal(reset.baselineRevision, 3, "换文件清提示不得动原版本");

  const rebased = uploadReducer(failed, {
    type: "setBaseline",
    baselineRevision: 5,
  });
  assert.equal(rebased.baselineRevision, 5);
  assert.equal(rebased.error, null);
  assert.equal(
    uploadReducer(failed, { type: "setBaseline", baselineRevision: 3 }),
    failed,
    "基线未变时必须返回原对象（不做无谓重挂）",
  );
});

// ===========================================================================
// 5. 不隐式采纳 / 不自动跳转 / 不自动切视图
// ===========================================================================

test("不隐式采纳：组件无 adopt/confirm 语义与调用，文案无'已采用/已确认'", () => {
  const src = readComponent();
  assert.ok(!/adopt/i.test(src), "不得出现 adopt 相关调用");
  assert.ok(!/confirm/i.test(src), "不得出现 confirm 相关调用");
  assert.ok(!src.includes("/adopt"), "不得引用采用端点");
  assert.ok(!src.includes("已采用"), "文案不得出现'已采用'");
  assert.ok(!src.includes("已确认"), "文案不得出现'已确认'");

  // 纯逻辑产出的状态文案同样无采纳语义
  const texts = [
    uploadStatusText(beginUpload(createUploadState(3))),
    uploadStatusText(
      applyUploadResult(beginUpload(createUploadState(3)), {
        ok: true,
        data: okDto(4),
      }),
    ),
    uploadStatusText(applyUploadResult(beginUpload(createUploadState(3)), fail422())),
  ];
  for (const text of texts) {
    assert.ok(!text.includes("已采用"));
    assert.ok(!text.includes("已确认"));
  }
  assert.ok(texts[1].includes("待审核"), "成功文案只能表达'待审核'");
});

test("不自动跳转/不自动切视图：无 router.push、window.location、无副作用钩子", () => {
  const src = readComponent();
  assert.ok(!src.includes("router.push"), "不得出现 router.push");
  assert.ok(!src.includes("window.location"), "不得出现 window.location");
  assert.ok(!src.includes("history.pushState"), "不得出现 history.pushState");
  assert.ok(!src.includes("useEffect"), "不得有自动提交/自动取数的副作用钩子");
  // 提交只由显式点击驱动
  assert.ok(src.includes("onClick={() => void handleUpload()}"), "上传必须由点击驱动");
});

// ===========================================================================
// 6. 可注入 upload（假实现 422 vs 200）
// ===========================================================================

test("可注入 upload：组件本体不直接 fetch，只经 resolveUploader(upload)", () => {
  const src = readComponent();
  assert.ok(src.includes("resolveUploader"), "缺 resolveUploader（注入开关）");
  assert.ok(
    componentBody(src).includes("resolveUploader(upload)"),
    "组件必须用注入的 upload（而非无条件 fetch）",
  );
  assert.ok(
    !componentBody(src).includes("fetch("),
    "组件本体不得直接 fetch：真实传输只能在 defaultUpload 内",
  );
});

test("可注入 upload：假实现 422 与 200 → UI 结果不同，422 路径保留原版本", async () => {
  const seen = [];
  const fake = async (request) => {
    seen.push(request);
    if (request.file.name === "pic.png") return fail422();
    return { ok: true, data: okDto(4) };
  };

  assert.equal(resolveUploader(fake), fake, "注入实现必须原样生效");
  assert.equal(
    resolveUploader(undefined),
    defaultUpload,
    "未注入时回落到真实 multipart 实现",
  );

  // 422 路径（图片扩展名边界）
  const failAttempt = await submitUpload(fake, {
    projectId: "p1",
    assetId: "a1",
    file: png(),
    expectedRevision: 3,
  });
  const failState = applyUploadResult(
    beginUpload(createUploadState(3)),
    failAttempt.outcome,
  );
  assert.equal(failState.pendingRevision, null);
  assert.equal(failState.baselineRevision, 3, "422 路径必须保留原版本");
  assert.notEqual(failState.error, null);
  const failText = uploadStatusText(failState);

  // 200 路径（合法媒体）
  const okAttempt = await submitUpload(fake, {
    projectId: "p1",
    assetId: "a1",
    file: mp4(),
    expectedRevision: 3,
  });
  const okState = applyUploadResult(
    beginUpload(createUploadState(3)),
    okAttempt.outcome,
  );
  assert.equal(okState.pendingRevision, 4);
  assert.equal(okState.error, null);
  assert.equal(okState.baselineRevision, 3, "成功也必须保留原版本信息");
  const okText = uploadStatusText(okState);

  assert.notEqual(failText, okText, "两条路径的 UI 结果必须不同");
  assert.ok(failText.includes("unsupported_type"), "422 路径显示真实 violation");
  assert.ok(!failText.includes("待审核"), "422 路径不得进入待审核态");
  assert.ok(okText.includes("待审核"), "200 路径只表达待审核");

  // 假实现真实收到的请求字段
  assert.equal(seen.length, 2);
  assert.equal(seen[0].file.name, "pic.png");
  assert.equal(seen[0].expectedRevision, 3);
  assert.equal(seen[1].file.name, "clip.mp4");
  assert.equal(typeof seen[0].commandId, "string");
  assert.ok(seen[0].commandId.length > 0);
});

// ===========================================================================
// 7. command_id
// ===========================================================================

test("command_id：每次提交现生成（crypto.randomUUID）且两次不同", async () => {
  const src = readComponent();
  assert.ok(src.includes("crypto.randomUUID"), "command_id 必须用 crypto.randomUUID");
  assert.ok(src.includes("newUploadCommandId"), "缺 command_id 生成函数");

  const first = newUploadCommandId();
  const second = newUploadCommandId();
  assert.equal(typeof first, "string");
  assert.ok(first.length > 0);
  assert.notEqual(first, second, "每次尝试必须生成新的 command_id");

  // 每次 submitUpload = 一次新尝试 = 一个全新 command_id（含同文件重试）
  const ids = [];
  const fake = async (request) => {
    ids.push(request.commandId);
    return { ok: true, data: okDto(4) };
  };
  const input = {
    projectId: "p1",
    assetId: "a1",
    file: mp4(),
    expectedRevision: null,
  };
  await submitUpload(fake, input);
  await submitUpload(fake, input);
  assert.equal(ids.length, 2);
  assert.notEqual(ids[0], ids[1], "重试同一文件也必须重新生成 command_id");

  // 注入 makeCommandId（测试可确定性复现）
  let n = 0;
  const withSeq = await submitUpload(
    fake,
    input,
    () => `cmd-${++n}`,
  );
  assert.equal(withSeq.commandId, "cmd-1");
});

// ===========================================================================
// 8. 禁止项 / 图片边界
// ===========================================================================

test("禁止项：无 /api/author、不 import app/、不引用旧产品与旧阅读视图", () => {
  const src = readComponent();
  assert.ok(!src.includes("/api/author"), "不得出现 /api/author");
  assert.ok(!/\bfrom\s+["']app\//.test(src), '不得 from "app/');
  assert.ok(!/ProjectHome/.test(src), "不得引用 ProjectHome");
  assert.ok(!/TaskActivity/.test(src), "不得引用 TaskActivity");
  assert.ok(!/reader/i.test(src), "不得引用旧 reader");
});

test("图片扩展名边界：不做前端白名单前置拦截（服务端为唯一权威）", () => {
  const src = readComponent();
  assert.ok(!src.includes("accept="), "不得用 accept 把图片挡在选择器外（服务端为权威）");
  assert.ok(
    src.includes("由服务端校验"),
    "必须如实说明媒体类型由服务端校验",
  );
  assert.ok(
    src.includes("其它文件会被服务端拒绝"),
    "必须如实说明其它文件由服务端拒绝并显示真实错误",
  );
  // 组件内部不得出现扩展名白名单数组（避免前端抢先判定/放宽契约）
  assert.ok(
    !/\[\s*["']mp4["']/.test(src),
    "不得内置前端扩展名白名单数组（不抢先拦截、不放宽 MEDIA-01）",
  );
});
