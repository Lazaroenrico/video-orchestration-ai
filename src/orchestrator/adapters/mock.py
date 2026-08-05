"""MockAdapter — implementação dry-run de todos os ports.

Saídas **determinísticas** (derivadas de hash dos inputs, sem ``random``) para que
toda a pipeline rode ponta a ponta sem rede e os testes sejam reproduzíveis.
Custo por tier segue o Context.md (LTX $0.01/s, Kling $0.10/s, Seedance $0.168/s).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any, Optional

from orchestrator.adapters._agent_loop import (
    DEFAULT_MAX_STEPS,
    AgentRunResult,
    ToolAttempt,
    ToolCall,
    run_agent_loop,
)
from orchestrator.adapters.base import StageToolRunner, VoiceProfile, resolve_voice_profile
from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    FinalizedVoice,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact, Item, QCResult
from orchestrator.tools.registry import tool_call_schemas
from orchestrator.tracing import add_trace_metadata, traced

_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_QC_SUSPECTS = ["hands", "eyes", "lip_sync", "lighting", "skin_texture"]


def _unit(*parts: Any) -> float:
    """Hash determinístico dos inputs -> float uniforme em [0, 1)."""
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:12], 16) / float(1 << 48)


def _digest_bytes(*parts: Any) -> bytes:
    key = "|".join(str(p) for p in parts)
    return hashlib.sha256(key.encode()).digest()


def _svg_data_uri(label: str, *seed_parts: Any) -> str:
    """SVG minúsculo e determinístico, renderável como imagem (sem rede/disco).

    A cor é derivada de hash dos ``seed_parts`` e o texto do rótulo torna o
    payload legível/único por creator, mantendo tudo pequeno (poucas centenas
    de bytes) — importante porque estes data URIs trafegam pelo buffer de SSE.
    """
    color = "#%06x" % (int(_unit(*seed_parts) * 0xFFFFFF))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        f'<rect width="64" height="64" fill="{color}"/>'
        f'<text x="32" y="36" font-size="9" text-anchor="middle" fill="#fff">{label}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _wav_data_uri(*seed_parts: Any) -> str:
    """WAV PCM 8-bit mono minúsculo (~0.1s) e determinístico.

    Cabeçalho RIFF/WAVE válido + amostras derivadas de hash (sem ``random``).
    Curto o bastante para caber no buffer de replay do SSE.
    """
    sample_rate = 4000
    n_samples = 400  # ~0.1s @ 4kHz
    digest = _digest_bytes("voice-preview", *seed_parts)
    samples = bytes(digest[i % len(digest)] for i in range(n_samples))
    data_size = len(samples)
    byte_rate = sample_rate  # mono, 8 bits/sample
    header = (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # PCM
        + (1).to_bytes(2, "little")  # mono
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # block align
        + (8).to_bytes(2, "little")  # bits per sample
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    return "data:audio/wav;base64," + base64.b64encode(header + samples).decode()


# mp4 H.264 minúsculo, VÁLIDO e REPRODUZÍVEL (1 frame azul 16x16, faststart:
# moov antes do mdat) — 932 bytes. Gerado offline uma vez; embutido como
# constante para que a UI toque o vídeo no demo sem rede/disco/custo.
_MP4_PLAYABLE_B64 = (
    "AAAAIGZ0eXBtcDQyAAAAAG1wNDJtcDQxaXNvbWlzbzIAAANHbW9vdgAAAGxtdmhkAAAAAOZrEP3maxD9"
    "AAAMgAAADIAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAApZ0cmFrAAAAXHRraGQAAAAH5msQ/eZrEP0AAAAB"
    "AAAAAAAADIAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAA"
    "ABAAAAAQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAyAAAAAAAABAAAAAAHRbWRpYQAAACBtZGhk"
    "AAAAAOZrEP3maxD9AAAAZAAAAGRVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRl"
    "b0hhbmRsZXIAAAABfG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAA"
    "AQAAAAx1cmwgAAAAAQAAATxzdGJsAAAAwHN0c2QAAAAAAAAAAQAAALBhdmMxAAAAAAAAAAEAAAAAAAAA"
    "AAAAAAAAAAAAABAAEABIAAAASAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "GP//AAAAI2F2Y0MBQtAL/+EADGdC0AuMjU5APCIRqAEABGjOPIAAAAAUYnRydAAAAAAAAAAAAAABqAAA"
    "ABNjb2xybmNseAAGAAYABgAAAAAQcGFzcAAAAAEAAAABAAAAGHN0dHMAAAAAAAAAAQAAAAEAAABkAAAA"
    "FHN0c3MAAAAAAAAAAQAAAAEAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAAGHN0c3oAAAAAAAAA"
    "AAAAAAEAAAA1AAAAFHN0Y28AAAAAAAAAAQAAA28AAAA9dWR0YQAAADVtZXRhAAAAAAAAACFoZGxyAAAA"
    "AG1obHJtZGlyAAAAAAAAAAAAAAAAAAAAAAhpbHN0AAAAPXVkdGEAAAA1bWV0YQAAAAAAAAAhaGRscgAA"
    "AABtaGxybWRpcgAAAAAAAAAAAAAAAAAAAAAIaWxzdAAAAD1tZGF0AAAADGdC0AuMjU5APCIRqAAAAARo"
    "zjyAAAAAGWW4AAQAAAn///giigACBr44AAhfRwABADw="
)


def _mp4_data_uri(*seed_parts: Any) -> str:
    """Vídeo REPRODUZÍVEL e determinístico (``data:video/mp4;base64,...``).

    Retorna um mp4 H.264 minúsculo, válido e tocável (constante compartilhada)
    para que o player da UI funcione no demo offline. Um ``#fragment`` derivado
    de hash dos ``seed_parts`` é anexado: o navegador o descarta ao decodificar
    (o mp4 tocado é idêntico), mas a string da URI varia por item/prompt —
    preservando o contrato de que outputs diferentes têm URIs diferentes, sem
    quebrar a reprodução nem a classificação ``data:video/mp4``.
    """
    tag = hashlib.sha256("|".join(str(p) for p in seed_parts).encode()).hexdigest()[:8]
    return "data:video/mp4;base64," + _MP4_PLAYABLE_B64 + "#" + tag


class _MockAgentBrain:
    """Brain determinístico para o loop de tool-calling do mock (offline, custo zero).

    Sem rede: decide as tool calls contando os resultados já vistos nas mensagens.
    0 resultados → chama a tool primária para o rascunho inicial. 1 resultado →
    ``critique`` (heurístico por hash) decide refinar (chama de novo com ``revision``)
    ou aprovar (para). >=2 resultados → para. Bounded a no máximo 2 execuções de tool.
    """

    def __init__(self, critique, system_prompt: Optional[str] = None) -> None:
        self._critique = critique
        self._system_prompt = system_prompt

    def initial_messages(
        self, stage: str, inputs: dict[str, Any], tool_schemas: list[dict[str, Any]]
    ) -> list[Any]:
        primary = tool_schemas[0]["name"] if tool_schemas else ""
        message = {"role": "user", "stage": stage, "primary_tool": primary}
        if self._system_prompt:
            message["system_prompt"] = self._system_prompt
        return [message]

    async def complete(
        self, messages: list[Any], tool_schemas: list[dict[str, Any]]
    ) -> tuple[Any, list[ToolCall]]:
        stage = messages[0].get("stage", "")
        primary = messages[0].get("primary_tool", "")
        results = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if not results:
            return {"role": "assistant"}, [ToolCall(id="mock-draft", name=primary)]
        if len(results) == 1:
            revision = self._critique(stage, results[0].get("result"))
            if revision:
                return {"role": "assistant"}, [
                    ToolCall(id="mock-refine", name=primary, arguments={"revision": revision})
                ]
        return {"role": "assistant"}, []

    def tool_result_message(self, call: ToolCall, result: Any) -> Any:
        return {"role": "tool", "name": call.name, "result": result}


def _terminal_submission(stage: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic creative-v2 tool arguments for offline agent runs."""
    campaign = inputs.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    if stage == "concepts":
        count = int(inputs.get("n") or campaign.get("batch_size") or 1)
        offer = str(campaign.get("offer") or inputs.get("offer") or "the offer")
        audience = str(campaign.get("audience") or "the audience")
        return {
            "proposals": [
                {
                    "hook": f"{offer}: angle {index + 1} for {audience}",
                    "angle": f"Deterministic test angle {index + 1}",
                    "audience_problem": f"A relevant problem for {audience}",
                    "product_mechanism": f"The supplied mechanism for {offer}",
                    "evidence_basis": "cold_test",
                    "format": "direct-to-camera",
                    "hook_style": _HOOK_STYLES[index % len(_HOOK_STYLES)],
                }
                for index in range(count)
            ]
        }
    if stage == "scripts":
        concept = inputs.get("concept")
        concept = concept if isinstance(concept, dict) else {}
        hook = str(concept.get("hook") or "I changed one part of my routine.")
        return {
            "draft": {
                "spoken_beats": [
                    {"section": "hook", "text": hook, "seconds": 3},
                    {
                        "section": "body",
                        "text": "Here is how the approved product fits the routine.",
                        "seconds": 8,
                    },
                    {"section": "cta", "text": "See the approved offer.", "seconds": 3},
                ],
                "visual_beats": [
                    "Creator addresses camera",
                    "Approved product demonstration",
                ],
                "on_screen_text": [hook[:80]],
                "call_to_action": "See the approved offer.",
                "estimated_duration": 14,
            }
        }
    if stage == "creator_profiles":
        concept_ids = [str(value) for value in inputs.get("concept_ids") or []]
        return {
            "creators": [
                {
                    "archetype": "Warm routine guide",
                    "visual_brief": "Adult creator in a bright, realistic home setting.",
                    "voice_brief": "Warm, clear, conversational delivery.",
                    "performance_style": "Calm, practical, and credible.",
                    "exclusions": ["medical authority", "guaranteed outcomes"],
                },
                {
                    "archetype": "Direct product tester",
                    "visual_brief": "Adult creator at a clean vanity with the real product.",
                    "voice_brief": "Direct, energetic, natural delivery.",
                    "performance_style": "Concise demonstration with visible product handling.",
                    "exclusions": ["medical authority", "guaranteed outcomes"],
                },
            ],
            "assignments": [
                {"concept_id": concept_id, "creator_index": index % 2}
                for index, concept_id in enumerate(concept_ids)
            ],
        }
    raise ValueError(f"unsupported terminal mock stage: {stage}")


class MockAdapter:
    """Serve aos papéis mock (llm/image/voice/video/assembly) no v1."""

    def __init__(self, tiers: list[dict[str, Any]], latency: float = 0.0) -> None:
        self.tiers = {t["name"]: t for t in tiers}
        self.latency = latency
        self._semaphores = {
            name: asyncio.Semaphore(int(t.get("max_concurrency", 8)))
            for name, t in self.tiers.items()
        }

    async def _tick(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)

    # --- Step 0: persona ---
    @traced("adapter.mock.write_persona", run_type="chain", step=0, provider="mock")
    async def write_persona(
        self,
        offer: str,
        brief: Optional[str] = None,
        revision: Optional[str] = None,
    ) -> str:
        await self._tick()
        parts: tuple[Any, ...] = ("persona", offer, brief or "")
        if revision:
            parts = (*parts, f"rev:{revision}")
        tag = hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:8]
        audience = ["busy moms", "skincare beginners", "performance buyers"][
            int(_unit(*parts, "audience") * 3)
        ]
        tone = ["warm expert", "direct friend", "calm skeptic"][
            int(_unit(*parts, "tone") * 3)
        ]
        context = f" Brief: {brief.strip()}." if brief else ""
        persona = (
            f"PERSONA[{tag}]: {audience} voice as a {tone} for {offer}."
            f"{context} Keep claims grounded and creator-safe."
        )
        if revision:
            persona += f" Revision directive: {revision}"
        return persona

    # --- Step 1: conceitos ---
    @traced("adapter.mock.generate_concepts", run_type="chain", step=1, provider="mock")
    async def generate_concepts(
        self,
        offer: str,
        n: int,
        seed: str,
        bias: Optional[list[str]] = None,
        revision: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        await self._tick()
        # bias = hooks vencedores do ciclo anterior (Step 10 -> 1). Uma fração dos
        # conceitos é puxada para esses estilos, mantendo determinismo e spread.
        bias = [b for b in (bias or []) if b in _HOOK_STYLES]
        bias_strength = 0.6
        # revision (Fase 7): quando o agent pede refino, a diretiva entra no hash para
        # produzir um output distinto e determinístico. None/"" mantém a saída base
        # byte-idêntica ao modo tool (o part de revisão nem entra no hash).
        concepts: list[dict[str, Any]] = []
        for i in range(n):
            persona_part = (f"persona:{persona}",) if persona else ()
            if revision:
                style_parts: tuple[Any, ...] = (seed, offer, *persona_part, f"rev:{revision}", i)
                tag_key = "|".join(str(part) for part in style_parts)
            else:
                style_parts = (seed, offer, *persona_part, i)
                tag_key = "|".join(str(part) for part in style_parts)
            style = _HOOK_STYLES[int(_unit(*style_parts) * len(_HOOK_STYLES))]
            if bias and _unit("bias", seed, offer, i) < bias_strength:
                style = bias[0]
            tag = hashlib.sha256(tag_key.encode()).hexdigest()[:8]
            concepts.append(
                {
                    "id": f"concept-{tag}",
                    "offer": offer,
                    "hook": f"hook[{style}]-{tag}",
                    "angle": style,
                    "hook_style": style,
                    "format": ["talking_head", "demo", "reaction"][i % 3],
                }
            )
        return concepts

    # --- Step 2: scripts ---
    @traced("adapter.mock.write_script", run_type="chain", step=2, provider="mock")
    async def write_script(
        self,
        concept: dict[str, Any],
        creator_ref: str,
        platform: str,
        revision: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> str:
        await self._tick()
        hook = concept.get("hook", "hook")
        pacing = "fast" if platform.lower() == "tiktok" else "medium"
        script = (
            f"HOOK: {hook} Se você não conhece precisa ver isso.\n"
            f"BODY: ({platform} / pacing={pacing}) creator={creator_ref} fala sobre "
            f"{concept.get('offer', 'o produto')} com resultados reais comprovados no dia a dia.\n"
            f"CTA: confere o link e garante o seu hoje mesmo."
        )
        if persona:
            tag = hashlib.sha256(persona.encode()).hexdigest()[:8]
            script += f"\nPERSONA_CONTEXT[{tag}]: {persona}"
        # revision (Fase 7): refino do agent anexa uma linha determinística; None mantém
        # o script base inalterado (backward-compatible com o modo tool).
        if revision:
            tag = hashlib.sha256(f"{hook}|{revision}".encode()).hexdigest()[:8]
            script += f"\nREVISED[{tag}]: {revision}"
        return script

    # --- Fase 1: execução agentic (concepts/scripts) via loop de tool-calling ---
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
        require_tool_call: bool = False,
        stop_after_success: bool = False,
    ) -> AgentRunResult:
        """Loop de tool-calling determinístico e offline (custo zero).

        Usa o loop compartilhado com um *brain* determinístico: chama a tool primária
        para o rascunho, avalia com um heurístico (``_agent_critique``) e, se pedir
        refino, chama a tool de novo com a diretiva. Nunca chama ``generate_concepts``/
        ``write_script`` diretamente — só ``run_tool`` (fronteira D29).
        """
        if require_tool_call and stop_after_success:
            call = ToolCall(
                id="mock-submission",
                name=allowed_tools[0],
                arguments=_terminal_submission(stage, inputs),
            )
            result = await run_tool(call.name, **call.arguments)
            run = AgentRunResult(
                result=result,
                attempts=(ToolAttempt(call=call, result=result),),
            )
            add_trace_metadata(
                agent_backend="mock",
                stage=stage,
                allowed_tools=list(allowed_tools),
                target_model=target_model,
                agent_steps=run.executed,
            )
            return run

        brain = _MockAgentBrain(self._agent_critique, system_prompt=system_prompt)
        run = await run_agent_loop(
            brain,
            stage=stage,
            allowed_tools=allowed_tools,
            run_tool=run_tool,
            inputs=inputs,
            max_steps=max_steps,
            tool_schemas=tool_call_schemas(allowed_tools),
            max_tool_calls=max_tool_calls,
            require_tool_call=require_tool_call,
            stop_after_success=stop_after_success,
        )
        add_trace_metadata(
            agent_backend="mock",
            stage=stage,
            allowed_tools=list(allowed_tools),
            target_model=target_model,
            agent_steps=run.executed,
        )
        return run

    @staticmethod
    def _agent_critique(stage: str, draft: Any) -> Optional[str]:
        """Heurístico determinístico: metade dos rascunhos (por hash) pede um refino.

        Retorno ``None`` = rascunho aceito; string = diretiva de refino.
        """
        fingerprint = hashlib.sha256(repr(draft).encode()).hexdigest()
        if int(fingerprint[:2], 16) % 2 == 0:
            return None
        return f"Refine the {stage} output: strengthen the hook and tighten the CTA."

    # --- Step 3: creator reutilizável ---
    @traced("adapter.mock.build_creator", run_type="tool", step=3, provider="mock")
    async def build_creator(
        self,
        index: int,
        system_prompt: Optional[str] = None,
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]:
        await self._tick()
        sfx = ""
        if system_prompt:
            sfx = "-" + hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
        resolved_voice = resolve_voice_profile(system_prompt, voice_profile)
        voice_seed = sfx
        if resolved_voice is not None:
            voice_seed += "-" + hashlib.sha256(
                f"{resolved_voice.preset}|{resolved_voice.prompt}".encode()
            ).hexdigest()[:8]
        # A imagem também codifica o preset resolvido: mesmo sem rosto real, o mock
        # mantém paridade imagem↔voz em nível de metadado/determinismo.
        image_preset = resolved_voice.preset if resolved_voice is not None else ""
        creator = {
            "id": f"creator-{index}",
            "angles": ["front", "3/4", "profile", "smile", "neutral"],
            "upscaled_base": _svg_data_uri(
                f"C{index}{sfx}", "creator", index, sfx, image_preset
            ),
            "voice_id": f"voice-{index}{voice_seed}",
            "voice_preview_uri": _wav_data_uri(
                "creator",
                index,
                voice_seed,
                resolved_voice.preset if resolved_voice is not None else "",
                resolved_voice.prompt if resolved_voice is not None else "",
            ),
        }
        if resolved_voice is not None:
            creator["voice_profile"] = resolved_voice.as_dict()
        return creator

    @traced(
        "adapter.mock.synthesize_voiceover",
        run_type="tool",
        step="voiceover",
        provider="mock",
    )
    async def synthesize_voiceover(self, *, voice_ref: str, text: str) -> Artifact:
        await self._tick()
        payload = _wav_data_uri("voiceover", voice_ref, text)
        return Artifact(
            kind="voiceover",
            uri=payload,
            meta={
                "provider": "mock",
                "voice_ref": voice_ref,
                "characters": len(text),
                "cost_usd": 0.0,
            },
        )

    @traced(
        "adapter.mock.design_voice_candidates",
        run_type="tool",
        step="voice_candidates",
        provider="mock",
    )
    async def design_voice_candidates(
        self,
        spec: CreatorVoiceSpec | dict[str, Any],
        *,
        preview_text: str | None = None,
    ) -> VoiceDesignBatch:
        await self._tick()
        spec_obj = (
            spec
            if isinstance(spec, CreatorVoiceSpec)
            else CreatorVoiceSpec.model_validate(spec)
        )
        spec_hash = hashlib.sha256(spec_obj.model_dump_json().encode()).hexdigest()[:10]
        candidates: list[VoiceCandidate] = []
        for i in range(3):
            cand_id = f"cand-mock-{spec_hash}-{i}"
            payload = _wav_data_uri("mock-candidate", cand_id, spec_obj.vocal_presentation)
            artifact = Artifact(
                kind="voice_preview",
                uri=payload,
                meta={"candidate_id": cand_id, "provider": "mock"},
            )
            candidates.append(
                VoiceCandidate(
                    candidate_id=cand_id,
                    preview=artifact,
                    duration_seconds=5.0,
                    media_type="audio/mpeg",
                )
            )
        return VoiceDesignBatch(
            provider="elevenlabs",
            design_model="eleven_ttv_v3",
            description_hash=spec_hash,
            prompt_version="voice-match-v1",
            candidates=candidates,
            cost_usd=0.0,
        )

    @traced(
        "adapter.mock.finalize_voice",
        run_type="tool",
        step="finalize_voices",
        provider="mock",
    )
    async def finalize_voice(
        self,
        candidate_id: str,
        *,
        batch: VoiceDesignBatch | dict[str, Any],
        creator_id: str,
        organization_id: str,
    ) -> FinalizedVoice:
        await self._tick()
        batch_obj = (
            batch
            if isinstance(batch, VoiceDesignBatch)
            else VoiceDesignBatch.model_validate(batch)
        )
        cand = next(
            (c for c in batch_obj.candidates if c.candidate_id == candidate_id),
            batch_obj.candidates[0],
        )
        voice_ref = f"voice-mock-{creator_id}-{candidate_id[:8]}"
        return FinalizedVoice(
            provider="elevenlabs",
            voice_ref=voice_ref,
            selected_candidate_id=candidate_id,
            preview_uri=cand.preview.uri,
            design_model=batch_obj.design_model,
            tts_model="eleven_turbo_v2_5",
        )

    # --- Steps 4/5: vídeo (talking-head / demo) ---
    @traced("adapter.mock.generate_clip", run_type="tool", step="video", provider="mock")
    async def generate_clip(
        self, item_id: str, tier: str, seconds: int, attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
    ) -> Artifact:
        spec = self.tiers[tier]  # KeyError em tier desconhecido (contratual)
        async with self._semaphores[tier]:
            await self._tick()
            cost = round(spec["cost_per_second"] * seconds, 4)
            sfx = ""
            if system_prompt:
                sfx = "-" + hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
            meta: dict[str, Any] = {
                "tier": tier,
                "model": spec["model"],
                "seconds": seconds,
                "cost_usd": cost,
                "attempt": attempt,
            }
            if system_prompt:
                meta["prompt_hash"] = hashlib.sha256(
                    system_prompt.encode()
                ).hexdigest()
            if reference_image_uri:
                meta["has_reference_image"] = True
            return Artifact(
                kind="clip",
                uri=_mp4_data_uri("clip", item_id, attempt, sfx),
                meta=meta,
            )

    # --- Step 7: QC ---
    @traced("adapter.mock.qc_check", run_type="tool", step=7, provider="mock")
    async def qc_check(
        self,
        item: Item | str | None = None,
        fail_rate: float = 0.34,
        *,
        item_id: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> QCResult:
        await self._tick()
        if isinstance(item, Item):
            resolved_item_id = item.id
            resolved_attempt = item.attempts if attempt is None else attempt
        else:
            resolved_item_id = item_id or str(item or "")
            resolved_attempt = int(attempt or 0)
        if not resolved_item_id:
            raise ValueError("qc_check requires item or item_id")
        base = _unit("qc", resolved_item_id)
        score = min(0.999, base + 0.25 * resolved_attempt)
        passed = score >= fail_rate
        reasons: list[str] = []
        if not passed:
            k = 1 + int(_unit("nreasons", resolved_item_id) * 2)  # 1..2 problemas
            start = int(_unit("which", resolved_item_id) * len(_QC_SUSPECTS))
            reasons = [_QC_SUSPECTS[(start + j) % len(_QC_SUSPECTS)] for j in range(k)]
        return QCResult(passed=passed, score=round(score, 4), reasons=reasons)

    # --- Step 8: montagem ---
    @traced("adapter.mock.assemble", run_type="tool", step=8, provider="mock")
    async def assemble(
        self,
        item: Item | str | None = None,
        platform: str = "tiktok",
        *,
        item_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> Artifact:
        await self._tick()
        if isinstance(item, Item):
            resolved_item_id = item.id
        else:
            resolved_item_id = item_id or str(item or "")
        if not resolved_item_id:
            raise ValueError("assemble requires item or item_id")
        return Artifact(
            kind="video",
            uri=_mp4_data_uri("video", resolved_item_id, system_prompt or ""),
            meta={"captions": True, "broll": True, "platform": platform},
        )

    # --- Step 8 (pós-montagem): upscale do vídeo final ---
    @traced("adapter.mock.upscale", run_type="tool", step=8, provider="mock")
    async def upscale(self, media_uri: str) -> str:
        """Upscale determinístico do vídeo final: deriva uma nova uri de ``media_uri``.

        Distinta da entrada (o node consegue provar que rodou), reprodutível por hash.
        """
        await self._tick()
        return _mp4_data_uri("upscaled", media_uri)

def build_mock_adapter(tiers: list[dict[str, Any]], latency: Optional[float] = None) -> MockAdapter:
    return MockAdapter(tiers=tiers, latency=latency or 0.0)
