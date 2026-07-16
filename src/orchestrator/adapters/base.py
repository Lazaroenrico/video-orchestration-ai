"""Interfaces (Protocols) dos adapters de provedores.

No v1 só existe o ``MockAdapter``. Adapters reais (Claude, GPT Image 2, Topaz,
ElevenLabs, Replicate/fal/AtlasCloud) implementam estes mesmos protocolos e são
plugados via ``registry.py`` — sem mexer no grafo.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
    Optional,
    Protocol,
    runtime_checkable,
)

from orchestrator.graph.state import Artifact, Item, JudgeVerdict, QCResult

if TYPE_CHECKING:  # pragma: no cover - só para anotação; _agent_loop importa deste módulo
    from orchestrator.adapters._agent_loop import AgentRunResult

# Executor validado de uma typed tool, injetado pelo stage executor no agent.
# Assinatura: ``await run_tool(tool_name, **tool_inputs)`` — o agent nomeia a tool
# que quer chamar (tool-calling real, Fase 1); o stage executor valida o nome contra
# ``allowed_tools`` e injeta os inputs server-authoritative. Roda a tool tipada (com
# seus validators) — o agent nunca fala com o adapter de domínio diretamente (D29).
StageToolRunner = Callable[..., Awaitable[Any]]

# Budget default de rodadas de decisão do modelo por stage agentic. Mora aqui (e não no
# ``_agent_loop``) por ser parte da interface do ``AgentPort``: ``_agent_loop`` importa
# ``StageToolRunner`` deste módulo, então o caminho inverso fecharia um ciclo.
DEFAULT_MAX_STEPS = 4

VoicePreset = Literal["male", "female", "neutral"]

_FEMALE_HINTS = ("female", "feminina", "woman", "mulher", "girl")
_MALE_HINTS = ("male", "masculino", "masculina", "man", "homem", "boy")


@dataclass(frozen=True)
class VoiceProfile:
    """Perfil leve de voz para criação/reroll de creators."""

    preset: VoicePreset = "neutral"
    prompt: str = ""

    def __post_init__(self) -> None:
        prompt = self.prompt.strip()
        if self.preset not in ("male", "female", "neutral"):
            raise ValueError(f"unsupported voice preset: {self.preset}")
        object.__setattr__(self, "prompt", prompt)

    def as_dict(self) -> dict[str, str]:
        return {"preset": self.preset, "prompt": self.prompt}


def infer_voice_profile(text: Optional[str]) -> Optional[VoiceProfile]:
    """Infere preset de voz a partir de um briefing humano opcional."""
    prompt = (text or "").strip()
    if not prompt:
        return None
    lowered = prompt.casefold()
    if any(token in lowered for token in _FEMALE_HINTS):
        preset: VoicePreset = "female"
    elif any(token in lowered for token in _MALE_HINTS):
        preset = "male"
    else:
        preset = "neutral"
    return VoiceProfile(preset=preset, prompt=prompt)


def resolve_voice_profile(
    system_prompt: Optional[str],
    voice_profile: Optional[VoiceProfile] = None,
) -> Optional[VoiceProfile]:
    """Override explícito vence; senão, tenta inferir do texto existente."""
    if voice_profile is not None:
        return voice_profile
    return infer_voice_profile(system_prompt)


def assign_voice_profile(
    system_prompt: Optional[str],
    voice_profile: Optional[VoiceProfile] = None,
    *,
    index: int,
) -> VoiceProfile:
    """Perfil de voz **concreto** (nunca ``None``) para um creator do roster.

    Precedência: override explícito → gênero inferido do texto → gênero concreto
    determinístico por índice (alterna ``female``/``male``). Isso garante paridade
    imagem↔voz: o mesmo preset alimenta o prompt de imagem e a criação de voz, e o
    roster ganha variedade mesmo quando o briefing não cita gênero.
    """
    if voice_profile is not None:
        return voice_profile
    inferred = infer_voice_profile(system_prompt)
    if inferred is not None and inferred.preset != "neutral":
        return inferred
    prompt = inferred.prompt if inferred is not None else (system_prompt or "").strip()
    preset: VoicePreset = "female" if index % 2 == 0 else "male"
    return VoiceProfile(preset=preset, prompt=prompt)


_GENDER_CLAUSE: dict[VoicePreset, str] = {
    "female": "The creator is an adult woman.",
    "male": "The creator is an adult man.",
}


def image_gender_clause(profile: Optional[VoiceProfile]) -> str:
    """Frase brand-safe de gênero para o prompt de imagem (``""`` p/ neutral/None)."""
    if profile is None:
        return ""
    return _GENDER_CLAUSE.get(profile.preset, "")


@runtime_checkable
class LLMPort(Protocol):
    """Claude — persona, conceitos (Step 1), scripts (Step 2)."""

    async def write_persona(
        self,
        offer: str,
        brief: Optional[str] = None,
        revision: Optional[str] = None,
    ) -> str: ...

    async def generate_concepts(
        self,
        offer: str,
        n: int,
        seed: str,
        bias: Optional[list[str]] = None,
        revision: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``bias`` = hooks vencedores do ciclo anterior (Step 10 -> 1), opcional.

        ``revision`` = diretiva de refino do agent (Fase 7). ``None`` = geração
        base (comportamento inalterado); setado = incorpora a diretiva.
        """
        ...
    async def write_script(
        self,
        concept: dict[str, Any],
        creator_ref: str,
        platform: str,
        revision: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> str: ...


@runtime_checkable
class AgentPort(Protocol):
    """Execução agentic de um stage (D32/D33).

    Um adapter LLM opcionalmente implementa ``run_stage_agent`` para rodar o loop de
    tool-calling real (ReAct bounded): o modelo recebe os schemas das tools permitidas,
    escolhe quais chamar e itera até parar ou estourar o budget. Recebe ``run_tool``
    (a typed tool já validada) e só fala com o domínio através dele — nunca chama o
    adapter de domínio diretamente (D29). Adapters sem esse método caem em passthrough
    no stage executor.

    Devolve um ``AgentRunResult``: o output final **e** todas as tentativas, porque uma
    tool de mídia custa dinheiro por chamada e o node precisa contabilizar as takes
    descartadas, não só a vencedora (D33).
    """

    async def run_stage_agent(
        self,
        *,
        stage: str,
        allowed_tools: tuple[str, ...],
        run_tool: StageToolRunner,
        inputs: dict[str, Any],
        target_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tool_calls: Optional[int] = None,
    ) -> "AgentRunResult": ...


@runtime_checkable
class CreatorPort(Protocol):
    """GPT Image 2 + Topaz + ElevenLabs — creator reutilizável (Step 3)."""

    async def build_creator(
        self,
        index: int,
        system_prompt: Optional[str] = None,
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class VoicePort(Protocol):
    """Sub-adapter de voz do creator."""

    async def create_voice(
        self, index: int, voice_profile: Optional[VoiceProfile] = None
    ) -> str: ...


@runtime_checkable
class VideoPort(Protocol):
    """LTX / Kling / Seedance via plataforma de geração (Steps 4 e 5)."""

    async def generate_clip(
        self, item_id: str, tier: str, seconds: int, attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
    ) -> Artifact: ...


@runtime_checkable
class UpscalePort(Protocol):
    """Upscale do vídeo final (pós-montagem, Step 8) — sobre o entregável, não a imagem.

    Mesma assinatura dos upscalers de imagem (``upscale(url) -> url``), de propósito:
    um upscaler de vídeo real pluga aqui trocando só o nome em ``providers.yaml``.
    """

    async def upscale(self, media_uri: str) -> str: ...


@runtime_checkable
class QCPort(Protocol):
    """QC sistematizado (Step 7)."""

    async def qc_check(self, item: Item, fail_rate: float = 0.0) -> QCResult: ...


@runtime_checkable
class AssemblyPort(Protocol):
    """Montagem/edição (Step 8)."""

    async def assemble(
        self, item: Item, platform: str, system_prompt: Optional[str] = None
    ) -> Artifact: ...


@runtime_checkable
class JudgePort(Protocol):
    """LLM Judge via API Gateway (avaliação determinística do QC)."""

    def judge(self, criteria: dict[str, Any], subject: dict[str, Any]) -> JudgeVerdict: ...
