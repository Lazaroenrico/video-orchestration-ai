"""ReplicateUpscaleAdapter — upscale de imagem via SDK oficial ``replicate``.

Usa ``replicate.async_run(ref, input=...)``, que resolve a versão do modelo, faz
o polling da prediction assíncrona e devolve o output já pronto (um ``FileOutput``
URL-like). Isso evita o contrato HTTP manual (campo ``version`` + header
``Prefer: wait`` + polling) que causava ``422``/``output: null``.

Modelo padrão: ``nightmareai/real-esrgan`` — input ``image`` (URL ou data URI
base64) + ``scale``. ``upscale`` retorna a URL da imagem upscalada como string.

Nota: modelos *community* (não-oficiais) exigem o **version hash** pinado no ref
(``owner/name:version``) — sem versão, o SDK usa o endpoint de official models e
retorna 404. O hash abaixo é a versão corrente; sobrescreva via ``model=`` se mudar.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import replicate

_DEFAULT_MODEL = (
    "nightmareai/real-esrgan:"
    "b3ef194191d13140337468c916c2c5b96dd0cb06dffc032a022a31807f6a5ea8"
)

# Assinatura do runner: (ref, input=...) -> output (FileOutput/URL/str)
Runner = Callable[..., Awaitable[Any]]


class ReplicateUpscaleAdapter:
    """Faz upscale de imagem via Replicate.

    Parameters
    ----------
    model:
        Ref do modelo Replicate (``owner/name`` ou ``owner/name:version``).
    scale:
        Fator de upscale (default 4).
    runner:
        Async callable ``(ref, input=...) -> output`` injetável para testes.
        Default: ``replicate.async_run`` (lê ``REPLICATE_API_TOKEN`` do ambiente).
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        scale: int = 4,
        runner: Optional[Runner] = None,
    ) -> None:
        self.model = model
        self.scale = scale
        self._runner: Runner = runner or replicate.async_run

    async def upscale(self, image_url: str) -> str:
        """Faz upscale da imagem. Retorna a URL da imagem upscalada (string)."""
        output = await self._runner(
            self.model,
            input={"image": image_url, "scale": self.scale},
        )
        return str(output)
