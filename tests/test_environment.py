from backend import environment


def test_default_config_matches_local_settings_file(monkeypatch):
    monkeypatch.delenv('STORYLOOM_ENV_FILE',raising=False)
    assert environment.env_file()==environment.ROOT/'.env.local'


def test_the_settings_file_supplies_the_model_endpoint(tmp_path,monkeypatch,payer):
    path=tmp_path/'.env.local'
    path.write_text('LLM_BASE_URL=https://api.example.com/v1\nLLM_API_KEY=test-key\n',encoding='utf-8')
    monkeypatch.setenv('STORYLOOM_ENV_FILE',str(path))
    monkeypatch.setenv('LLM_BASE_URL','https://stale.example/v1')
    cfg=environment.model_config()
    assert cfg['LLM_BASE_URL']=='https://api.example.com/v1'
    assert cfg['LLM_API_KEY']=='test-key'


def test_a_configured_environment_variable_is_kept_when_the_file_is_silent(tmp_path,monkeypatch,payer):
    path=tmp_path/'.env.local'
    path.write_text('LLM_API_KEY=test-key\n',encoding='utf-8')
    monkeypatch.setenv('STORYLOOM_ENV_FILE',str(path))
    monkeypatch.setenv('LLM_BASE_URL','https://chosen.example/v1')
    assert environment.model_config()['LLM_BASE_URL']=='https://chosen.example/v1'
