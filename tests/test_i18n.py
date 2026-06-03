"""Tests for the minimal i18n shim."""

from __future__ import annotations

from homelabsage.i18n import STRINGS, supported_langs, translator


def test_supported_langs():
    assert "en" in supported_langs()
    assert "es" in supported_langs()


def test_translator_returns_string_for_known_key():
    t = translator("es")
    assert t("nav.dashboard") == "Panel"


def test_translator_falls_back_to_english_for_missing_translation():
    # Inject a key that exists in en only.
    STRINGS["en"]["__test_only__"] = "Test"
    try:
        t = translator("es")
        assert t("__test_only__") == "Test"
    finally:
        del STRINGS["en"]["__test_only__"]


def test_translator_returns_raw_key_when_missing_everywhere():
    t = translator("en")
    assert t("nav.totally_unknown") == "nav.totally_unknown"


def test_translator_unknown_lang_falls_back_to_english():
    t = translator("klingon")
    assert t("nav.dashboard") == "Dashboard"


def test_every_english_key_exists_in_spanish():
    """A missing Spanish key isn't an error (fallback handles it), but the
    test surfaces drift early — when an `en` key is added the Spanish
    table should be kept in sync."""
    missing = sorted(set(STRINGS["en"]) - set(STRINGS["es"]))
    assert missing == [], f"Spanish missing keys: {missing}"


def test_lang_config_rejects_unknown():
    import pytest
    from pydantic import ValidationError

    from homelabsage.config import I18nConfig
    with pytest.raises(ValidationError):
        I18nConfig(lang="klingon")


def test_lang_config_accepts_supported():
    from homelabsage.config import I18nConfig
    assert I18nConfig(lang="en").lang == "en"
    assert I18nConfig(lang="es").lang == "es"
    # Case-insensitive normalisation
    assert I18nConfig(lang="ES").lang == "es"
