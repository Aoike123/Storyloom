import pytest
from dotenv import dotenv_values
from backend import local_config


def test_save_preserves_other_settings_and_blank_key(tmp_path,monkeypatch):
    target=tmp_path/'.env.local'
    monkeypatch.setattr(local_config,'env_file',lambda:target)
    target.write_text('DATABASE_URL=sqlite:///keep.db\nLLM_API_KEY=existing-secret\n',encoding='utf-8')
    local_config.save_config({'LLM_API_KEY':'','VIDEO_API_KEY':"test-$'secret",'VIDEO_MODEL':'MiniMax-H3-Max'})
    result=dotenv_values(target)
    assert result['DATABASE_URL']=='sqlite:///keep.db'
    assert result['LLM_API_KEY']=='existing-secret'
    assert result['VIDEO_API_KEY']=="test-$'secret"
    assert not list(tmp_path.glob('.config-*'))


def test_rejects_env_injection_without_writing(tmp_path,monkeypatch):
    target=tmp_path/'.env.local'
    monkeypatch.setattr(local_config,'env_file',lambda:target)
    with pytest.raises(ValueError):
        local_config.save_config({'LLM_API_KEY':'secret\nALLOW_PAID_CALLS=true'})
    assert not target.exists()


def test_settings_response_never_contains_keys(client,monkeypatch):
    monkeypatch.setattr(local_config,'save_config',lambda values:None)
    data=client.get('/api/settings').json()
    assert not any('KEY' in k for k in data['editable'])


def test_cross_origin_settings_write_rejected(client):
    r=client.post('/api/settings',headers={'Origin':'https://example.com'},json={'values':{'LLM_API_KEY':'test'}})
    assert r.status_code==403
