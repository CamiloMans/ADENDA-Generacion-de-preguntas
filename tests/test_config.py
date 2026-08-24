from app.core.config import Settings


def test_validation_icsara_key_prefers_dedicated_key() -> None:
    settings = Settings(
        _env_file=None,
        anthropic_adenda_validacion_icsara_api_key=" dedicated-key ",
        anthropic_api_key="legacy-key",
    )

    assert settings.anthropic_validacion_icsara_api_key == "dedicated-key"


def test_validation_icsara_key_falls_back_to_legacy_key() -> None:
    settings = Settings(
        _env_file=None,
        anthropic_adenda_validacion_icsara_api_key="",
        anthropic_api_key=" legacy-key ",
    )

    assert settings.anthropic_validacion_icsara_api_key == "legacy-key"
