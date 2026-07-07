"""Resolução de adapters a partir dos configs (papel -> adapter).

Cada **papel** da pipeline (llm, creator, video, qc, assembly) é
mapeado em ``config/providers.yaml`` para o nome de um adapter registrado aqui.
``build_adapter_from_providers`` monta um ``CompositeAdapter`` que roteia cada
método para o adapter do papel correspondente — assim dá para misturar adapters
reais e mock por papel (ex.: ``llm: anthropic`` + resto ``mock``) sem tocar o grafo.

Papéis não especificados em ``providers.yaml`` caem em ``mock`` (dry-run, custo zero).
"""
from __future__ import annotations

from typing import Any, Callable

from orchestrator.adapters.anthropic_llm import (
    build_anthropic_llm_adapter,
    build_vercel_gateway_llm_adapter,
)
from orchestrator.adapters.creator_real import (
    build_real_creator_adapter,
    build_real_creator_replicate_adapter,
    build_real_creator_vercel_adapter,
)
from orchestrator.adapters.integrity_qc import build_integrity_qc_adapter
from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters._throttle import get_replicate_throttle
from orchestrator.adapters.passthrough_upscale import build_passthrough_upscale_adapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.adapters.vercel_seedance_assembly import (
    build_vercel_seedance_assembly_adapter,
)
from orchestrator.tracing import traced

# Papéis que o grafo exerce (cada método de node mapeia para um destes).
# ``upscale`` roda pós-montagem, sobre o vídeo final (não a imagem do creator).
ROLES = ("llm", "creator", "video", "qc", "assembly", "upscale")


def _build_replicate(pipeline: dict[str, Any]) -> ReplicateVideoAdapter:
    """Fábrica do ReplicateVideoAdapter — SDK lê REPLICATE_API_TOKEN do ambiente."""
    return ReplicateVideoAdapter(
        tiers=pipeline["tiers"],
        clip=pipeline.get("clip", {}),
        throttle=get_replicate_throttle(),
        allow_mock_fallback=bool(
            pipeline.get("video", {}).get("allow_mock_fallback", True)
        ),
    )


# nome -> fábrica de adapter
_ADAPTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "mock": lambda pipeline: MockAdapter(
        tiers=pipeline["tiers"], latency=float(pipeline.get("latency", 0.0))
    ),
    "replicate": _build_replicate,
    "anthropic": build_anthropic_llm_adapter,
    "vercel_gateway_llm": build_vercel_gateway_llm_adapter,
    "creator_real": build_real_creator_adapter,
    "creator_real_vercel": build_real_creator_vercel_adapter,
    "creator_real_replicate": build_real_creator_replicate_adapter,
    "integrity_qc": build_integrity_qc_adapter,
    "vercel_seedance_assembly": build_vercel_seedance_assembly_adapter,
    "passthrough_upscale": build_passthrough_upscale_adapter,
}


def resolve_adapter(name: str, pipeline: dict[str, Any]) -> Any:
    """Instancia o adapter pelo nome (ex.: 'mock', 'anthropic')."""
    if name not in _ADAPTERS:
        raise KeyError(f"adapter desconhecido: {name!r} (registrados: {sorted(_ADAPTERS)})")
    return _ADAPTERS[name](pipeline)


def register_adapter(name: str, factory: Callable[[dict[str, Any]], Any]) -> None:
    """Registra um adapter real (chamado por quem for plugar Claude/ElevenLabs/etc.)."""
    _ADAPTERS[name] = factory


class CompositeAdapter:
    """Roteia cada método de port para o adapter do papel correspondente.

    Os nodes chamam um único objeto adapter (via ``config['configurable']['adapter']``)
    e esperam que ele implemente TODOS os ports. Este composite delega cada chamada
    para a instância configurada para aquele papel em ``providers.yaml``.
    """

    def __init__(self, by_role: dict[str, Any]) -> None:
        self._by_role = by_role

    # Ports OPCIONAIS do papel creator (reroll de voz e o sub-adapter ``voice``
    # usado nos previews): só existem quando o adapter do papel os expõe. Quem
    # chama usa ``getattr(adapter, ..., None)`` e cai no fallback quando ausente
    # (ex.: MockAdapter) — por isso delegamos via __getattr__ em vez de métodos
    # fixos, que fariam o fallback nunca disparar.
    _OPTIONAL_CREATOR_ATTRS = frozenset({"reroll_creator_voice", "voice"})

    def __getattr__(self, name: str) -> Any:
        if name in CompositeAdapter._OPTIONAL_CREATOR_ATTRS:
            value = getattr(self._by_role["creator"], name, None)
            if value is not None:
                return value
        raise AttributeError(name)

    # --- llm (Steps 1 e 2) ---
    @traced("adapter.llm.generate_concepts", run_type="chain", role="llm", step=1)
    async def generate_concepts(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["llm"].generate_concepts(*args, **kwargs)

    @traced("adapter.llm.write_script", run_type="chain", role="llm", step=2)
    async def write_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["llm"].write_script(*args, **kwargs)

    # --- creator (Step 3) ---
    @traced("adapter.creator.build_creator", run_type="chain", role="creator", step=3)
    async def build_creator(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["creator"].build_creator(*args, **kwargs)

    # --- video (Steps 4 e 5) ---
    @traced("adapter.video.generate_clip", run_type="chain", role="video", step="video")
    async def generate_clip(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["video"].generate_clip(*args, **kwargs)

    # --- qc (Step 7) ---
    @traced("adapter.qc.qc_check", run_type="chain", role="qc", step=7)
    async def qc_check(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["qc"].qc_check(*args, **kwargs)

    # --- assembly (Step 8) ---
    @traced("adapter.assembly.assemble", run_type="chain", role="assembly", step=8)
    async def assemble(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["assembly"].assemble(*args, **kwargs)

    # --- upscale (pós-montagem, Step 8) ---
    @traced("adapter.upscale.upscale", run_type="chain", role="upscale", step=8)
    async def upscale(self, *args: Any, **kwargs: Any) -> Any:
        return await self._by_role["upscale"].upscale(*args, **kwargs)


def build_adapter_from_providers(
    providers: dict[str, Any], pipeline: dict[str, Any]
) -> CompositeAdapter:
    """Monta o CompositeAdapter a partir do mapa papel->nome de ``providers.yaml``.

    Papéis ausentes caem em ``mock``. Cada nome distinto é instanciado UMA vez
    (compartilhado entre os papéis que o referenciam) — preserva o determinismo do
    mock e evita construir adapters reais (e exigir suas chaves) sem necessidade.
    """
    names = providers.get("adapters", {})
    cache: dict[str, Any] = {}
    by_role: dict[str, Any] = {}
    for role in ROLES:
        name = names.get(role, "mock")
        if name not in cache:
            cache[name] = resolve_adapter(name, pipeline)
        by_role[role] = cache[name]
    return CompositeAdapter(by_role)
