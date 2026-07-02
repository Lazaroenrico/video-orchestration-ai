"""Helpers de perfil de voz — atribuição determinística e cláusula de gênero p/ imagem."""
from orchestrator.adapters.base import (
    VoiceProfile,
    assign_voice_profile,
    image_gender_clause,
)


def test_assign_respects_explicit_override_even_when_neutral():
    override = VoiceProfile(preset="neutral", prompt="calm narrator")
    result = assign_voice_profile("energetic woman", override, index=0)
    assert result is override


def test_assign_infers_gender_from_text():
    result = assign_voice_profile("energetic female skincare creator", None, index=1)
    assert result.preset == "female"
    assert result.prompt == "energetic female skincare creator"


def test_assign_falls_back_to_deterministic_gender_by_index_when_text_silent():
    # Sem palavra de gênero → alterna female/male por índice, nunca neutral.
    even = assign_voice_profile("friendly creator", None, index=0)
    odd = assign_voice_profile("friendly creator", None, index=1)
    assert even.preset == "female"
    assert odd.preset == "male"
    # preserva o briefing textual como prompt
    assert even.prompt == "friendly creator"


def test_assign_falls_back_by_index_when_no_text():
    assert assign_voice_profile(None, None, index=0).preset == "female"
    assert assign_voice_profile(None, None, index=3).preset == "male"


def test_assign_is_deterministic():
    a = assign_voice_profile(None, None, index=2)
    b = assign_voice_profile(None, None, index=2)
    assert a == b


def test_image_gender_clause_for_concrete_presets():
    assert "woman" in image_gender_clause(VoiceProfile(preset="female")).lower()
    assert "man" in image_gender_clause(VoiceProfile(preset="male")).lower()


def test_image_gender_clause_empty_for_neutral_and_none():
    assert image_gender_clause(VoiceProfile(preset="neutral")) == ""
    assert image_gender_clause(None) == ""


def test_image_gender_clause_stays_brand_safe():
    # Não deve conter termos sensíveis/explícitos — só descreve adulto profissional.
    for preset in ("female", "male"):
        clause = image_gender_clause(VoiceProfile(preset=preset)).lower()
        assert "adult" in clause
        for banned in ("nude", "naked", "sexy", "sexual", "explicit"):
            assert banned not in clause
