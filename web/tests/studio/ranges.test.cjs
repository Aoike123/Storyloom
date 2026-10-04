const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

// ---------------------------------------------------------------------------
// Transpile ranges.ts（与 api-client.test.cjs 同模式）
// ---------------------------------------------------------------------------
const source = fs.readFileSync(
  path.join(__dirname, "../../features/studio/source/ranges.ts"),
  "utf8",
);
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

const context = { exports: {} };
vm.runInNewContext(transpiled, context);
const {
  canonicalize,
  utf16Len,
  validateRange,
  sliceRange,
  rangesOverlap,
} = context.exports;

// ---------------------------------------------------------------------------
// 冻结 fixture（web/tests/studio 到仓库根为上三级）
// ---------------------------------------------------------------------------
const fixture = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "../../../tests/fixtures/studio/ranges.json"),
    "utf8",
  ),
);
assert.equal(fixture.schema, "storyloom.ranges-fixture/v1");
const CASES = fixture.cases;
assert.equal(CASES.length, 9);

// ---------------------------------------------------------------------------
// canonicalize / utf16Len（9 例）
// ---------------------------------------------------------------------------
for (const c of CASES) {
  test(`canonicalize: ${c.name}`, () => {
    const got = canonicalize(c.raw);
    assert.equal(
      got,
      c.canonical,
      `case=${c.name}: canonicalize(${JSON.stringify(c.raw)}) = ${JSON.stringify(got)}, expected ${JSON.stringify(c.canonical)}`,
    );
  });

  test(`utf16Len: ${c.name}`, () => {
    const got = utf16Len(c.canonical);
    assert.equal(
      got,
      c.utf16_len,
      `case=${c.name}: utf16Len(${JSON.stringify(c.canonical)}) = ${got}, expected ${c.utf16_len}`,
    );
  });
}

// ---------------------------------------------------------------------------
// validateRange / sliceRange（展平所有 case/range）
// ---------------------------------------------------------------------------
for (const c of CASES) {
  for (const r of c.ranges ?? []) {
    test(`validateRange: ${c.name} [${r.start}-${r.end}]`, () => {
      const got = validateRange(c.canonical, r.start, r.end);
      assert.equal(
        got,
        r.verdict,
        `case=${c.name} start=${r.start} end=${r.end}: validateRange = ${got}, expected ${r.verdict}`,
      );
    });

    test(`sliceRange: ${c.name} [${r.start}-${r.end}]`, () => {
      const got = sliceRange(c.canonical, r.start, r.end);
      const expected = r.slice; // null ⇔ null
      assert.equal(
        got,
        expected,
        `case=${c.name} start=${r.start} end=${r.end}: sliceRange = ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`,
      );
    });
  }
}

// ---------------------------------------------------------------------------
// rangesOverlap（展平所有 case/pair）
// ---------------------------------------------------------------------------
for (const c of CASES) {
  for (const p of c.pairs ?? []) {
    test(
      `rangesOverlap: ${c.name} [${p.a}]x[${p.b}]`,
      () => {
        const got = rangesOverlap(
          { start: p.a[0], end: p.a[1] },
          { start: p.b[0], end: p.b[1] },
        );
        assert.equal(
          got,
          p.overlap,
          `case=${c.name} a=${JSON.stringify(p.a)} b=${JSON.stringify(p.b)}: rangesOverlap = ${got}, expected ${p.overlap}`,
        );
      },
    );
  }
}

// ---------------------------------------------------------------------------
// 非有限整数入参 → RangeError
// 注：vm context 抛出的错误属于另一 realm，instanceof 宿主 RangeError 恒假，
// 故按 err.name 与消息断言（仍锁定 RangeError 语义）。
// ---------------------------------------------------------------------------
const isRangeError = (err) =>
  err.name === "RangeError" && /must be a finite integer/.test(String(err.message));

test("validateRange rejects NaN / 1.5 (RangeError)", () => {
  assert.throws(() => validateRange("abc", Number.NaN, 2), isRangeError);
  assert.throws(() => validateRange("abc", 1.5, 2), isRangeError);
  assert.throws(() => validateRange("abc", 2, Number.NaN), isRangeError);
  assert.throws(() => validateRange("abc", 2, 1.5), isRangeError);
});

test("sliceRange rejects NaN / 1.5 (RangeError)", () => {
  assert.throws(() => sliceRange("abc", Number.NaN, 2), isRangeError);
  assert.throws(() => sliceRange("abc", 1.5, 2), isRangeError);
});

test("rangesOverlap rejects NaN / 1.5 (RangeError)", () => {
  assert.throws(
    () => rangesOverlap({ start: Number.NaN, end: 2 }, { start: 0, end: 2 }),
    isRangeError,
  );
  assert.throws(
    () => rangesOverlap({ start: 0, end: 2 }, { start: 1.5, end: 2 }),
    isRangeError,
  );
});

// ---------------------------------------------------------------------------
// 冻结空白集（v2.5）：与 tests/studio/test_ranges.py 同名断言逐条一致——
// 任意文本上两栈 blank/ok 判定严格相等（码点级单测，fixture 之外）。
// ---------------------------------------------------------------------------
const FROZEN_WS_PARAMS = [
  ["\u00a0", 0, 1, "blank"],    // NBSP
  ["\u1680", 0, 1, "blank"],    // OGHAM SPACE MARK
  ["\u2007", 0, 1, "blank"],    // U+2000-0A 中点
  ["\u3000", 0, 1, "blank"],    // 全角空格
  ["\ufeff", 0, 1, "blank"],    // BOM/ZWNBSP（冻结集成员）
  ["\t\n", 0, 2, "blank"],      // C0 空白
  ["\u0085", 0, 1, "ok"],       // NEL（冻结集非成员）
  ["\u001c", 0, 1, "ok"],       // FILE SEPARATOR
  ["\u001f", 0, 1, "ok"],       // UNIT SEPARATOR
  ["\u0085\u00a0", 0, 2, "ok"], // 含非空白（NEL）即非 blank
];

test("frozen whitespace set: blank/ok verdicts (v2.5 cross-stack parity)", () => {
  for (const [s, start, end, expected] of FROZEN_WS_PARAMS) {
    const got = validateRange(s, start, end);
    assert.equal(
      got,
      expected,
      `frozen-whitespace: ${JSON.stringify(s)} [${start}-${end}] = ${got}, expected ${expected}`,
    );
  }
});
