"""SOURCE-02：ranges.py（附录 00 §7，offset_policy lf-utf16-v1）fixture 驱动测试。

唯一事实来源 = 冻结 fixture tests/fixtures/studio/ranges.json（9 案例，不得修改）。
纯函数，无 DB fixture、不写 STUDIO_* env（conftest 的 session 丢弃库机制不受影响）。
"""

import json
import pathlib

import pytest

from backend.studio.sources.ranges import (
    canonicalize,
    ranges_overlap,
    slice_range,
    utf16_len,
    validate_range,
)

_FIXTURE = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1]
        / "fixtures"
        / "studio"
        / "ranges.json"
    ).read_text(encoding="utf-8")
)

# 前置断言：schema 与案例数
assert _FIXTURE["schema"] == "storyloom.ranges-fixture/v1", (
    f"fixture schema mismatch: {_FIXTURE['schema']!r}"
)
CASES = _FIXTURE["cases"]
assert len(CASES) == 9, f"expected 9 frozen cases, got {len(CASES)}"


def _case_id(case: dict) -> str:
    return case["name"]


# ---------------------------------------------------------------------------
# canonicalize / utf16_len（9 例）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[_case_id(c) for c in CASES])
def test_canonicalize(case: dict) -> None:
    got = canonicalize(case["raw"])
    assert got == case["canonical"], (
        f"case={case['name']}: canonicalize(raw={case['raw']!r}) "
        f"= {got!r}, expected {case['canonical']!r}"
    )


@pytest.mark.parametrize("case", CASES, ids=[_case_id(c) for c in CASES])
def test_utf16_len(case: dict) -> None:
    got = utf16_len(case["canonical"])
    assert got == case["utf16_len"], (
        f"case={case['name']}: utf16_len({case['canonical']!r}) = {got}, "
        f"expected {case['utf16_len']}"
    )


# ---------------------------------------------------------------------------
# validate_range / slice_range（展平所有 case/range）
# ---------------------------------------------------------------------------

_RANGE_PARAMS = [
    (case, rng) for case in CASES for rng in case.get("ranges", [])
]
assert _RANGE_PARAMS, "fixture contains no ranges"
_RANGE_IDS = [
    f"{case['name']}[{rng['start']}-{rng['end']}]" for case, rng in _RANGE_PARAMS
]


@pytest.mark.parametrize(("case", "rng"), _RANGE_PARAMS, ids=_RANGE_IDS)
def test_validate_range(case: dict, rng: dict) -> None:
    got = validate_range(case["canonical"], rng["start"], rng["end"])
    assert got == rng["verdict"], (
        f"case={case['name']} start={rng['start']} end={rng['end']}: "
        f"validate_range = {got!r}, expected {rng['verdict']!r}"
    )


@pytest.mark.parametrize(("case", "rng"), _RANGE_PARAMS, ids=_RANGE_IDS)
def test_slice_range(case: dict, rng: dict) -> None:
    got = slice_range(case["canonical"], rng["start"], rng["end"])
    expected = rng["slice"]  # null ⇔ None
    assert got == expected, (
        f"case={case['name']} start={rng['start']} end={rng['end']}: "
        f"slice_range = {got!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# ranges_overlap（展平所有 case/pair）
# ---------------------------------------------------------------------------

_PAIR_PARAMS = [(case, p) for case in CASES for p in case.get("pairs", [])]
assert _PAIR_PARAMS, "fixture contains no pairs"
_PAIR_IDS = [
    f"{case['name']}({p['a']}x{p['b']})" for case, p in _PAIR_PARAMS
]


@pytest.mark.parametrize(("case", "pair"), _PAIR_PARAMS, ids=_PAIR_IDS)
def test_ranges_overlap(case: dict, pair: dict) -> None:
    got = ranges_overlap(tuple(pair["a"]), tuple(pair["b"]))
    assert got == pair["overlap"], (
        f"case={case['name']} a={pair['a']} b={pair['b']}: "
        f"ranges_overlap = {got}, expected {pair['overlap']}"
    )


# ---------------------------------------------------------------------------
# 非 int 入参（含 bool）→ TypeError
# ---------------------------------------------------------------------------

_BAD_INTS = ["0", 0.5, True]
_CANONICAL = CASES[0]["canonical"]


@pytest.mark.parametrize("bad", _BAD_INTS, ids=["str-0", "float-0.5", "bool-True"])
def test_validate_range_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        validate_range(_CANONICAL, bad, 2)
    with pytest.raises(TypeError):
        validate_range(_CANONICAL, 2, bad)


@pytest.mark.parametrize("bad", _BAD_INTS, ids=["str-0", "float-0.5", "bool-True"])
def test_slice_range_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        slice_range(_CANONICAL, bad, 2)


@pytest.mark.parametrize("bad", _BAD_INTS, ids=["str-0", "float-0.5", "bool-True"])
def test_ranges_overlap_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        ranges_overlap((bad, 2), (0, 2))
    with pytest.raises(TypeError):
        ranges_overlap((0, 2), (bad, 2))


# ---------------------------------------------------------------------------
# 冻结空白集（v2.5）：与 web/tests/studio/ranges.test.cjs 同名断言逐条一致，
# 任意文本上两栈 blank/ok 判定严格相等（fixture 之外的码点级单测）。
# ---------------------------------------------------------------------------

# (文本, start, end, 期望判定)：空白集成员 → blank；分歧码点（NEL/FS/GS）→ ok
_FROZEN_WS_PARAMS = [
    ("\u00a0", 0, 1, "blank"),   # NBSP
    ("\u1680", 0, 1, "blank"),   # OGHAM SPACE MARK
    ("\u2007", 0, 1, "blank"),   # U+2000-0A 中点
    ("\u3000", 0, 1, "blank"),   # 全角空格
    ("\ufeff", 0, 1, "blank"),   # BOM/ZWNBSP（冻结集成员；Python isspace() 判 False 的分歧点）
    ("\t\n", 0, 2, "blank"),     # C0 空白
    ("\u0085", 0, 1, "ok"),      # NEL（冻结集非成员；Python isspace() 判 True 的分歧点）
    ("\u001c", 0, 1, "ok"),      # FILE SEPARATOR
    ("\u001f", 0, 1, "ok"),      # UNIT SEPARATOR
    ("\u0085\u00a0", 0, 2, "ok"),  # 含非空白（NEL）即非 blank
]
_FROZEN_IDS = [
    f"U+{ord(chs[0]):04X}" + (f".." if len(chs) > 1 else "") for chs, *_ in _FROZEN_WS_PARAMS
]


@pytest.mark.parametrize(
    ("s", "start", "end", "expected"), _FROZEN_WS_PARAMS, ids=_FROZEN_IDS
)
def test_frozen_whitespace_set(s: str, start: int, end: int, expected: str) -> None:
    got = validate_range(s, start, end)
    assert got == expected, (
        f"frozen-whitespace: {s!r} [{start}-{end}] = {got!r}, expected {expected!r}"
    )
