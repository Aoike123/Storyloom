import os
import shutil
import uuid
from pathlib import Path

TEST_DATA = (
    Path(__file__).resolve().parents[1] / "data" / ("test-" + uuid.uuid4().hex[:8])
)
TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f'sqlite:///{TEST_DATA / "tests.db"}'
os.environ["DATA_DIR"] = str(TEST_DATA)
# Tests start from an empty settings file, so no model is "configured" until a fixture fills it in.
os.environ["STORYLOOM_ENV_FILE"] = str(TEST_DATA / ".env")

import pytest
from fastapi.testclient import TestClient
from backend.app import app
from backend.db import Base, engine, init_db


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def tmp_path():
    path = TEST_DATA / "tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def model_keys(tmp_path, monkeypatch):
    """Point the workbench at a settings file whose keys are filled in, so a test may drive a
    provider adapter. Everything is local: "configured" is the only gate, and the actual provider
    HTTP call is still mocked by the test that uses this."""
    path = tmp_path / ".env"
    path.write_text(
        "LLM_BASE_URL=https://models.test.local/v1\n"
        "LLM_API_KEY=test-llm-key\n"
        "IMAGE_ENDPOINT=https://images.test.local/v1/images/generations\n"
        "IMAGE_API_KEY=test-image-key\n"
        "VIDEO_ENDPOINT=https://video.test.local/v2/video_generation\n"
        "VIDEO_API_KEY=test-video-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYLOOM_ENV_FILE", str(path))
    yield


@pytest.fixture(scope="session")
def sample_video():
    """Five distinct one-second scenes with a real audio track, without model calls."""
    from backend.video_files import ffmpeg

    path = TEST_DATA / "media" / "fixture_av.mp4"
    colors = [("lime", 1, 2), ("blue", 2, 3), ("white", 3, 4), ("black", 4, 5)]
    filters = ",".join(
        f"drawbox=x=0:y=0:w=iw:h=ih:color={c}:t=fill:enable='gte(t,{a})*lt(t,{b})'"
        for c, a, b in colors
    )
    ffmpeg(
        [
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x48:r=24:d=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=5",
            "-vf",
            filters,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            path,
        ]
    )
    return path


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    shutil.rmtree(TEST_DATA, ignore_errors=True)
