"""D03 precise text-range contract: half-open code-point intervals, versioned and conflict-checked.

Covers the contract in docs/workbench-redesign-2026-10-02/d03-range-contract.md:
- character counting unit (code points), newline convention, body-text revision
- half-open interval legality, non-empty, conflict (overlap vs adjacent)
- stable fragment ids, candidate/confirm, full-plan check (order + coverage + trim)
- legacy paragraph-reference migration
"""

import hashlib

from sqlalchemy import select

from backend import source_ranges
from backend.db import Record, Session

CONTENT = (
    "在雨夜里，灯塔守塔人发现光并非来自灯，而是来自海面。\n"
    "他沿着潮声走向礁石，发现一道裂缝里嵌着一枚熄灭的灯芯。\n"
    "黎明前，他把灯芯放回灯室，海面却开始发出微弱的光。"
)


# ---------------------------------------------------------------------------
# Unit: character counting, revision, range legality
# ---------------------------------------------------------------------------


def test_revision_is_sha256_of_content():
    assert source_ranges.revision_of(CONTENT) == hashlib.sha256(
        CONTENT.encode("utf-8")
    ).hexdigest()


def test_text_length_counts_code_points():
    # The content contains no astral characters, so code points == UTF-16 units here.
    assert source_ranges.text_length(CONTENT) == len(CONTENT)
    # An astral character is one code point but two UTF-16 units.
    assert source_ranges.text_length("a\U0001F600b") == 3


def test_validate_range_accepts_a_valid_half_open_interval():
    assert source_ranges.validate_range(0, 5, CONTENT) == []


def test_validate_range_rejects_empty_and_whitespace_only():
    # start == end is empty
    assert source_ranges.validate_range(3, 3, CONTENT)
    # the newline at index 26 is whitespace-only -> rejected
    assert source_ranges.validate_range(26, 27, CONTENT)
    # a non-whitespace span is valid
    assert source_ranges.validate_range(0, 5, CONTENT) == []


def test_validate_range_rejects_out_of_bounds():
    assert source_ranges.validate_range(-1, 5, CONTENT)
    assert source_ranges.validate_range(0, len(CONTENT) + 1, CONTENT)


def test_validate_range_rejects_non_integer():
    assert source_ranges.validate_range(0.5, 5, CONTENT)
    assert source_ranges.validate_range("0", 5, CONTENT)


def test_overlap_half_open():
    # overlapping
    assert source_ranges.overlaps(0, 5, 4, 9)
    # contained
    assert source_ranges.overlaps(0, 10, 2, 5)
    # adjacent (shared boundary) is NOT an overlap
    assert not source_ranges.overlaps(0, 5, 5, 10)
    # disjoint
    assert not source_ranges.overlaps(0, 5, 6, 9)


def test_find_conflicts():
    fragments = [
        {"fragment_id": "F01", "name": "a", "range": {"start": 0, "end": 10}},
        {"fragment_id": "F02", "name": "b", "range": {"start": 20, "end": 30}},
    ]
    # overlaps F01
    assert [f["fragment_id"] for f in source_ranges.find_conflicts(5, 15, fragments)] == ["F01"]
    # adjacent to F01 (start at 10) is not a conflict
    assert source_ranges.find_conflicts(10, 15, fragments) == []
    # overlaps F02
    assert [f["fragment_id"] for f in source_ranges.find_conflicts(25, 35, fragments)] == ["F02"]


def test_next_fragment_id_never_reuses():
    fragments = [
        {"fragment_id": "F01"},
        {"fragment_id": "F03"},  # F02 was deleted; the id must not be reused
    ]
    assert source_ranges.next_fragment_id(fragments) == "F04"
    assert source_ranges.next_fragment_id([]) == "F01"


# ---------------------------------------------------------------------------
# Unit: legacy paragraph-reference migration
# ---------------------------------------------------------------------------


def test_migrate_source_refs_maps_p_numbers_to_code_point_range():
    # P{n} covers [(n-1)*240, n*240). A contiguous run maps to one continuous range.
    rng = source_ranges.migrate_source_refs(["P001", "P002", "P003"], "x" * 1000)
    assert rng == (0, 720)
    rng = source_ranges.migrate_source_refs(["P002"], "x" * 1000)
    assert rng == (240, 480)


def test_migrate_source_refs_rejects_non_contiguous_and_unordered():
    assert source_ranges.migrate_source_refs(["P001", "P003"], "x" * 1000) is None
    assert source_ranges.migrate_source_refs(["P002", "P001"], "x" * 1000) is None
    assert source_ranges.migrate_source_refs(["P001", "P001"], "x" * 1000) is None
    assert source_ranges.migrate_source_refs([], "x" * 1000) is None


def test_migrate_source_refs_rejects_out_of_bounds():
    # content shorter than the window end
    assert source_ranges.migrate_source_refs(["P005"], "x" * 100) is None


# ---------------------------------------------------------------------------
# Unit: full-plan check (order + coverage + trim/out-of-range)
# ---------------------------------------------------------------------------


def _frag(start, end, fid="F01"):
    return {"fragment_id": fid, "range": {"start": start, "end": end}}


def test_full_plan_detects_overlap_and_order_issues():
    report = source_ranges.check_full_plan(
        [_frag(0, 10, "F01"), _frag(5, 20, "F02")], "x" * 100
    )
    assert any("重叠" in i for i in report["issues"])


def test_full_plan_reports_gaps_and_trims():
    # one fragment covering the middle: head/tail trims + no internal gap
    report = source_ranges.check_full_plan([_frag(10, 50)], "x" * 100)
    assert report["trims"] == [
        {"start": 0, "end": 10, "side": "head"},
        {"start": 50, "end": 100, "side": "tail"},
    ]
    assert report["gaps"] == []

    # two fragments with a hole between them
    report = source_ranges.check_full_plan(
        [_frag(0, 10, "F01"), _frag(20, 30, "F02")], "x" * 100
    )
    assert {"start": 10, "end": 20} in report["gaps"]
    assert {"start": 30, "end": 100, "side": "tail"} in report["trims"]


def test_full_plan_full_coverage_has_no_gaps_or_trims():
    report = source_ranges.check_full_plan(
        [_frag(0, 50, "F01"), _frag(50, 100, "F02")], "x" * 100
    )
    assert report["gaps"] == []
    assert report["trims"] == []
    assert report["issues"] == []


# ---------------------------------------------------------------------------
# Endpoint: candidate/confirm/boundary/delete/list/plan/migrate (logged-in owner)
# ---------------------------------------------------------------------------


def _login(client, username="author1"):
    r = client.post("/api/auth/register", json={"username": username, "password": "secret1"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_project_with_story(client, token, content=CONTENT, title="灯塔"):
    r = client.post(
        "/api/author/projects",
        json={"title": title},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = client.post(
        f"/api/author/projects/{pid}/story",
        json={"content": content, "title": title},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return pid, r.json()["source"]["content_hash"]


def test_create_and_list_fragment(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    frag = r.json()["fragment"]
    assert frag["fragment_id"] == "F01"
    assert frag["status"] == "candidate"
    assert frag["range"] == {"start": 0, "end": 24}

    listing = client.get(
        f"/api/author/projects/{pid}/source/fragments", headers=_auth(token)
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["revision"] == revision
    assert len(body["fragments"]) == 1
    assert body["fragments"][0]["stale"] is False


def test_create_fragment_rejects_overlap(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    )
    # overlapping range is rejected with 409
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 20, "end": 40}, "expected_revision": revision},
        headers=_auth(token),
    )
    assert r.status_code == 409
    # adjacent range (start at 24) is allowed
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 24, "end": 48}, "expected_revision": revision},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["fragment"]["fragment_id"] == "F02"


def test_create_fragment_rejects_stale_revision(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": "stale-rev"},
        headers=_auth(token),
    )
    assert r.status_code == 409


def test_confirm_fragment_is_independent(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    frag = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    ).json()["fragment"]
    fid = frag["fragment_id"]
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments/{fid}/confirm",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["fragment"]["status"] == "confirmed"
    # confirming again is a 409
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments/{fid}/confirm",
        headers=_auth(token),
    )
    assert r.status_code == 409


def test_update_fragment_boundary_with_conflict_check(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    f1 = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    ).json()["fragment"]
    f2 = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 24, "end": 48}, "expected_revision": revision},
        headers=_auth(token),
    ).json()["fragment"]
    # extending f1 into f2's range is rejected
    r = client.put(
        f"/api/author/projects/{pid}/source/fragments/{f1['fragment_id']}",
        json={"range": {"start": 0, "end": 30}, "expected_revision": revision},
        headers=_auth(token),
    )
    assert r.status_code == 409
    # shrinking f1 is allowed
    r = client.put(
        f"/api/author/projects/{pid}/source/fragments/{f1['fragment_id']}",
        json={"range": {"start": 0, "end": 20}, "expected_revision": revision},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["fragment"]["range"] == {"start": 0, "end": 20}


def test_delete_fragment_does_not_reuse_id(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    f1 = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    ).json()["fragment"]
    r = client.delete(
        f"/api/author/projects/{pid}/source/fragments/{f1['fragment_id']}",
        headers=_auth(token),
    )
    assert r.status_code == 200
    # the next fragment must not reuse F01
    f2 = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 24, "end": 48}, "expected_revision": revision},
        headers=_auth(token),
    ).json()["fragment"]
    assert f2["fragment_id"] == "F02"


def test_plan_check_reports_coverage(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    # a plan that leaves a gap in the middle
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments/plan",
        json={
            "fragments": [
                {"start": 0, "end": 10},
                {"start": 30, "end": 60},
            ],
            "expected_revision": revision,
        },
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert {"start": 10, "end": 30} in report["gaps"]
    assert {"start": 60, "end": len(CONTENT), "side": "tail"} in report["trims"]


def test_workspace_includes_source_revision_and_fragments(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    )
    work = client.get(f"/api/author/projects/{pid}", headers=_auth(token)).json()
    assert work["source_revision"] == revision
    assert len(work["fragments"]) == 1
    assert work["fragments"][0]["stale"] is False


def test_reimport_marks_fragments_stale(client):
    token = _login(client)
    pid, revision = _create_project_with_story(client, token)
    client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    )
    # re-import a different text -> new revision -> the fragment is stale
    new_content = CONTENT + " 新增的一段正文。"
    r = client.post(
        f"/api/author/projects/{pid}/story",
        json={"content": new_content},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    new_revision = r.json()["source"]["content_hash"]
    assert new_revision != revision
    listing = client.get(
        f"/api/author/projects/{pid}/source/fragments", headers=_auth(token)
    ).json()
    assert listing["fragments"][0]["stale"] is True
    # a write against the old revision is rejected
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments",
        json={"range": {"start": 0, "end": 24}, "expected_revision": revision},
        headers=_auth(token),
    )
    assert r.status_code == 409


def test_migrate_from_legacy_segment_plan(client):
    token = _login(client)
    # a 1200-code-point content so that P001..P005 (240 each) exist
    long_content = (CONTENT * 15)
    pid, revision = _create_project_with_story(client, token, content=long_content)
    # plant a legacy segment plan on the project (P-number source_refs)
    with Session.begin() as db:
        project = db.get(Record, pid)
        project.data = {
            **project.data,
            "director_id": "legacy-director",
        }
        db.add(
            Record(
                id="legacy-director",
                kind="director",
                data={
                    "segments": {
                        "title": "旧切割",
                        "segments": [
                            {"id": "G01", "title": "片段 1", "source_refs": ["P001", "P002"]},
                            {"id": "G02", "title": "片段 2", "source_refs": ["P003"]},
                        ],
                    }
                },
            )
        )
    r = client.post(
        f"/api/author/projects/{pid}/source/fragments/migrate",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    migrated = r.json()["migrated"]
    assert len(migrated) == 2
    # P001+P002 -> [0, 480); P003 -> [480, 720)
    assert migrated[0]["range"] == {"start": 0, "end": 480}
    assert migrated[1]["range"] == {"start": 480, "end": 720}
    assert all(f["origin"] == "migrated" for f in migrated)
