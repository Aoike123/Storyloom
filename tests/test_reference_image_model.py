import pytest
from backend import image_provider
from backend.environment import DEFAULTS


def test_removed_edit_model_is_not_public_configuration():
    # The image-edit model was removed; it must not reappear as a configurable default.
    assert "IMAGE_EDIT_MODEL" not in DEFAULTS and "IMAGE_EDIT_STEPS" not in DEFAULTS


def test_reference_composition_rejects_excess_images_before_charge(monkeypatch):
    monkeypatch.setattr(
        image_provider, "model_config", lambda: {"IMAGE_PROVIDER": "siliconflow"}
    )
    monkeypatch.setattr(
        image_provider,
        "reserve_call",
        lambda *a, **kw: pytest.fail("Invalid reference count must not charge"),
    )
    with pytest.raises(image_provider.ProviderError, match="1–3"):
        image_provider.generate_from_references(
            "组合素材", "test", references=["/media/one.png"] * 4
        )
