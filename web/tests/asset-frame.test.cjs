const { test } = require("node:test");
const assert = require("node:assert/strict");
const ts = require("typescript");
const vm = require("vm");
const fs = require("fs");
const path = require("path");

// A tiny multi-slot hook harness: useState/useRef/useEffect keep their slots across renders,
// effects re-run when their deps change, and window timers are captured for manual flushing.
function loadHarness() {
  const states = [],
    refs = [],
    effects = [];
  let cursor = 0;
  const timers = [];
  const React = {
    useState(init) {
      const i = cursor++;
      if (!(i in states))
        states[i] = typeof init === "function" ? init() : init;
      return [
        states[i],
        (v) => {
          states[i] = typeof v === "function" ? v(states[i]) : v;
        },
      ];
    },
    useRef(init) {
      const i = cursor++;
      if (!(i in refs)) refs[i] = { current: init };
      return refs[i];
    },
    useEffect(fn, deps) {
      const i = cursor++;
      const prev = effects[i];
      const changed = !prev || !deps || deps.some((d, k) => d !== prev.deps[k]);
      if (!changed) return;
      if (prev && prev.cleanup) prev.cleanup();
      effects[i] = { fn, deps };
      const c = fn();
      if (typeof c === "function") effects[i].cleanup = c;
    },
  };
  const window = {
    setTimeout: (fn) => {
      timers.push(fn);
      return timers.length;
    },
    clearTimeout: (id) => {
      if (id) timers[id - 1] = null;
    },
  };
  const requireShim = (name) => {
    if (name === "react") return React;
    if (name === "react/jsx-runtime") return require("react/jsx-runtime");
    return require(name);
  };
  const file = path.join(__dirname, "..", "app", "author", "AssetFrame.tsx");
  const code = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const mod = { exports: {} };
  vm.runInNewContext(
    code,
    { require: requireShim, module: mod, exports: mod.exports, window },
    { filename: file },
  );
  return {
    AssetFrame: mod.exports.default,
    reset: () => {
      cursor = 0;
    },
    flush: () => {
      const t = timers.splice(0);
      for (const fn of t) if (fn) fn();
    },
  };
}

function kids(el) {
  const c = el.props.children;
  return Array.isArray(c) ? c.flat() : [c];
}

test("replacing the src crossfades: the old layer exits below, the new one enters on top", () => {
  const { AssetFrame, reset } = loadHarness();
  AssetFrame({ src: "a.jpg", alt: "镜头" });
  reset();
  const tree = AssetFrame({ src: "b.jpg", alt: "镜头" }); // effect adds the new layer during this render
  reset();
  const settled = AssetFrame({ src: "b.jpg", alt: "镜头" });
  const layers = kids(settled);
  assert.equal(layers.length, 2);
  // DOM order: old first (below), new last (on top)
  assert.equal(layers[0].props.src, "a.jpg");
  assert.ok(layers[0].props.className.includes("is-exiting"));
  assert.equal(layers[0].key, "a.jpg/exit");
  assert.equal(layers[1].props.src, "b.jpg");
  assert.ok(
    layers[1].props.className.includes("is-entering"),
    "the incoming layer must carry is-entering, otherwise it pops in instantly",
  );
  assert.equal(layers[1].key, "b.jpg");
});

test("pruning the exited layer neither remounts nor restyles the layer that stays", () => {
  const { AssetFrame, reset, flush } = loadHarness();
  AssetFrame({ src: "a.jpg", alt: "镜头" });
  reset();
  AssetFrame({ src: "b.jpg", alt: "镜头" });
  reset();
  flush(); // the 260ms prune timer
  const tree = AssetFrame({ src: "b.jpg", alt: "镜头" });
  const layers = kids(tree);
  assert.equal(layers.length, 1);
  assert.equal(
    layers[0].key,
    "b.jpg",
    "the kept layer must keep its key, otherwise React remounts it and the fade replays",
  );
  assert.ok(
    layers[0].props.className.includes("is-entering"),
    "the kept layer must keep its class, otherwise the animation restarts",
  );
  assert.ok(!layers[0].props.className.includes("is-first"));
});

test("first mount fades in once; re-rendering the same src adds no layer", () => {
  const { AssetFrame, reset } = loadHarness();
  const first = AssetFrame({ src: "a.jpg", alt: "镜头" });
  const firstLayers = kids(first);
  assert.equal(firstLayers.length, 1);
  assert.ok(firstLayers[0].props.className.includes("is-first"));
  reset();
  const again = AssetFrame({ src: "a.jpg", alt: "镜头" });
  assert.equal(kids(again).length, 1);
});
