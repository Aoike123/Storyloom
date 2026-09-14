import httpx
import pytest
from backend import providers as p


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(p,'settings',lambda: {})
    monkeypatch.setattr(p,'reserve_call',lambda *args: None)
    monkeypatch.setenv('VIDEO_PROVIDER','minimax')
    monkeypatch.setenv('VIDEO_MODEL','MiniMax-H3-Max')
    monkeypatch.setenv('VIDEO_ENDPOINT','https://api.minimax.io/v2/video_generation')
    monkeypatch.setenv('VIDEO_DURATION','8')
    monkeypatch.setenv('VIDEO_RESOLUTION','768P')


def test_minimax_submission_and_query(config,monkeypatch):
    def post(url,**kwargs):
        assert url=='https://api.minimax.io/v2/video_generation'
        body=kwargs['json']
        assert body['model']=='MiniMax-H3-Max'
        assert body['duration']==8 and body['resolution']=='768P'
        assert body['content'][1]['role']=='reference_image'
        return httpx.Response(200,json={'task_id':'123'})
    def get(url,**kwargs):
        assert url=='https://api.minimax.io/v2/query/video_generation/123'
        return httpx.Response(200,json={'task':{'status':'succeeded','content':{'url':'https://example.com/video.mp4'}}})
    monkeypatch.setattr(p.httpx,'post',post)
    monkeypatch.setattr(p.httpx,'get',get)
    assert p.submit_video('人物走入房间','test','https://example.com/frame.png')=='123'
    assert p.poll_video('123')==('succeeded','https://example.com/video.mp4')


def test_unsupported_max_resolution_does_not_reserve_budget(config,monkeypatch):
    monkeypatch.setenv('VIDEO_RESOLUTION','2K')
    def unexpected(*args): pytest.fail('Invalid request must not reserve budget')
    monkeypatch.setattr(p,'reserve_call',unexpected)
    with pytest.raises(p.ProviderError,match='分辨率'): p.submit_video('test','test')


def test_deepseek_rejects_truncated_json(config,monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER','deepseek')
    monkeypatch.setenv('LLM_BASE_URL','https://api.deepseek.com')
    monkeypatch.setenv('LLM_MODEL','deepseek-chat')
    def post(url,**kwargs):
        assert kwargs['json']['thinking']=={'type':'disabled'}
        return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{}'}}]})
    monkeypatch.setattr(p.httpx,'post',post)
    with pytest.raises(p.ProviderError,match='未正常完成'): p.chat_json('JSON',{},'test')


def test_poll_rejects_path_injection(config):
    with pytest.raises(p.ProviderError,match='编号'): p.poll_video('../other')
