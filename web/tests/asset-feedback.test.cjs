const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function load(file, globals = {}) {
  const context = { exports: {}, require, ...globals };
  const code = ts.transpileModule(
    fs.readFileSync(path.join(__dirname, "../app/author", file), "utf8"),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    },
  ).outputText;
  vm.runInNewContext(code, context);
  return context.exports;
}
const AssetFeedback = load("AssetFeedback.tsx").default;
const base = {
  name: "服装参考图",
  task: { id: "current-image", status: "completed", kind: "image" },
  text: "衣服改成黑色",
  busy: false,
  inFlight: false,
  browsingEarlier: false,
  onTextChange: () => {},
  onSubmit: () => {},
};
const button = (tree) =>
  tree.props.children.find((child) => child?.type === "button");

test("a valid note enables the feedback button and clicking submits", () => {
  let submitted = 0;
  const props = { ...base, onSubmit: () => submitted++ };
  const tree = AssetFeedback(props);
  assert.equal(button(tree).props.disabled, false);
  button(tree).props.onClick();
  assert.equal(submitted, 1);
  const html = renderToStaticMarkup(React.createElement(AssetFeedback, props));
  assert.match(html, /让 AI 按意见修改/);
});

test("unavailable feedback actions explain the specific blocking condition", () => {
  for (const [override, reason] of [
    [{ text: "" }, "修改意见"],
    [{ busy: true }, "正在提交"],
    [{ inFlight: true }, "本轮任务正在执行"],
    [{ browsingEarlier: true }, "返回当前进度"],
    [{ task: null }, "尚未准备好"],
    [{ task: { ...base.task, status: "superseded" } }, "当前不能修改"],
  ]) {
    const tree = AssetFeedback({ ...base, ...override });
    assert.equal(button(tree).props.disabled, true);
    assert.ok(button(tree).props.title.includes(reason));
    assert.ok(renderToStaticMarkup(tree).includes(reason));
  }
});

test("asset feedback carries no permission or checkbox control", () => {
  const html = renderToStaticMarkup(React.createElement(AssetFeedback, base));
  assert.doesNotMatch(html, /type="checkbox"/);
  assert.doesNotMatch(html, /模型调用许可/);
});
