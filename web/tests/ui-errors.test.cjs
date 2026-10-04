const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const context = { exports: {} };
vm.runInNewContext(
  ts.transpileModule(
    fs.readFileSync(path.join(__dirname, "../app/ui-errors.ts"), "utf8"),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText,
  context,
);
const { uiErrorMessage } = context.exports;

test("browser network failures explain how to recover in Chinese", () => {
  for (const message of [
    "Failed to fetch",
    "Load failed",
    "NetworkError when attempting to fetch resource.",
  ]) {
    assert.equal(
      uiErrorMessage(new TypeError(message)),
      "无法连接本地服务，请确认服务正在运行后重试。",
    );
  }
});

test("specific service errors stay intact, while unreadable responses get the action fallback", () => {
  assert.equal(
    uiErrorMessage(new Error("请先配置画面模型。")),
    "请先配置画面模型。",
  );
  assert.equal(
    uiErrorMessage(
      new SyntaxError("Unexpected token < in JSON"),
      "导入失败，请重试。",
    ),
    "导入失败，请重试。",
  );
  assert.equal(
    uiErrorMessage(null, "导入失败，请重试。"),
    "导入失败，请重试。",
  );
});
