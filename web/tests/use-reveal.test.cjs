const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const source = fs.readFileSync(
  path.join(__dirname, "../app/useReveal.ts"),
  "utf8",
);

// A minimal hook harness: the hook body runs against fake react primitives, and the captured
// effect is invoked manually, the way a renderer would.
function makeHarness({ reduced }) {
  const hooks = { calls: 0 };
  hooks.ref = { current: null };
  hooks.revealed = false;
  hooks.effect = undefined;
  const react = {
    useRef: (v) => hooks.ref,
    useState: (v) => [
      hooks.revealed,
      (next) => {
        hooks.revealed =
          typeof next === "function" ? next(hooks.revealed) : next;
      },
    ],
    useEffect: (fn) => {
      hooks.effect = fn;
    },
  };
  const observers = [];
  const context = {
    exports: {},
    window: { matchMedia: () => ({ matches: reduced }) },
    IntersectionObserver: class {
      constructor(cb) {
        this.cb = cb;
        this.observed = [];
        this.unobserved = [];
        observers.push(this);
      }
      observe(el) {
        this.observed.push(el);
      }
      unobserve(el) {
        this.unobserved.push(el);
      }
      disconnect() {}
    },
    require: (name) =>
      name === "react"
        ? react
        : name === "./reader-types"
          ? { reducedMotion: () => reduced }
          : null,
  };
  const code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  vm.runInNewContext(code, context);
  return {
    hooks,
    observers,
    runHook: () => {
      hooks.revealed = false;
      hooks.effect = undefined;
      const result = context.exports.useReveal();
      hooks.effect();
      return result;
    },
  };
}

test("a section reveals on first intersection and the observer then steps away", () => {
  const { hooks, observers, runHook } = makeHarness({ reduced: false });
  const [ref, revealed] = runHook();
  assert.equal(ref, hooks.ref);
  assert.equal(revealed, false);
  hooks.ref.current = { id: "shelf" };
  hooks.effect();
  assert.equal(observers.length, 1);
  assert.deepEqual(observers[0].observed, [{ id: "shelf" }]);
  observers[0].cb([{ isIntersecting: true, target: { id: "shelf" } }]);
  assert.equal(hooks.revealed, true);
  assert.deepEqual(observers[0].unobserved, [{ id: "shelf" }]);
});

test("reduced motion reveals immediately without an observer", () => {
  const { hooks, observers, runHook } = makeHarness({ reduced: true });
  hooks.ref.current = { id: "shelf" };
  const [ref] = runHook();
  assert.equal(ref, hooks.ref);
  assert.equal(hooks.revealed, true);
  assert.equal(observers.length, 0);
});
