"""Cutting a demo bundle out of a finished work and handing it to real accounts."""
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import model_access
from backend.app import app
from backend.db import DATA, Record, Session, Task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tool():
    """The command-line tool as a module, so its steps can be called directly in a test."""
    spec = importlib.util.spec_from_file_location('demo_data', ROOT / 'scripts' / 'demo-data.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finished_film(db, sample_video):
    """One published episode: the smallest work the tool is expected to carry."""
    import shutil
    from PIL import Image
    from backend.video_storage import attach_artifact, register_artifact

    shutil.copyfile(sample_video, DATA / 'media' / 'demo.mp4')
    Image.new('RGB', (64, 64), 'white').save(DATA / 'media' / 'demo.png')
    db.add(Record(id='source_demo', kind='story_source', data={
        'title': '蓝血', 'work_id': '2025684191967294692', 'author_name': '桃花先生',
        'labels': ['脑洞'], 'content': '原文'}))
    db.add(Record(id='director_demo', kind='director', version=3, data={
        'source_id': 'source_demo', 'status': 'approved', 'treatment': {'premise': '一个脑洞'},
        'segments': {'segments': [{'id': 'G01'}]},
        'board': {'title': '短场景', 'shots': [
            {'id': 'G01-S01', 'segment_id': 'G01', 'motion_prompt': '单镜动作描述',
             'generation_seconds': 5, 'edit_seconds': 3}]}}))
    artifact = register_artifact(db, 'clip_demo', DATA / 'media' / 'demo.mp4',
                                 {'origin': 'generation', 'task_id': 'video_demo'})
    clip = Record(id='clip_demo', kind='clip', data={'status': 'approved',
        'visual_reviewed': True, 'reader_trim': {'start': 0.0, 'end': 3.0}})
    attach_artifact(clip, artifact)
    db.add(clip)
    media = artifact.data['playback']['media']
    db.add(Task(id='video_demo', kind='video', status='completed',
                payload={'director_id': 'director_demo', 'director_version': 3, 'shot_id': 'G01-S01'},
                result={'clip_id': 'clip_demo', 'media': media, 'provider_id': 'provider-demo'}))
    db.add(Task(id='rendering_demo', kind='author_render', status='completed',
                message='本集片段已完成', payload={'phase': 'rendering', 'work_id': 'work_demo',
                'run_id': 'attempt-demo', 'production_phase': 'rendering'}))
    # The studio matches a shot's clip to the reviewed visual version, so the demo needs that
    # binding and the same consistency stamp the video task was submitted with.
    reference = Record(id='asset_demo', kind='asset', data={
        'name': '方诺', 'type': 'character', 'status': 'approved', 'media': '/media/demo.png'})
    db.add(reference)
    visual = Record(id='visual_director_demo', kind='visual_config', data={
        'approved': True, 'director_version': 3, 'bindings': {'G01-S01': ['asset_demo']},
        'asset_versions': {'asset_demo': reference.version}, 'style': '写实影视摄影'})
    db.add(visual)
    db.flush()
    from backend.consistency import stamp as visual_stamp
    token = visual_stamp(db, visual)
    db.get(Task, 'video_demo').payload = {**db.get(Task, 'video_demo').payload,
                                          'consistency_stamp': token}
    db.add(Record(id='work_demo', kind='author_project', data={
        'title': '蓝血', 'source_id': 'source_demo', 'zhihu_work_id': '2025684191967294692',
        'director_id': 'director_demo', 'stage': 'published', 'run_id': 'attempt-demo',
        'release_id': 'release_demo', 'production_nodes': {'rendering': 'rendering_demo'},
        'attempt_history': [{'stale': True}]}))
    db.add(Record(id='creative_director_demo', kind='creative_run', data={'stage': 'published'}))
    db.add(Record(id='release_demo', kind='reader_release', data={
        'schema_version': 1, 'status': 'ready', 'title': '蓝血：嵌套现实', 'author': '桃花先生',
        'source_title': '蓝血', 'source_id': 'source_demo', 'director_id': 'director_demo',
        'description': '一个脑洞', 'published_units': ['G01'], 'total_units': 1,
        'units_complete': True, 'entries': [{'media': media, 'start': 0.0, 'end': 3.0,
        'shot_id': 'G01-S01'}]}))
    return media


def export_to(tmp_path, tool, **overrides):
    options = {'out': str(tmp_path / 'bundle'), 'director': 'director_demo', 'force': False}
    options.update(overrides)
    tool.export(type('Args', (), options))
    return tmp_path / 'bundle'


def wipe_deployment(bundle):
    """Empty a deployment the way a production reset does: records and the demo's own media.

    Only the files this bundle carries are removed, so a test can prove the bundle is self-contained
    without taking the session's shared fixtures with it.
    """
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    with Session.begin() as db:
        for row in db.query(Record).all():
            db.delete(row)
        for row in db.query(Task).all():
            db.delete(row)
    for name in manifest['media']:
        (DATA / 'media' / name).unlink(missing_ok=True)


def test_a_bundle_carries_the_film_and_none_of_the_debug_history(client, sample_video, tmp_path, tool):
    """演示包只带成片链路：失败与废弃的尝试、账目和审计不进入演示数据。"""
    with Session.begin() as db:
        media = finished_film(db, sample_video)
        # Debris the tool must leave behind: a failed attempt, a superseded clip and an audit row.
        db.add(Task(id='video_failed', kind='video', status='needs_review',
                    payload={'director_id': 'director_demo', 'shot_id': 'G01-S02'},
                    message='视频接口返回 HTTP 402'))
        db.add(Record(id='audit_x', kind='audit', data={'action': 'noise'}))
        db.add(Record(id='usage_x', kind='usage', data={'provider': 'video'}))
    bundle = export_to(tmp_path, tool)

    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    kinds = {item['kind'] for item in manifest['records']}
    task_ids = {item['id'] for item in manifest['tasks']}
    assert kinds == {'story_source', 'director', 'clip', 'clip_artifact', 'asset',
                     'visual_config', 'author_project', 'creative_run', 'reader_release'}
    assert task_ids == {'video_demo', 'rendering_demo'}
    assert not (bundle / 'media' / 'media').exists()

    # The studio needs its coordinator on the work row; nothing may point at a task left behind.
    work = next(item for item in manifest['records'] if item['kind'] == 'author_project')
    assert work['data']['production_nodes'] == {'rendering': 'rendering_demo'}
    assert 'attempt_history' not in work['data']
    assert (bundle / 'media' / media.removeprefix('/media/')).is_file()


def test_an_imported_demo_plays_for_a_signed_in_account(client, sample_video, tmp_path, tool,
                                                        sign_in, monkeypatch):
    """导入后，登录账号在工作室能看到这部演示片，读者目录能直接播。"""
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    with Session.begin() as db:
        finished_film(db, sample_video)
    bundle = export_to(tmp_path, tool)

    wipe_deployment(bundle)

    # The account signs in first, which is the order the operator will use: create the account, sign
    # in once, then load the demo. Importing on its own leaves the work unassigned and invisible.
    sign_in('969570047710216200')
    load = type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                             'creator_name': None, 'creator_avatar': None})
    tool.import_bundle(load)

    works = client.get('/api/author/projects').json()
    assert [work['id'] for work in works] == ['work_zhihu_' + __import__('hashlib').sha256(
        b'2025684191967294692|account:969570047710216200').hexdigest()[:32]]
    studio = client.get('/api/author/projects/' + works[0]['id']).json()
    assert studio['stage'] == 'published'
    assert studio['release_id'] == 'release_demo'
    assert [(episode['segment_id'], episode['clips']) for episode in studio['episodes']] \
        == [('G01', 1)]
    assert any(item['kind'] == 'video' for item in studio['outputs'])

    catalog = client.get('/api/reader/catalog').json()
    release = next(item['release'] for item in catalog['items'] if item.get('release'))
    assert release['id'] == 'release_demo'
    assert release['entries'][0]['start'] == 0.0 and release['entries'][0]['end'] == 3.0
    assert client.get('/api/reader/releases/release_demo/manifest').status_code == 200


def test_importing_twice_changes_nothing(client, sample_video, tmp_path, tool):
    """重复导入是安全的：记录已存在就跳过，媒体只补齐缺失的部分。"""
    with Session.begin() as db:
        finished_film(db, sample_video)
    bundle = export_to(tmp_path, tool)
    load = type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                             'creator_name': None, 'creator_avatar': None})
    tool.import_bundle(load)
    first_release = None
    with Session() as db:
        first_release = db.get(Record, 'release_demo').data
    tool.import_bundle(load)
    with Session() as db:
        assert db.get(Record, 'release_demo').data == first_release
        assert db.get(Task, 'video_demo').status == 'completed'
        assert len(list(db.query(Record).filter(Record.kind == 'author_project').all())) == 1


def test_handing_the_demo_to_accounts_does_not_need_the_media_again(client, sample_video, tmp_path,
                                                                    tool, monkeypatch):
    """后生成的账号也能拿到演示片：assign 只补归属，不重抄 114MB 媒体。"""
    import hashlib
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    with Session.begin() as db:
        finished_film(db, sample_video)
    bundle = export_to(tmp_path, tool)
    wipe_deployment(bundle)
    tool.import_bundle(type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                                         'creator_name': None, 'creator_avatar': None}))

    # The account signs in after the import, which is what the operator will do by hand.
    import backend.zhihu_oauth as zhihu_oauth
    session_id = 'demo-login-0123456789abcdef'
    model_access.create_account_session(session_id, '42')
    with Session.begin() as db:
        db.add(Record(id=zhihu_oauth._session_record_id(session_id), kind=zhihu_oauth.SESSION_KIND,
                      data={'uid': '42', 'token': 'stored-server-side', 'fullname': '演示账号',
                            'avatar_path': 'https://picx.zhimg.com/avatar.jpg',
                            'expires_at': time.time() + 3600}))
    client.cookies.set(zhihu_oauth.COOKIE_NAME, session_id)
    assert client.get('/api/author/projects').json() == []

    tool.assign_only(type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                                       'creator_name': None, 'creator_avatar': None}))
    works = client.get('/api/author/projects').json()
    assert len(works) == 1
    assert works[0]['id'] == 'work_zhihu_' + hashlib.sha256(
        b'2025684191967294692|account:42').hexdigest()[:32]
    # The single account present signs the release, so the market card carries a real name.
    release = db_release()
    assert release['creator']['name'] == '演示账号'
    assert release['creator']['avatar_path'] == 'https://picx.zhimg.com/avatar.jpg'
    # Re-running later, after another signup, adds the new account and leaves this one alone.
    with Session.begin() as db:
        db.add(Record(id='zhihu_login_second', kind=zhihu_oauth.SESSION_KIND,
                      data={'uid': '77', 'token': 'stored-server-side', 'fullname': '第二位',
                            'expires_at': time.time() + 3600}))
    tool.assign_only(type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                                       'creator_name': None, 'creator_avatar': None}))
    with Session() as db:
        owners = {row.data.get('owner') for row in
                  db.query(Record).filter(Record.kind == 'author_project').all()}
    assert {'account:42', 'account:77'} <= owners


def test_the_tool_says_so_when_nobody_can_see_the_demo(client, sample_video, tmp_path, tool, capsys):
    """还没有账号时导入不算失败，但必须说清楚现在没人能看到这部演示片。"""
    with Session.begin() as db:
        finished_film(db, sample_video)
    bundle = export_to(tmp_path, tool)
    tool.import_bundle(type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                                         'creator_name': None, 'creator_avatar': None}))
    printed = capsys.readouterr().out
    assert 'the demo belongs to nobody' in printed
    assert 'assign <bundle>' in printed


def test_an_explicit_owner_uid_assigns_exactly_that_account(client, sample_video, tmp_path, tool):
    """--owner-uid 只给指定账号，便于把演示片交到某一个真实账号名下。"""
    with Session.begin() as db:
        finished_film(db, sample_video)
    bundle = export_to(tmp_path, tool)
    tool.import_bundle(type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': None,
                                         'creator_name': None, 'creator_avatar': None}))
    tool.assign_only(type('Args', (), {'folder': str(bundle), 'owner': None, 'owner_uid': '5150',
                                       'creator_name': '演示创作者',
                                       'creator_avatar': 'https://picx.zhimg.com/a.jpg'}))
    with Session() as db:
        owned = [row.data.get('owner') for row in
                 db.query(Record).filter(Record.kind == 'author_project').all()]
    assert 'account:5150' in owned
    release = db_release()
    assert release['creator'] == {'name': '演示创作者',
                                  'avatar_path': 'https://picx.zhimg.com/a.jpg'}


def db_release():
    with Session() as db:
        return db.get(Record, 'release_demo').data
