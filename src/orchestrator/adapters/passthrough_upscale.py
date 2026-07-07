"""PassthroughUpscaleAdapter — upscale de vídeo no-op (placeholder do perfil live).

O upscale foi movido da imagem para o vídeo final, mas ainda não há um upscaler de
vídeo real plugado. Este adapter mantém o *stage* no grafo sem tocar o entregável:
``upscale(uri)`` devolve a mesma uri. Quando um upscaler de vídeo real existir, basta
registrá-lo e trocar o nome do papel ``upscale`` em ``config/providers.yaml`` — o grafo
não muda (mesma convenção de "plugar adapter depois" do resto do projeto).
"""
from __future__ import annotations

from typing import Any

from orchestrator.tracing import traced


class PassthroughUpscaleAdapter:
    """Implementa ``UpscalePort`` devolvendo a uri inalterada (no-op)."""

    @traced("adapter.passthrough_upscale.upscale", run_type="tool", step=8, provider="passthrough")
    async def upscale(self, media_uri: str) -> str:
        return media_uri


def build_passthrough_upscale_adapter(pipeline: dict[str, Any]) -> PassthroughUpscaleAdapter:
    return PassthroughUpscaleAdapter()
