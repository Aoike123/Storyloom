import pytest
from dotenv import dotenv_values
from backend import local_config,image_provider
from backend.environment import DEFAULTS


def test_removed_edit_model_is_not_public_configuration(tmp_path,monkeypatch):
    assert 'IMAGE_EDIT_MODEL' not in DEFAULTS and 'IMAGE_EDIT_STEPS' not in DEFAULTS
    monkeypatch.setattr(local_config,'ROOT',tmp_path)
    (tmp_path/'.env.local').write_text('IMAGE_API_KEY=kept-key\n',encoding='utf-8')
    values={key:value for key,value in DEFAULTS.items() if not key.endswith('_API_KEY')}
    local_config.save_config(values)
    assert dotenv_values(tmp_path/'.env.local')['IMAGE_API_KEY']=='kept-key'
    with pytest.raises(ValueError):local_config.save_config({'IMAGE_EDIT_MODEL':'Qwen/Qwen-Image-Edit'})


def test_reference_composition_rejects_excess_images_before_charge(monkeypatch):
    monkeypatch.setattr(image_provider,'model_config',lambda:{'IMAGE_PROVIDER':'siliconflow'})
    monkeypatch.setattr(image_provider,'reserve_call',lambda *a,**kw:pytest.fail('Invalid reference count must not charge'))
    with pytest.raises(image_provider.ProviderError,match='1–3'):
        image_provider.generate_from_references('组合素材','test',references=['/media/one.png']*4)
