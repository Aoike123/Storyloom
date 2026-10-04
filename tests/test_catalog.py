from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.app import app
from backend.db import DATA, Session, Record, Task
from backend import story_sources as ss


def import_story(client, title="同名故事", content="故事原文", labels=None):
    body = {"title": title, "content": content}
    if labels:
        body["labels"] = labels
    response = client.post("/api/stories/import", json=body)
    assert response.status_code == 200, response.text
    return response.json()["story"]


def release(
    rid,
    work_id,
    media="/media/catalog-test.mp4",
    created=100,
    creator=None,
    director_id=None,
):
    data = {
        "title": "成片",
        "source_title": "同名故事",
        "source_work_id": work_id,
        "entries": [
            {
                "clip_id": "clip",
                "media": media,
                "start": 0,
                "end": 4,
                "shot_id": "shot1",
            }
        ],
    }
    if director_id:
        data["director_id"] = director_id
    if creator is not None:
        data["creator"] = creator
    with Session.begin() as db:
        db.add(Record(id=rid, kind="reader_release", created=created, data=data))


def test_catalog_lists_imported_stories_and_published_releases(client):
    (DATA / "media" / "catalog-test.mp4").write_bytes(b"test-video")
    story = import_story(client)
    sid = story["id"]
    release("latest", sid)
    import_story(client, title="另一篇", content="另一段正文")
    data = client.get("/api/reader/catalog").json()
    assert data["count"] == 2 and data["upstream_total"] == 2
    assert data["ready_count"] == 1
    published = next(item for item in data["items"] if item["work_id"] == sid)
    assert published["release"]["id"] == "latest"
    assert published["release"]["project_id"] is None
    assert published["release"]["mine"] is False
    unreleased = next(item for item in data["items"] if item["work_id"] != sid)
    assert unreleased["release"] is None


def test_open_story_seeds_a_project_visible_to_everyone(client):
    story = import_story(client)
    sid = story["id"]
    opened = client.post(f"/api/author/stories/{sid}/open", json={})
    assert opened.status_code == 200, opened.text
    work = opened.json()
    assert work["source"]["content"] == story["content"]
    assert work["stage"] == "style"
    assert work["source_work_id"] == sid
    # A single local maker: everyone sees the same single project.
    other = TestClient(app)
    assert [item["id"] for item in other.get("/api/author/projects").json()] == [
        work["id"]
    ]
    assert other.get("/api/author/projects/" + work["id"]).status_code == 200
    # Reopening the same story resumes the same project without reimporting its source.
    resumed = client.post(f"/api/author/stories/{sid}/open", json={}).json()
    assert resumed["id"] == work["id"]
    with Session() as db:
        assert (
            len(list(db.scalars(select(Record).where(Record.kind == "author_project"))))
            == 1
        )
        assert not list(db.scalars(select(Task)))


def test_only_imported_stories_can_open(client):
    story = import_story(client)
    assert (
        client.post("/api/author/stories/nonexistent/open", json={}).status_code == 404
    )
    assert (
        client.post(f'/api/author/stories/{story["id"]}/open', json={}).status_code
        == 200
    )


def test_release_requires_safe_complete_media_entries(client):
    story = import_story(client)
    (DATA / "outside-catalog.mp4").write_bytes(b"outside-media-root")
    release("escape", story["id"], media="/media/../outside-catalog.mp4")
    release("remote", story["id"], media="https://example.com/clip.mp4")
    assert client.get("/api/reader/catalog").json()["ready_count"] == 0


def test_catalog_creator_is_sanitized_to_public_fields(client):
    story = import_story(client)
    (DATA / "media" / "catalog-test.mp4").write_bytes(b"test-video")
    release(
        "creator-release",
        story["id"],
        creator={
            "name": "版本制作者",
            "avatar_path": "javascript:alert(1)",
            "headline": "不应进入",
        },
    )
    item = next(
        i
        for i in client.get("/api/reader/catalog").json()["items"]
        if i["work_id"] == story["id"]
    )
    assert item["release"]["creator"] == {"name": "版本制作者", "avatar_path": None}
