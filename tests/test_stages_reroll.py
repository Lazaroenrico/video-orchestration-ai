"""Testes diretos de ``reroll_creator_voice`` (nodes/stages.py) — a lacuna de cobertura.

Cobre os dois branches (adapter com método vs. fallback), a preservação da imagem e do
gênero (``voice_profile.preset``) e o determinismo/sensibilidade ao preset do preview.
"""
from orchestrator.adapters.base import VoiceProfile
from orchestrator.nodes.stages import reroll_creator_voice


def _creator(*, preset="male", preview=None):
    c = {
        "id": "creator-2",
        "upscaled_base": "data:image/svg+xml;base64,IMG",
        "voice_id": "voice-2",
        "voice_ref": "voice-2",
        "voice_profile": {"preset": preset, "prompt": "warm delivery"},
        "voice_reroll_count": 0,
    }
    if preview is not None:
        c["voice_preview_uri"] = preview
    return c


class _NoMethodAdapter:
    """Sem ``reroll_creator_voice`` e sem sub-adapter de voz → cai no fallback."""


class _MethodAdapter:
    def __init__(self):
        self.calls = []

    async def reroll_creator_voice(self, *, creator_id, index, reroll_count, creator, voice_profile):
        self.calls.append(
            {
                "creator_id": creator_id,
                "index": index,
                "reroll_count": reroll_count,
                "voice_profile": voice_profile,
            }
        )
        return {
            "voice_id": "new-voice",
            "voice_ref": "new-voice",
            "voice_preview_uri": "data:audio/wav;base64,NEW",
        }


async def test_reroll_fallback_suffixes_voice_and_preserves_image_and_gender(tmp_path):
    result = await reroll_creator_voice(
        _NoMethodAdapter(), _creator(preset="male"), run_id="run-x", media_root=tmp_path
    )
    assert result["voice_reroll_count"] == 1
    assert result["voice_ref"] == "voice-2::reroll-1"
    assert result["voice_id"] == "voice-2::reroll-1"
    assert result["voice"] == "voice-2::reroll-1"
    # imagem preservada
    assert result["upscaled_base"] == "data:image/svg+xml;base64,IMG"
    # gênero travado
    assert result["voice_profile"] == {"preset": "male", "prompt": "warm delivery"}
    assert result["voice_preview_uri"].startswith("data:audio/wav")


async def test_reroll_fallback_increments_across_rerolls(tmp_path):
    first = await reroll_creator_voice(
        _NoMethodAdapter(), _creator(), run_id="run-x", media_root=tmp_path
    )
    second = await reroll_creator_voice(
        _NoMethodAdapter(), first, run_id="run-x", media_root=tmp_path
    )
    assert first["voice_reroll_count"] == 1
    assert second["voice_reroll_count"] == 2
    assert second["voice_ref"] == "voice-2::reroll-1::reroll-2"


async def test_reroll_fallback_preview_is_deterministic(tmp_path):
    a = await reroll_creator_voice(
        _NoMethodAdapter(), _creator(), run_id="run-x", media_root=tmp_path
    )
    b = await reroll_creator_voice(
        _NoMethodAdapter(), _creator(), run_id="run-x", media_root=tmp_path
    )
    assert a["voice_preview_uri"] == b["voice_preview_uri"]


async def test_reroll_fallback_preview_varies_by_preset(tmp_path):
    male = await reroll_creator_voice(
        _NoMethodAdapter(), _creator(preset="male"), run_id="run-x", media_root=tmp_path
    )
    female = await reroll_creator_voice(
        _NoMethodAdapter(), _creator(preset="female"), run_id="run-x", media_root=tmp_path
    )
    # Só a amostra muda por preset; o preview reflete o gênero travado.
    assert male["voice_preview_uri"] != female["voice_preview_uri"]


async def test_reroll_uses_adapter_method_with_profile_and_locks_gender(tmp_path):
    adapter = _MethodAdapter()
    result = await reroll_creator_voice(
        adapter, _creator(preset="female"), run_id="run-x", media_root=tmp_path
    )
    # o método do adapter recebe o VoiceProfile reconstruído do creator
    assert len(adapter.calls) == 1
    passed = adapter.calls[0]["voice_profile"]
    assert isinstance(passed, VoiceProfile)
    assert passed.preset == "female"
    assert adapter.calls[0]["reroll_count"] == 1
    assert adapter.calls[0]["index"] == 2
    # resultado mesclado + contador + gênero travado
    assert result["voice_id"] == "new-voice"
    assert result["voice_reroll_count"] == 1
    assert result["voice_profile"] == {"preset": "female", "prompt": "warm delivery"}
    assert result["voice_preview_uri"] == "data:audio/wav;base64,NEW"


async def test_reroll_without_profile_still_works(tmp_path):
    creator = {
        "id": "creator-0",
        "upscaled_base": "data:image/svg+xml;base64,IMG",
        "voice_id": "voice-0",
    }
    result = await reroll_creator_voice(
        _NoMethodAdapter(), creator, run_id="run-x", media_root=tmp_path
    )
    assert result["voice_reroll_count"] == 1
    assert result["voice_ref"] == "voice-0::reroll-1"
    assert result["upscaled_base"] == "data:image/svg+xml;base64,IMG"
