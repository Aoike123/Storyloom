const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// Transpile client.ts (type-only import → no runtime require)
// ---------------------------------------------------------------------------
const clientSource = fs.readFileSync(
  path.join(__dirname, "../../shared/api/client.ts"),
  "utf8",
);
const transpiled = ts.transpileModule(clientSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

// ---------------------------------------------------------------------------
// Shared mutable state
// ---------------------------------------------------------------------------
let uuidCounter = 0;
let currentFetch;

// ---------------------------------------------------------------------------
// Build the VM context once
// ---------------------------------------------------------------------------
const store = new Map();
const localStorageImpl = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => void store.set(k, String(v)),
  removeItem: (k) => void store.delete(k),
};

const context = {
  window: { localStorage: localStorageImpl },
  fetch: (...args) => currentFetch(...args),
  AbortController: AbortController,
  crypto: { randomUUID: () => "fixed-uuid-" + ++uuidCounter },
  process: { env: {} },
  setTimeout,
  clearTimeout,
  exports: {},
};

vm.runInNewContext(transpiled, context);
const { createClient } = context.exports;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function makeClient(extraOpts) {
  return createClient(extraOpts);
}

// ---------------------------------------------------------------------------
// T1: Successful 200 response
// ---------------------------------------------------------------------------
test("T1: 200 returns ok:true with parsed data", async () => {
  currentFetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ id: "x" }),
    text: async () => JSON.stringify({ id: "x" }),
  });
  const client = makeClient();
  const result = await client.get("/anything");
  assert.equal(result.ok, true);
  assert.deepEqual(result.data, { id: "x" });
});

// ---------------------------------------------------------------------------
// T2: 409 revision_conflict with full error body
// ---------------------------------------------------------------------------
test("T2: 409 revision_conflict preserves code, status, details", async () => {
  const errorBody = {
    error: {
      code: "revision_conflict",
      message: "片段边界已变化",
      details: {
        object: { kind: "fragment", id: "f1" },
        expected: 3,
        actual: 5,
      },
    },
  };
  currentFetch = async () => ({
    ok: false,
    status: 409,
    text: async () => JSON.stringify(errorBody),
    json: async () => errorBody,
  });
  const client = makeClient();
  const result = await client.get("/fragments/f1");
  assert.equal(result.ok, false);
  assert.equal(result.code, "revision_conflict");
  assert.equal(result.status, 409);
  assert.equal(result.details.object.kind, "fragment");
  assert.equal(result.details.object.id, "f1");
  assert.equal(result.details.expected, 3);
  assert.equal(result.details.actual, 5);
});

// ---------------------------------------------------------------------------
// T3: 409 range_overlap with conflicts array
// ---------------------------------------------------------------------------
test("T3: 409 range_overlap details.conflicts[0].fragment_id", async () => {
  const errorBody = {
    error: {
      code: "range_overlap",
      message: "重叠",
      details: {
        conflicts: [
          { fragment_id: "f9", name: "旧片段", start: 0, end: 5 },
        ],
      },
    },
  };
  currentFetch = async () => ({
    ok: false,
    status: 409,
    text: async () => JSON.stringify(errorBody),
    json: async () => errorBody,
  });
  const client = makeClient();
  const result = await client.get("/fragments");
  assert.equal(result.ok, false);
  assert.equal(result.code, "range_overlap");
  assert.equal(result.details.conflicts[0].fragment_id, "f9");
});

// ---------------------------------------------------------------------------
// T4: Non-contract error body (plain text) → status-mapped code
// ---------------------------------------------------------------------------
test("T4: 404 with non-JSON body maps to not_found, never ok:true", async () => {
  currentFetch = async () => ({
    ok: false,
    status: 404,
    text: async () => "nope",
    json: async () => {
      throw new SyntaxError("Unexpected token");
    },
  });
  const client = makeClient();
  const result = await client.get("/missing");
  assert.equal(result.ok, false);
  assert.equal(result.code, "not_found");
  assert.equal(result.status, 404);
});

// ---------------------------------------------------------------------------
// T5: fetch rejects (network failure)
// ---------------------------------------------------------------------------
test("T5: fetch TypeError → code:network, no status", async () => {
  currentFetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  const client = makeClient();
  const result = await client.get("/x");
  assert.equal(result.ok, false);
  assert.equal(result.code, "network");
  assert.equal(result.message, "连接失败，请重试。");
  assert.equal(result.status, undefined);
});

// ---------------------------------------------------------------------------
// T6: Timeout aborts a hanging fetch
// ---------------------------------------------------------------------------
test("T6: timeoutMs:50 aborts within 2 s → code:network", async () => {
  currentFetch = (_url, opts) => {
    return new Promise((_resolve, reject) => {
      if (opts && opts.signal) {
        opts.signal.addEventListener("abort", () => {
          reject(new Error("Aborted"));
        });
      }
      // never resolves on its own
    });
  };
  const client = makeClient();
  const result = await Promise.race([
    client.get("/slow", { timeoutMs: 50 }),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("test harness timeout")), 2000),
    ),
  ]);
  assert.equal(result.ok, false);
  assert.equal(result.code, "network");
});

// ---------------------------------------------------------------------------
// T7: command_id generation and reuse
// ---------------------------------------------------------------------------
test("T7: post auto-generates command_id; explicit commandId is reused", async () => {
  let capturedBody;
  currentFetch = async (_url, opts) => {
    capturedBody = JSON.parse(opts.body);
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => "{}",
    };
  };
  const client = makeClient();

  // No commandId → auto-generated (first call → uuid-1)
  const r1 = await client.post("/action");
  assert.equal(r1.ok, true);
  assert.equal(capturedBody.command_id, "fixed-uuid-1");

  // Second call → different id (uuid-2)
  const r2 = await client.post("/action");
  assert.equal(r2.ok, true);
  assert.equal(capturedBody.command_id, "fixed-uuid-2");
  assert.notEqual(capturedBody.command_id, "fixed-uuid-1");

  // Explicit commandId → used as-is
  const r3 = await client.post("/action", undefined, {
    commandId: "fixed-id",
  });
  assert.equal(r3.ok, true);
  assert.equal(capturedBody.command_id, "fixed-id");
});

// ---------------------------------------------------------------------------
// T8: Authorization header from stored token
// ---------------------------------------------------------------------------
test("T8: setToken sets Bearer header; setToken(null) removes it", async () => {
  let capturedHeaders;
  currentFetch = async (_url, opts) => {
    capturedHeaders = opts.headers;
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => "{}",
    };
  };
  const client = makeClient();

  client.setToken("tok-123");
  await client.get("/auth");
  assert.equal(capturedHeaders["Authorization"], "Bearer tok-123");

  client.setToken(null);
  await client.get("/auth");
  assert.equal(capturedHeaders["Authorization"], undefined);
});

// ---------------------------------------------------------------------------
// T9: Default and custom base URL
// ---------------------------------------------------------------------------
test("T9: default base is 3011; custom baseUrl overrides", async () => {
  let capturedUrl;
  currentFetch = async (url) => {
    capturedUrl = url;
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => "{}",
    };
  };

  // Default (no baseUrl in ClientOptions, process.env empty)
  const c1 = makeClient();
  await c1.get("/x");
  assert.ok(
    capturedUrl.startsWith("http://127.0.0.1:3011"),
    `expected 3011, got ${capturedUrl}`,
  );

  // Custom baseUrl
  const c2 = makeClient({ baseUrl: "http://127.0.0.1:9999" });
  await c2.get("/x");
  assert.ok(
    capturedUrl.startsWith("http://127.0.0.1:9999"),
    `expected 9999, got ${capturedUrl}`,
  );
});
