"""SOURCE 域范围校验——服务端权威唯一实现（附录 00 §7 冻结，offset_policy lf-utf16-v1）。

附录 00 §7 公共规则：

- 范围 = canonical 文本的 UTF-16 code unit 半开区间 ``[start, end)``；
- canonical：``\\r\\n`` → ``\\n``、单独 ``\\r`` → ``\\n``（其余不变）；
- 判定优先级（严格顺序）：
  1. ``end <= start`` → ``"empty"``（含 start > end；先于越界判定）；
  2. ``start < 0`` 或 ``end > utf16_len(canonical)`` → ``"out_of_bounds"``；
  3. start 或 end 边界落在代理对内侧 → ``"surrogate_split"``
     （设 u[i] 为 canonical 的 UTF-16 code unit 数组：边界 b（0≤b≤len）
     在代理对内侧 ⇔ b>0 且 u[b-1]∈[0xD800,0xDBFF] 且 u[b]∈[0xDC00,0xDFFF]）；
  4. 切片内每个码点均属**冻结空白集**（v2.5：= JS ``\\s`` 码点集，
     见 ``_WHITESPACE_CODEPOINTS``；不用 ``isspace()``——其与 JS ``\\s``
     在 U+0085/U+001C-1F/U+FEFF 等码点上不一致）→ ``"blank"``；
  5. 否则 → ``"ok"``；
- 冲突：``a.start < b.end && b.start < a.end``（边界相接=不重叠；完全相同=重叠）。

本模块为 SOURCE/EDIT/REL 域范围校验的唯一实现（服务端权威）；纯函数、
无 DB/网络依赖；与 ``web/features/studio/source/ranges.ts`` 行为逐条一致
（两栈共用冻结 fixture ``tests/fixtures/studio/ranges.json`` 作为验收标准；
空白判定用同一冻结集，任意文本上跨栈严格相等）。

非 int 入参（含 bool）抛 ``TypeError``（在 docstring/注释中声明）。
"""

from __future__ import annotations

__all__ = [
    "canonicalize",
    "utf16_len",
    "validate_range",
    "slice_range",
    "ranges_overlap",
]


def canonicalize(raw: str) -> str:
    """把 raw 规范化为 canonical 文本：``\\r\\n``→``\\n``、单独 ``\\r``→``\\n``（其余不变）。"""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def utf16_len(s: str) -> int:
    """``s`` 的 UTF-16 code unit 计数（非补充平面每码点 1、补充平面每码点 2）。"""
    return len(s.encode("utf-16-be")) // 2


def _units(s: str) -> list[int]:
    """``s`` 的 UTF-16 code unit 数组（大端字节序，每 2 字节一个 code unit）。"""
    b = s.encode("utf-16-be")
    return [b[i] << 8 | b[i + 1] for i in range(0, len(b), 2)]


# 冻结空白集（附录 00 §7，v2.5）：= JS ``\s`` 码点集，两栈统一，
# 任意文本上 blank 判定跨栈严格相等（Python ``isspace()`` 与 JS ``\s``
# 在 U+0085/U+001C-1F/U+FEFF 等码点上不一致，故显式枚举）。
_WHITESPACE_CODEPOINTS = frozenset(
    {0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20}
    | {0x00A0, 0x1680}
    | set(range(0x2000, 0x200B))
    | {0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF}
)


def _is_whitespace(ch: str) -> bool:
    """码点是否属冻结空白集（v2.5）。"""
    return ord(ch) in _WHITESPACE_CODEPOINTS


def _check_int(value: object, name: str) -> int:
    """入参必须为 int（bool 虽是 int 子类，亦拒绝）；否则抛 TypeError。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def _pair(name: str, pair: object) -> tuple[int, int]:
    """(start, end) 二元组入参校验：容器须为长度 2 的 tuple/list，元素须为 int。"""
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        raise TypeError(f"{name} must be a (start, end) pair, got {type(pair).__name__}")
    return _check_int(pair[0], f"{name}.start"), _check_int(pair[1], f"{name}.end")


def validate_range(canonical: str, start: int, end: int) -> str:
    """按附录 00 §7 对 canonical 上的 ``[start, end)`` 做范围判定（优先级严格从上到下）。

    返回 ``"empty"`` / ``"out_of_bounds"`` / ``"surrogate_split"`` / ``"blank"`` / ``"ok"``。

    非 int（含 bool）start/end 入参 → ``TypeError``。
    """
    start = _check_int(start, "start")
    end = _check_int(end, "end")
    if end <= start:
        return "empty"
    if start < 0 or end > utf16_len(canonical):
        return "out_of_bounds"
    units = _units(canonical)
    for b in (start, end):
        if b > 0 and 0xD800 <= units[b - 1] <= 0xDBFF and 0xDC00 <= units[b] <= 0xDFFF:
            return "surrogate_split"
    seg = canonical.encode("utf-16-be")[start * 2 : end * 2].decode("utf-16-be")
    if all(_is_whitespace(ch) for ch in seg):
        return "blank"
    return "ok"


def slice_range(canonical: str, start: int, end: int) -> str | None:
    """判定为 ``"ok"`` 时返回 UTF-16 code unit 切片（``units[start:end]`` 拼 bytes 后
    ``.decode("utf-16-be")``）；否则返回 ``None``。

    非 int（含 bool）start/end 入参 → ``TypeError``。
    """
    start = _check_int(start, "start")
    end = _check_int(end, "end")
    if validate_range(canonical, start, end) != "ok":
        return None
    data = canonical.encode("utf-16-be")
    return data[start * 2 : end * 2].decode("utf-16-be")


def ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """重叠判定：``a.start < b.end && b.start < a.end``（边界相接=不重叠；完全相同=重叠）。

    a/b 为 ``(start, end)`` 二元组；非 int 元素（含 bool）→ ``TypeError``。
    """
    a_start, a_end = _pair("a", a)
    b_start, b_end = _pair("b", b)
    return a_start < b_end and b_start < a_end
