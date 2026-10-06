const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// SOURCE-12 验收：api.ts（vm+transpile 纯逻辑）+ 两个组件（字符串断言）
// 模式与 api-client.test.cjs / studio-app.test.cjs 一致。
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
const listTsx = path.join(
  root,
  "features",
  "studio",
  "source",
  "components",
  "FragmentList.tsx",
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
const {
  getProject,
  getSourceRevision,
  listFragments,
  fetchAllFragments,
  computeSegments,
} = apiContext.exports;

// ---------------------------------------------------------------------------
// vm 加载 client.ts（真客户端 + 每测试注入 mock fetch，复刻 api-client.test.cjs）
// ---------------------------------------------------------------------------
const clientContext = {
  window: { localStorage: null }, // 各客户端经 ClientOptions.storage 注入独立 store
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

const PROJECT = {
  id: "p1",
  name: "项目",
  description: "",
  visibility: "private",
  active_source_revision_id: "rev1",
  created_at: 1,
  updated_at: 2,
};

const REVISION = {
  id: "rev1",
  project_id: "p1",
  previous_revision_id: null,
  raw_content: "raw\r\n",
  raw_hash: "rh",
  canonical_content: "raw\n",
  canonical_hash: "ch",
  offset_policy: "lf-utf16-v1",
  char_length: 4,
  created_at: 3,
  is_active: true,
};

const FRAG_LIST = { items: [], next_cursor: null };

// ---------------------------------------------------------------------------
// getProject / getSourceRevision：URL / 方法 / 请求头
// ---------------------------------------------------------------------------

test("getProject: URL、GET、Bearer 头、无 Content-Type（无 body）", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(PROJECT);
  });
  client.setToken("tok-1");
  const res = await getProject(client, "p1");
  assert.equal(captured.url, "http://test.local/api/studio/projects/p1");
  assert.equal(captured.opts.method, "GET");
  assert.equal(captured.opts.headers["Authorization"], "Bearer tok-1");
  assert.equal(captured.opts.headers["Content-Type"], undefined);
  assert.equal(res.ok, true);
  assert.deepEqual(res.data, PROJECT);
});

test("getProject: pid 经 encodeURIComponent 编码", async () => {
  let captured;
  const client = makeClient(async (url) => {
    captured = url;
    return okJson(PROJECT);
  });
  await getProject(client, "a b/c");
  assert.equal(captured, "http://test.local/api/studio/projects/a%20b%2Fc");
});

test("getProject: 无 token 时不带 Authorization 头", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = opts;
    return okJson(PROJECT);
  });
  await getProject(client, "p1");
  assert.equal(captured.headers["Authorization"], undefined);
});

test("getSourceRevision: URL 与 GET 方法", async () => {
  let captured;
  const client = makeClient(async (url, opts) => {
    captured = { url, opts };
    return okJson(REVISION);
  });
  const res = await getSourceRevision(client, "p1", "rev1");
  assert.equal(
    captured.url,
    "http://test.local/api/studio/projects/p1/sources/rev1",
  );
  assert.equal(captured.opts.method, "GET");
  assert.equal(res.ok, true);
  assert.equal(res.data.canonical_content, "raw\n");
  assert.equal(res.data.is_active, true);
});

test("getSourceRevision: pid 与 revisionId 均编码", async () => {
  let captured;
  const client = makeClient(async (url) => {
    captured = url;
    return okJson(REVISION);
  });
  await getSourceRevision(client, "p/p", "r r");
  assert.equal(
    captured,
    "http://test.local/api/studio/projects/p%2Fp/sources/r%20r",
  );
});

// ---------------------------------------------------------------------------
// listFragments：query 编码（state/limit/cursor 仅在有值时附加）
// ---------------------------------------------------------------------------

function captureList() {
  const box = {};
  const client = makeClient(async (url, opts) => {
    box.url = url;
    box.opts = opts;
    return okJson(FRAG_LIST);
  });
  return { box, client };
}

test("listFragments: 无 opts / 空 opts → 无 query 串", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1");
  assert.equal(box.url, "http://test.local/api/studio/projects/p1/fragments");
  await listFragments(client, "p1", {});
  assert.equal(box.url, "http://test.local/api/studio/projects/p1/fragments");
  assert.equal(box.opts.method, "GET");
});

test("listFragments: 仅 state", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1", { state: "candidate" });
  assert.equal(
    box.url,
    "http://test.local/api/studio/projects/p1/fragments?state=candidate",
  );
});

test("listFragments: 仅 limit", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1", { limit: 10 });
  assert.equal(
    box.url,
    "http://test.local/api/studio/projects/p1/fragments?limit=10",
  );
});

test("listFragments: 仅 cursor", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1", { cursor: "01arz3ndektsv4rrfq69oag6cz" });
  assert.equal(
    box.url,
    "http://test.local/api/studio/projects/p1/fragments?cursor=01arz3ndektsv4rrfq69oag6cz",
  );
});

test("listFragments: state+limit+cursor 组合", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1", {
    state: "confirmed",
    limit: 25,
    cursor: "abc123",
  });
  assert.equal(
    box.url,
    "http://test.local/api/studio/projects/p1/fragments?state=confirmed&limit=25&cursor=abc123",
  );
});

test("listFragments: 特殊字符经 encodeURIComponent 编码", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1", { state: "x y&z", cursor: "c+d=e" });
  assert.equal(
    box.url,
    "http://test.local/api/studio/projects/p1/fragments?state=x%20y%26z&cursor=c%2Bd%3De",
  );
});

test("listFragments: 空串值视为无值不附加", async () => {
  const { box, client } = captureList();
  await listFragments(client, "p1", { state: "", cursor: "" });
  assert.equal(box.url, "http://test.local/api/studio/projects/p1/fragments");
});

// ---------------------------------------------------------------------------
// 错误透传：404 / 409 / 422 / network — code/details/status/message 不丢
// ---------------------------------------------------------------------------

test("404 not_found：code/status/message/details 原样透传", async () => {
  const body = {
    error: {
      code: "not_found",
      message: "项目不存在。",
      details: { kind: "project", id: "p1" },
    },
  };
  const client = makeClient(async () => errJson(404, body));
  const res = await getProject(client, "p1");
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.equal(res.message, "项目不存在。");
  assert.deepEqual(norm(res.details), { kind: "project", id: "p1" });
});

test("409 revision_conflict：details.object/expected/actual 不丢", async () => {
  const body = {
    error: {
      code: "revision_conflict",
      message: "范围集合版本已变化，请刷新片段列表后重试。",
      details: {
        object: { kind: "range_set", id: "s1" },
        expected: 1,
        actual: 2,
      },
    },
  };
  const client = makeClient(async () => errJson(409, body));
  const res = await listFragments(client, "p1");
  assert.equal(res.ok, false);
  assert.equal(res.status, 409);
  assert.equal(res.code, "revision_conflict");
  assert.deepEqual(norm(res.details), {
    object: { kind: "range_set", id: "s1" },
    expected: 1,
    actual: 2,
  });
});

test("422 validation_failed：details.violations 数组原样保留", async () => {
  const body = {
    error: {
      code: "validation_failed",
      message: "参数校验失败。",
      details: {
        violations: [
          {
            field: "state",
            rule: "invalid",
            message: "state 必须是 candidate/confirmed/retired/pending_review 之一。",
          },
        ],
      },
    },
  };
  const client = makeClient(async () => errJson(422, body));
  const res = await listFragments(client, "p1", { state: "bogus" });
  assert.equal(res.ok, false);
  assert.equal(res.status, 422);
  assert.equal(res.code, "validation_failed");
  assert.equal(res.details.violations.length, 1);
  assert.equal(res.details.violations[0].field, "state");
  assert.equal(res.details.violations[0].rule, "invalid");
});

test("network：fetch 抛错 → code:network、无 status、不重抛", async () => {
  const client = makeClient(async () => {
    throw new TypeError("Failed to fetch");
  });
  const res = await getSourceRevision(client, "p1", "rev1");
  assert.equal(res.ok, false);
  assert.equal(res.code, "network");
  assert.equal(res.status, undefined);
  assert.equal(res.message, "连接失败，请重试。");
});

// ---------------------------------------------------------------------------
// computeSegments
// ---------------------------------------------------------------------------

function makeFragment(id, start, end, state = "candidate") {
  return {
    object_ref: { kind: "fragment", id, revision: 1 },
    name: id,
    summary: null,
    state,
    revision_is_active: true,
    range: { start, end },
    source_revision_id: "rev1",
    predecessor_ids: [],
    created_at: 1,
    updated_at: 1,
    retired_at: null,
  };
}

function assertTiling(canonical, segments) {
  if (segments.length === 0) return;
  assert.equal(segments[0].start, 0, "首段必须从 0 开始");
  for (let i = 1; i < segments.length; i++) {
    assert.equal(
      segments[i].start,
      segments[i - 1].end,
      `段 ${i} 必须与前一段相接（无空段/无重叠）`,
    );
  }
  assert.equal(
    segments[segments.length - 1].end,
    canonical.length,
    "末段必须覆盖到文本结尾",
  );
}

test("computeSegments: 间隙切出普通段，完整覆盖全文", () => {
  const canonical = "abcdefghij";
  const segments = computeSegments(canonical, [makeFragment("f1", 2, 5)]);
  assert.deepEqual(norm(segments), [
    { start: 0, end: 2, fragmentId: null, state: null },
    { start: 2, end: 5, fragmentId: "f1", state: "candidate" },
    { start: 5, end: 10, fragmentId: null, state: null },
  ]);
  assertTiling(canonical, segments);
});

test("computeSegments: 相接边界不产生空段", () => {
  const canonical = "abcd";
  const segments = computeSegments(canonical, [
    makeFragment("f1", 0, 2),
    makeFragment("f2", 2, 4, "confirmed"),
  ]);
  assert.equal(segments.length, 2);
  assert.deepEqual(norm(segments[0]), {
    start: 0,
    end: 2,
    fragmentId: "f1",
    state: "candidate",
  });
  assert.deepEqual(norm(segments[1]), {
    start: 2,
    end: 4,
    fragmentId: "f2",
    state: "confirmed",
  });
});

test("computeSegments: 乱序输入按 start 升序计算", () => {
  const canonical = "abcdefghij";
  const segments = computeSegments(canonical, [
    makeFragment("f2", 5, 8),
    makeFragment("f1", 0, 2),
  ]);
  assert.deepEqual(
    norm(segments.map((s) => [s.start, s.end, s.fragmentId])),
    [
      [0, 2, "f1"],
      [2, 5, null],
      [5, 8, "f2"],
      [8, 10, null],
    ],
  );
  assertTiling(canonical, segments);
});

test("computeSegments: 空文本返回 []", () => {
  assert.deepEqual(norm(computeSegments("", [makeFragment("f1", 0, 0)])), []);
  assert.deepEqual(norm(computeSegments("", [])), []);
});

test("computeSegments: 退役片段不成高亮段", () => {
  const canonical = "abcd";
  const segments = computeSegments(canonical, [
    makeFragment("f1", 0, 2, "retired"),
  ]);
  assert.deepEqual(norm(segments), [
    { start: 0, end: 4, fragmentId: null, state: null },
  ]);
});

test("computeSegments: 高亮段携带真实 fragmentId 与有效 state", () => {
  const canonical = "abcd";
  const segments = computeSegments(canonical, [
    makeFragment("frag-9", 1, 3, "confirmed"),
  ]);
  assert.equal(segments[1].fragmentId, "frag-9");
  assert.equal(segments[1].state, "confirmed");
  assert.equal(canonical.slice(segments[1].start, segments[1].end), "bc");
});

test("computeSegments: 全覆盖时只有高亮段", () => {
  const canonical = "abcd";
  const segments = computeSegments(canonical, [makeFragment("f1", 0, 4)]);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].fragmentId, "f1");
});

test("computeSegments: 防御——空范围跳过、越界夹取后仍连续覆盖", () => {
  const canonical = "abcdefghij";
  const segments = computeSegments(canonical, [
    makeFragment("f-empty", 5, 5),
    makeFragment("f-wide", 8, 99),
  ]);
  assertTiling(canonical, segments);
  assert.deepEqual(
    norm(segments.map((s) => [s.start, s.end, s.fragmentId])),
    [
      [0, 8, null],
      [8, 10, "f-wide"],
    ],
  );
});

test("computeSegments: emoji（代理对）边界切片不劈对", () => {
  // 前(0) 言(1) 😀(2,3 代理对) 后(4) 续(5)，共 6 个 UTF-16 code unit
  const canonical = "前言😀后续";
  assert.equal(canonical.length, 6);
  const segments = computeSegments(canonical, [makeFragment("f1", 2, 4)]);
  assert.deepEqual(
    norm(segments.map((s) => [s.start, s.end, s.fragmentId])),
    [
      [0, 2, null],
      [2, 4, "f1"],
      [4, 6, null],
    ],
  );
  const lone = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  const slices = segments.map((s) => canonical.slice(s.start, s.end));
  assert.deepEqual(norm(slices), ["前言", "😀", "后续"]);
  for (const text of slices) {
    assert.ok(!lone.test(text), `切片含落单代理半字: ${JSON.stringify(text)}`);
  }
  // 两个 emoji 相邻高亮：边界 2..6 覆盖 😀😀（各 2 unit）
  const canonical2 = "a😀😀b";
  assert.equal(canonical2.length, 6);
  const seg2 = computeSegments(canonical2, [makeFragment("f2", 1, 5)]);
  assert.equal(canonical2.slice(seg2[1].start, seg2[1].end), "😀😀");
  for (const s of seg2) {
    assert.ok(!lone.test(canonical2.slice(s.start, s.end)));
  }
});

// ---------------------------------------------------------------------------
// fetchAllFragments：跟随 next_cursor 全量聚合（SOURCE-12-fix-1）
// 组件翻页逻辑在组件内无法 vm 触达 → 聚合纯函数提取到 api.ts，两组件共用。
// ---------------------------------------------------------------------------

test("fetchAllFragments: 两页聚合 + 第二页请求透传第一页 next_cursor", async () => {
  const f1 = makeFragment("f1", 0, 2);
  const f2 = makeFragment("f2", 2, 4, "confirmed");
  const pages = [
    { items: [f1], next_cursor: "cursor-2" }, // 第一页 next_cursor 非空
    { items: [f2], next_cursor: null },
  ];
  const urls = [];
  const client = makeClient(async (url) => {
    urls.push(url);
    return okJson(pages[urls.length - 1]);
  });
  const res = await fetchAllFragments(client, "p1");
  assert.equal(res.ok, true);
  assert.equal(urls.length, 2, "两页都必须请求");
  assert.equal(
    urls[0],
    "http://test.local/api/studio/projects/p1/fragments?limit=200",
  );
  assert.equal(
    urls[1],
    "http://test.local/api/studio/projects/p1/fragments?limit=200&cursor=cursor-2",
    "第二页必须携带第一页返回的 next_cursor",
  );
  assert.deepEqual(norm(res.data), norm([f1, f2]), "items 按页序拼接");
});

test("fetchAllFragments: 单页 next_cursor=null → 只请求一次", async () => {
  let calls = 0;
  const client = makeClient(async () => {
    calls += 1;
    return okJson({ items: [makeFragment("f1", 0, 2)], next_cursor: null });
  });
  const res = await fetchAllFragments(client, "p1");
  assert.equal(calls, 1);
  assert.equal(res.ok, true);
  assert.equal(res.data.length, 1);
});

test("fetchAllFragments: 第二页 404 → 错误整体透传，不静默返回部分数据", async () => {
  const body = {
    error: {
      code: "not_found",
      message: "项目不存在。",
      details: { kind: "project", id: "p1" },
    },
  };
  let calls = 0;
  const client = makeClient(async () => {
    calls += 1;
    return calls === 1
      ? okJson({ items: [makeFragment("f1", 0, 2)], next_cursor: "cursor-2" })
      : errJson(404, body);
  });
  const res = await fetchAllFragments(client, "p1");
  assert.equal(calls, 2);
  assert.equal(res.ok, false);
  assert.equal(res.status, 404);
  assert.equal(res.code, "not_found");
  assert.equal(res.message, "项目不存在。");
  assert.deepEqual(norm(res.details), { kind: "project", id: "p1" });
});

test("fetchAllFragments: 页数上限防御触发 → 返回错误而非死循环", async () => {
  let calls = 0;
  const client = makeClient(async () => {
    calls += 1;
    // 每页都返回前进的游标，永不终止
    return okJson({
      items: [makeFragment(`f${calls}`, 0, 1)],
      next_cursor: `cursor-${calls + 1}`,
    });
  });
  const res = await fetchAllFragments(client, "p1", { maxPages: 3 });
  assert.equal(calls, 3, "请求数恰为 maxPages，不得死循环");
  assert.equal(res.ok, false);
  assert.equal(res.code, "internal_error");
  assert.equal(res.details.reason, "max_pages_exceeded");
});

test("fetchAllFragments: next_cursor 不前进 → 即停返回错误（不耗尽页数上限）", async () => {
  let calls = 0;
  const client = makeClient(async () => {
    calls += 1;
    return okJson({ items: [], next_cursor: "same-cursor" });
  });
  const res = await fetchAllFragments(client, "p1", { maxPages: 50 });
  assert.equal(calls, 2, "第二页仍返回相同游标 → 立即停住");
  assert.equal(res.ok, false);
  assert.equal(res.code, "internal_error");
  assert.equal(res.details.reason, "cursor_not_advancing");
});

// ---------------------------------------------------------------------------
// SourceReader.tsx / FragmentList.tsx 字符串断言（studio-app.test.cjs 模式）
// ---------------------------------------------------------------------------

const readerSrc = fs.readFileSync(readerTsx, "utf8");
const listSrc = fs.readFileSync(listTsx, "utf8");
const apiSrc = fs.readFileSync(apiTs, "utf8");

test("api.ts 是纯逻辑模块：不 import react、无 JSX 标签", () => {
  assert.ok(!/import\s+[^\n]*from\s+["']react["']/.test(apiSrc));
  assert.ok(!apiSrc.includes("<span"));
  assert.ok(!apiSrc.includes("<mark"));
  assert.ok(!apiSrc.includes("<div"));
});

test("SourceReader: use client + 三态文案分开（加载/错误/空态）", () => {
  assert.ok(readerSrc.startsWith('"use client"'));
  assert.ok(readerSrc.includes("正在加载正文"), "缺加载态文案");
  assert.ok(readerSrc.includes("正文读取失败"), "缺错误态文案");
  assert.ok(readerSrc.includes("{state.message}"), "错误态必须渲染真实 message");
  assert.ok(readerSrc.includes("重试"), "错误态缺重试按钮");
  assert.ok(readerSrc.includes("尚未导入正文"), "缺空态诚实文案");
  // 错误态 ≠ 空态：两套文案各自独立存在
  assert.notEqual(
    readerSrc.indexOf("正文读取失败"),
    readerSrc.indexOf("尚未导入正文"),
  );
});

test("SourceReader: 正文连续排布 pre-wrap + mark 高亮 + 锚点 id 模式", () => {
  assert.ok(readerSrc.includes("pre-wrap"), "正文须 white-space: pre-wrap");
  assert.ok(readerSrc.includes("<mark"), "高亮段须为 <mark>");
  assert.ok(readerSrc.includes("data-fragment-id"), "高亮段须带 data-fragment-id");
  assert.ok(
    readerSrc.includes("studio-fragment-"),
    "锚点 id 须为 studio-fragment-${id} 模式",
  );
  assert.ok(readerSrc.includes("computeSegments"), "须用 computeSegments 切段");
  assert.ok(readerSrc.includes("onFragmentClick"), "须有点击回调");
  assert.ok(readerSrc.includes("refreshKey"), "须支持 refreshKey 重取");
});

test("SourceReader: 无硬编码样例正文、不直接 fetch", () => {
  assert.ok(!readerSrc.includes("Lorem"));
  assert.ok(!readerSrc.includes("lorem"));
  assert.ok(!readerSrc.includes("示例正文"));
  assert.ok(!readerSrc.includes("样例正文"));
  assert.ok(!readerSrc.includes("fetch("), "组件不直接 fetch，须走 client");
});

test("FragmentList: use client + 三态文案分开（加载/错误/空态）", () => {
  assert.ok(listSrc.startsWith('"use client"'));
  assert.ok(listSrc.includes("正在加载片段"), "缺加载态文案");
  assert.ok(listSrc.includes("片段列表读取失败"), "缺错误态文案");
  assert.ok(listSrc.includes("{state.message}"), "错误态必须渲染真实 message");
  assert.ok(listSrc.includes("重试"), "错误态缺重试按钮");
  assert.ok(listSrc.includes("还没有片段"), "缺空态诚实文案");
  assert.ok(
    listSrc.includes("state.items.length === 0"),
    "空态须与错误态分支分开",
  );
});

test("FragmentList: 状态中文标签四值齐全", () => {
  assert.ok(listSrc.includes("候选"), "candidate=候选");
  assert.ok(listSrc.includes("已确认"), "confirmed=已确认");
  assert.ok(listSrc.includes("待复核"), "pending_review=待复核");
  assert.ok(listSrc.includes("已退役"), "retired=已退役");
});

test("FragmentList: 点击定位 scrollIntoView + 闪烁 class + 退役禁用", () => {
  assert.ok(listSrc.includes("scrollIntoView"), "缺省定位须 scrollIntoView");
  assert.ok(listSrc.includes("smooth"), "smooth 滚动");
  assert.ok(listSrc.includes("center"), "block center");
  assert.ok(listSrc.includes("studio-fragment-"), "定位锚点 id 模式一致");
  assert.ok(listSrc.includes("setTimeout"), "闪烁 class 须 setTimeout 移除");
  assert.ok(listSrc.includes("onLocate"), "须有 onLocate 回调");
  assert.ok(listSrc.includes("disabled={!locatable}"), "退役片段定位须禁用");
});

test("FragmentList: 走 fetchAllFragments 全量翻页、无硬编码样例、不直接 fetch", () => {
  assert.ok(
    listSrc.includes("fetchAllFragments"),
    "须走 fetchAllFragments 全量聚合",
  );
  assert.ok(!listSrc.includes("fetch("), "组件不直接 fetch，须走 client");
  assert.ok(!listSrc.includes("Lorem"));
  assert.ok(!listSrc.includes("示例"));
  assert.ok(!listSrc.includes("样例"));
});

test("两组件共用 fetchAllFragments：>200 片段时无高亮缺口（SOURCE-12-fix-1）", () => {
  assert.ok(
    readerSrc.includes("fetchAllFragments"),
    "SourceReader 须走 fetchAllFragments 全量翻页",
  );
  assert.ok(
    listSrc.includes("fetchAllFragments"),
    "FragmentList 须走 fetchAllFragments 全量翻页",
  );
  // 组件内不得再保留单页 listFragments 直调（翻页逻辑统一收口到 api.ts）
  assert.ok(!readerSrc.includes("listFragments"), "SourceReader 不得直调单页 listFragments");
  assert.ok(!listSrc.includes("listFragments"), "FragmentList 不得直调单页 listFragments");
});
