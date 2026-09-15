import os
import shutil
import time
import uuid
from pathlib import Path

TEST_DATA=Path(__file__).resolve().parents[1]/'data'/('test-'+uuid.uuid4().hex[:8])
TEST_DATA.mkdir(parents=True,exist_ok=True)
os.environ['DATABASE_URL']=f'sqlite:///{TEST_DATA / "tests.db"}'
os.environ['DATA_DIR']=str(TEST_DATA)
os.environ['STORYLOOM_ENV_FILE']=str(TEST_DATA/'.env')

import pytest
from fastapi.testclient import TestClient
from backend.app import app
from backend.db import Base,Record,Session,engine,init_db

@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture(autouse=True)
def reset_request_limits():
    """The per-IP limiter keeps module-level counters; each test starts with empty buckets."""
    from backend.public_limits import clear_request_limit_state

    clear_request_limit_state()
    yield
    clear_request_limit_state()


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    shutil.rmtree(TEST_DATA, ignore_errors=True)

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def tmp_path():
    path = TEST_DATA / 'tmp' / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def payer():
    """A bean-backed account session for the duration of one test.

    Every paid call needs a payer in every deployment, a developer's local run included, so a test
    that drives a provider adapter directly signs in the same way a visitor does.
    """
    from backend import model_access

    created = model_access.create_account_session('test-login-' + uuid.uuid4().hex[:16], 'test-account')
    with model_access.access_scope(created['access_id']), model_access.signed_in_scope('test-account'):
        yield created


@pytest.fixture
def sign_in(client):
    """Return a callable that signs this browser in, so its requests carry a payer session."""
    import backend.zhihu_oauth as zhihu_oauth
    from backend import model_access

    def sign(uid='test-account'):
        session_id = 'test-login-' + uuid.uuid4().hex[:16]
        model_access.create_account_session(session_id, uid)
        with Session.begin() as db:
            db.add(Record(id=zhihu_oauth._session_record_id(session_id), kind=zhihu_oauth.SESSION_KIND,
                          data={'uid': str(uid), 'token': 'stored-server-side', 'expires_at': time.time() + 3600}))
        client.cookies.set(zhihu_oauth.COOKIE_NAME, session_id)
        return session_id

    return sign


@pytest.fixture(scope='session')
def sample_video():
    """Five distinct one-second scenes with a real audio track, without model calls."""
    from backend.video_files import ffmpeg
    path=TEST_DATA/'media'/'fixture_av.mp4'
    colors=[('lime',1,2),('blue',2,3),('white',3,4),('black',4,5)]
    filters=','.join(f"drawbox=x=0:y=0:w=iw:h=ih:color={c}:t=fill:enable='gte(t,{a})*lt(t,{b})'" for c,a,b in colors)
    ffmpeg(['-v','error','-y','-f','lavfi','-i','color=c=red:s=64x48:r=24:d=5',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5',
            '-vf',filters,'-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2',
            '-movflags','+faststart',path])
    return path
