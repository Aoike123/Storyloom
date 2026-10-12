"""D03 precise text-range contract: half-open intervals over code points, versioned and conflict-checked.

The story source is the project's own ``story_source`` record (see ``story_sources.py``). Ranges are
half-open ``[start, end)`` code-point offsets into the stored content, never paragraph numbers,
display lines or DOM coordinates. Every write carries the body-text revision it was made against, so a
concurrent text change returns an explicit conflict instead of silently moving a range.

Contract: ``docs/workbench-redesign-2026-10-02/d03-range-contract.md``.
"""

import hashlib
import re
from typing import Literal

from fastapi import HTTPException

#: P-number window width in code points (``director.source_passages`` cuts the content into
#: ``240``-code-point windows; ``P{n}`` covers ``[(n-1)*240, n*240)``).
PASSAGE_WINDOW = 240

#: Fragment ids are stable, project-unique and never reused: ``F01``, ``F02``, ...
FRAGMENT_ID_PATTERN = re.compile(r"^F[0-9]{2,}$")


def revision_of(content: str) -> str:
    """The body-text revision: SHA-256 hex of the stored content (code points, UTF-8)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def text_length(content: str) -> int:
    """Code-point length of the stored content (Python ``len`` counts code points natively)."""
    return len(content)


def validate_range(start, end, content: str) -> list[str]:
    """Deterministic range legality: integers, in bounds, half-open, non-empty.

    Returns a list of human-readable issues (empty means valid). The frontend ``checkRange`` uses the
    same rules, so immediate feedback matches the server's final check.
    """
    issues = []
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        issues.append("范围边界必须是整数码点偏移。")
        return issues
    if start < 0:
        issues.append("范围起点不能为负。")
    if end > text_length(content):
        issues.append(f"范围终点 {end} 超出正文长度 {text_length(content)}。")
    if start >= end:
        issues.append("范围必须非空：起点必须小于终点。")
    if not issues and not content[start:end].strip():
        issues.append("选区不能为空白。")
    return issues


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open overlap: ``a.start < b.end && b.start < a.end``. Adjacent boundaries (``a.end == b.start``)
    do not overlap."""
    return a_start < b_end and b_start < a_end


def find_conflicts(start: int, end: int, fragments: list[dict]) -> list[dict]:
    """Existing fragments whose range overlaps the given range.

    ``fragments`` are dicts carrying ``fragment_id``, ``name`` and ``range`` (``{"start", "end"}``).
    Both candidates and confirmed fragments participate (contract §5).
    """
    return [
        f
        for f in fragments
        if overlaps(start, end, f["range"]["start"], f["range"]["end"])
    ]


def next_fragment_id(fragments: list[dict]) -> str:
    """Allocate the next stable fragment id (never reuses deleted ids)."""
    highest = 0
    for f in fragments:
        m = FRAGMENT_ID_PATTERN.fullmatch(str(f.get("fragment_id") or ""))
        if m:
            highest = max(highest, int(m.group(0)[1:]))
    return f"F{highest + 1:02d}"


def migrate_source_refs(refs: list[str], content: str) -> tuple[int, int] | None:
    """Map legacy paragraph-level ``source_refs`` (P numbers) to one continuous character range.

    ``P{n}`` covers ``[(n-1)*PASSAGE_WINDOW, n*PASSAGE_WINDOW)``. The refs must be ordered, unique and
    contiguous (the same rules ``segments.plan_issues`` enforces); anything else returns ``None`` so the
    caller reports the legacy plan as inconsistent instead of inventing a range.
    """
    numbers = []
    for ref in refs:
        m = re.fullmatch(r"P([0-9]{3,})", str(ref or "").strip().upper())
        if not m:
            return None
        numbers.append(int(m.group(1)))
    if not numbers:
        return None
    if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
        return None
    if any(b - a != 1 for a, b in zip(numbers, numbers[1:])):
        return None
    start = (numbers[0] - 1) * PASSAGE_WINDOW
    end = numbers[-1] * PASSAGE_WINDOW
    if start < 0 or end > text_length(content):
        return None
    return (start, end)


def check_full_plan(
    fragments: list[dict],
    content: str,
    production_start: int = 0,
    production_end: int | None = None,
) -> dict:
    """Full-cut submission check, separate from the per-candidate check (contract §7).

    ``fragments`` are the ranges being submitted as the complete cut (each ``{"fragment_id", "range"}``).
    Against the explicit production range (defaults to the whole stored text) it reports:

    - ``issues``: ranges out of order, out of bounds, or overlapping each other;
    - ``gaps``: covered holes inside the production range (missing coverage);
    - ``trims``: the head/tail of the production range not covered by any fragment (裁减／范围外);
    - ``out_of_range``: fragments extending beyond the production range.

    The old quantity budget (``MAX_SEGMENTS``) stays with the legacy computation window and is not
    applied here: a full story is not capped by it.
    """
    if production_end is None:
        production_end = text_length(content)
    ranges = sorted(
        (
            (f["range"]["start"], f["range"]["end"], f.get("fragment_id"))
            for f in fragments
        ),
        key=lambda r: (r[0], r[1]),
    )
    issues: list[str] = []
    for (start, end, fid) in ranges:
        for issue in validate_range(start, end, content):
            issues.append(f"{fid or '?'}: {issue}")
    for (a_s, a_e, a_id), (b_s, b_e, b_id) in zip(ranges, ranges[1:]):
        if a_s > b_s:
            issues.append(f"{a_id} 与 {b_id} 顺序与来源顺序不一致。")
        if overlaps(a_s, a_e, b_s, b_e):
            issues.append(f"{a_id} 与 {b_id} 范围重叠。")

    # gaps: uncovered holes strictly between consecutive fragments (internal)
    gaps: list[dict] = []
    for i in range(len(ranges) - 1):
        a_end = ranges[i][1]
        b_start = ranges[i + 1][0]
        if b_start > a_end:
            gaps.append({"start": a_end, "end": b_start})

    # trims: the head/tail of the production range not covered by any fragment (裁减／范围外)
    trims: list[dict] = []
    if ranges and ranges[0][0] > production_start:
        trims.append({"start": production_start, "end": ranges[0][0], "side": "head"})
    if ranges and ranges[-1][1] < production_end:
        trims.append({"start": ranges[-1][1], "end": production_end, "side": "tail"})

    out_of_range = [
        {"fragment_id": fid, "start": start, "end": end}
        for (start, end, fid) in ranges
        if start < production_start or end > production_end
    ]
    return {
        "issues": issues,
        "gaps": gaps,
        "trims": trims,
        "out_of_range": out_of_range,
    }


def fragment_dict(row) -> dict:
    """Serialize a ``source_fragment`` record (or its data dict) for the API, adding the ``stale`` flag."""
    data = row.data if hasattr(row, "data") else row
    range_ = data.get("range") or {}
    return {
        "fragment_id": data.get("fragment_id"),
        "name": data.get("name"),
        "status": data.get("status"),
        "origin": data.get("origin"),
        "range": {
            "start": range_.get("start"),
            "end": range_.get("end"),
        },
        "revision": data.get("revision"),
        "stale": bool(data.get("stale")),
        "created": data.get("created"),
        "updated": data.get("updated"),
    }


class RangeConflict(HTTPException):
    """A range write collided with existing fragments or a stale revision."""

    def __init__(self, detail: str):
        super().__init__(409, detail)
