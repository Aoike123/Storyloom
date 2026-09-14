from backend import environment


def test_default_config_matches_local_settings_file(monkeypatch):
    monkeypatch.delenv('STORYLOOM_ENV_FILE',raising=False)
    assert environment.env_file()==environment.ROOT/'.env.local'


def test_legacy_endpoint_preserves_provider_on_upgrade(tmp_path,monkeypatch):
    path=tmp_path/'.env.local'
    path.write_text('LLM_ENDPOINT=https://legacy.example/v1/chat/completions\nLLM_API_KEY=test-key\n',encoding='utf-8')
    monkeypatch.setenv('STORYLOOM_ENV_FILE',str(path))
    monkeypatch.setenv('LLM_BASE_URL','https://stale.example/v1')
    cfg=environment.model_config()
    assert cfg['LLM_BASE_URL']=='https://legacy.example/v1'
    assert cfg['LLM_API_KEY']=='test-key'


def test_explicit_base_url_takes_precedence(tmp_path,monkeypatch):
    path=tmp_path/'.env.local'
    path.write_text('LLM_ENDPOINT=https://legacy.example/chat/completions\nLLM_BASE_URL=https://chosen.example/v1\n',encoding='utf-8')
    monkeypatch.setenv('STORYLOOM_ENV_FILE',str(path))
    assert environment.model_config()['LLM_BASE_URL']=='https://chosen.example/v1'
