// 附录 00 §7 冻结（offset_policy lf-utf16-v1）：
// - 范围 = canonical 文本的 UTF-16 code unit 半开区间 [start, end)；
// - canonical: \r\n → \n、单独 \r → \n（其余不变）；
// - 判定优先级（严格顺序）：
//   1. end <= start → "empty"（含 start > end；先于越界判定）；
//   2. start < 0 或 end > utf16Len(canonical) → "out_of_bounds"；
//   3. start 或 end 边界落在代理对内侧 → "surrogate_split"
//      （设 u[i] 为 canonical 的 UTF-16 code unit 数组：边界 b（0≤b≤len）
//      在代理对内侧 ⇔ b>0 且 u[b-1]∈[0xD800,0xDBFF] 且 u[b]∈[0xDC00,0xDFFF]）；
//   4. 切片内每个 UTF-16 unit 均属**冻结空白集**（v2.5：= JS /\s/ 码点集：
//      U+0009~0D、U+0020、U+00A0、U+1680、U+2000-0A、U+2028/29、U+202F、
//      U+205F、U+3000、U+FEFF；surrogate 半字必非空白）→ "blank"；
//   5. 否则 → "ok"；
// - 重叠：a.start < b.end && b.start < a.end（边界相接=不重叠；完全相同=重叠）。
//
// 与服务端 backend/studio/sources/ranges.py 行为逐条一致（服务端权威；
// 两栈共用冻结 fixture tests/fixtures/studio/ranges.json 作为验收标准）。
// 前端选区提交前合成单一连续区间后用此校验（附录 02/12）。
//
// 非有限整数入参（NaN/Infinity/小数）抛 RangeError。不依赖 DOM/网络。

export type Range = { start: number; end: number };

function checkInt(value: number, name: string): number {
  if (!Number.isInteger(value)) {
    throw new RangeError(`${name} must be a finite integer, got ${String(value)}`);
  }
  return value;
}

export function canonicalize(raw: string): string {
  return raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

export function utf16Len(s: string): number {
  return s.length;
}

export function validateRange(
  canonical: string,
  start: number,
  end: number,
): "empty" | "out_of_bounds" | "surrogate_split" | "blank" | "ok" {
  start = checkInt(start, "start");
  end = checkInt(end, "end");
  if (end <= start) return "empty";
  if (start < 0 || end > canonical.length) return "out_of_bounds";
  for (const b of [start, end]) {
    if (b > 0) {
      const prev = canonical.charCodeAt(b - 1);
      const next = canonical.charCodeAt(b);
      if (prev >= 0xd800 && prev <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) {
        return "surrogate_split";
      }
    }
  }
  for (let i = start; i < end; i++) {
    if (!/\s/.test(canonical.charAt(i))) return "ok";
  }
  return "blank";
}

export function sliceRange(canonical: string, start: number, end: number): string | null {
  start = checkInt(start, "start");
  end = checkInt(end, "end");
  if (validateRange(canonical, start, end) !== "ok") return null;
  return canonical.slice(start, end);
}

export function rangesOverlap(a: Range, b: Range): boolean {
  checkInt(a.start, "a.start");
  checkInt(a.end, "a.end");
  checkInt(b.start, "b.start");
  checkInt(b.end, "b.end");
  return a.start < b.end && b.start < a.end;
}
