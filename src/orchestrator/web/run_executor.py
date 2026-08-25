"""Execução da pipeline em background para o dashboard web.

Hospeda ``_execute_run`` e os helpers de emissão de eventos SSE (``_emit``,
``_emit_sync``), extraídos do antigo ``web/server.py`` sem mudança de
comportamento — incluindo o gate humano dual-mode: no modo local o loop pausa
no interrupt ``review_creative_plan``, publica o payload canônico no registro
de runs e aguarda o Future resolvido por ``POST /api/v2/runs/{run_id}/review``;
no modo durável quem consome gates de PostgreSQL é o Runner (fora daqui).

Colaboradores que os testes fazem ``monkeypatch.setattr(web_server, ...)``
(load_pipeline/providers/agent_catalog, open_checkpointer, build_graph,
run_trace_config, default_creator_store_path, _emit, _emit_sync) são resolvidos
tardiamente via :func:`_server_attr`, preservando esse contrato sem criar ciclo
de import com o composition root.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Optional

from langgraph.types import Command

import orchestrator.creator_store as creator_store
import orchestrator.run_store as run_store
from orchestrator import runner, stream_bus
from orchestrator.common.plain import to_plain as _to_plain
from orchestrator.creative_contracts import CampaignInput
from orchestrator.dependencies import RunDependencies
from orchestrator.graph.topology import topology_for_tiers
from orchestrator.nodes.base import tier_names
from orchestrator.progress import ProgressEventTranslator
from orchestrator.runtime_contract import build_runtime_contract
from orchestrator.web import runs_registry
from orchestrator.web.events import (
    _build_item_update,
    _complete_item_payload,
    _extract_artifacts,
    _item_payload_from_result,
    _normalize_creator,
    _safe_serialize,
)

_RUN_REPOSITORY_UNSET = object()


def _server_attr(name: str) -> Any:
    """Lookup tardio de colaborador no composition root (web.server).

    Mantém visível, em runtime, qualquer patch feito em ``orchestrator.web.server``
    pelos testes; sem patch, retorna exatamente a função reexportada.
    """
    from orchestrator.web import server

    return getattr(server, name)


def _emit_sync(run_id: str, event: dict[str, Any]) -> None:
    """Emite evento de forma síncrona (seguro dentro de contexto async)."""
    state = runs_registry.REGISTRY.get(run_id)
    if state is None:
        return
    sequence = int(state.get("event_sequence") or len(state.get("buffer") or [])) + 1
    state["event_sequence"] = sequence
    event = {
        **event,
        "event_id": event.get("event_id") or f"local-{sequence}",
        "occurred_at": event.get("occurred_at") or datetime.now(UTC).isoformat(),
    }
    state["buffer"].append(event)
    for q in list(state["queues"]):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _emit(run_id: str, event: dict[str, Any]) -> None:
    _emit_sync(run_id, event)


async def _execute_run(
    run_id: str,
    offer: str,
    batch: int,
    platform: str,
    config_dir: Optional[str],
    db_path: str,
    creator_prompt: Optional[str] = None,
    video_prompt: Optional[str] = None,
    approve_creators: bool = True,
    edit_concepts: bool = True,
    seed_creator: Optional[dict[str, Any]] = None,
    campaign_payload: Optional[dict[str, Any]] = None,
    review_plan: Optional[bool] = None,
    _run_repository: Any = _RUN_REPOSITORY_UNSET,
) -> None:
    """Roda a pipeline completa, emitindo eventos para os subscribers SSE.

    Quando ``approve_creators=True`` o loop pausa no interrupt, emite
    ``awaiting_approval`` e aguarda a resolução do Future criado por
    ``POST /api/approve/{run_id}``, depois retoma com ``Command(resume=...)``.
    """
    if _run_repository is _RUN_REPOSITORY_UNSET:
        async with run_store.open_repository() as repository:
            await _execute_run(
                run_id,
                offer,
                batch,
                platform,
                config_dir,
                db_path,
                creator_prompt,
                video_prompt,
                approve_creators,
                edit_concepts,
                seed_creator,
                campaign_payload,
                review_plan,
                repository,
            )
        return

    def token_cb(event: dict[str, Any]) -> None:
        if event.get("type") == "creator_ready" and isinstance(event.get("creator"), dict):
            event = {**event, "creator": _normalize_creator(event["creator"])}
        _server_attr("_emit_sync")(run_id, event)

    stream_bus.set_token_callback(token_cb)

    store_path = str(_server_attr("default_creator_store_path")())
    # Guarda metadados do run para uso no record_creators
    run_state = runs_registry.REGISTRY.get(run_id, {})
    run_state["offer"] = offer
    run_state["creator_prompt"] = creator_prompt
    run_state["video_prompt"] = video_prompt
    campaign = CampaignInput.model_validate(
        campaign_payload
        or {
            "offer": offer,
            "audience": "General adult audience",
            "creator_direction": creator_prompt,
            "video_direction": video_prompt,
            "platform": platform,
            "batch_size": batch,
        }
    )
    run_state["campaign"] = campaign.model_dump(mode="json")
    run_state.setdefault("item_snapshots", {})

    try:
        pipeline = _server_attr("load_pipeline")(config_dir)
        topology = topology_for_tiers(tier_names(pipeline))
        providers = _server_attr("load_providers")(config_dir)
        agent_catalog = _server_attr("load_agent_catalog")(config_dir)
        contract = build_runtime_contract(pipeline, providers, agent_catalog=agent_catalog)
        run_state["runtime_contract"] = contract.as_dict()
        dependencies = RunDependencies.build(pipeline, providers, agent_catalog=agent_catalog)
        run_state["adapter"] = dependencies.adapter

        cfg: dict[str, Any] = {
            "configurable": dependencies.configurable(
                run_id=run_id,
                platform=platform,
                run_options={
                    "platform": platform,
                    "creator_prompt": creator_prompt,
                    "video_prompt": video_prompt,
                    # Default: pausa no gate humano para o usuário escolher quais
                    # creators (imagem + voz) estrelam os vídeos; opt-out via
                    # approve_creators=False no POST /api/run.
                    "approve_creators": approve_creators,
                    # Default: pausa ANTES do creator para o usuário editar/descartar
                    # concept+script; opt-out via edit_concepts=False no POST /api/run.
                    "edit_concepts": edit_concepts,
                    "seed_creator": seed_creator,
                    "review_plan": (
                        review_plan
                        if review_plan is not None
                        else bool(approve_creators or edit_concepts)
                    ),
                },
            ),
            "max_concurrency": int(pipeline.get("batch", {}).get("max_concurrency", 8)),
            "recursion_limit": 100,
        }
        cfg.update(
            _server_attr("run_trace_config")(run_id, offer=offer, platform=platform, batch=batch)
        )
        init: Any = {
            "run_id": run_id,
            "runtime_contract": contract.as_dict(),
            "config": {"offer": offer, "batch_size": batch},
            "campaign": campaign.model_dump(mode="json"),
        }

        await _server_attr("_emit")(
            run_id, {"type": "run_start", "run_id": run_id, "offer": offer, "batch": batch}
        )

        final_output: dict[str, Any] = {}
        progress_translator = ProgressEventTranslator(topology=topology)

        async with _server_attr("open_checkpointer")(db_path) as cp:
            graph = _server_attr("build_graph")(pipeline, checkpointer=cp)
            resume_input = init

            while True:
                async for event in graph.astream_events(resume_input, cfg, version="v2"):
                    etype: str = event["event"]
                    meta = event.get("metadata", {})
                    node = meta.get("langgraph_node") or event.get("name", "")
                    progress_event = progress_translator.translate(event)
                    if progress_event is not None:
                        await _server_attr("_emit")(run_id, progress_event)

                    if node in topology.pipeline_nodes:
                        if etype == "on_chain_start":
                            await _server_attr("_emit")(
                                run_id,
                                {
                                    "type": "node_start",
                                    "node": node,
                                    "label": topology.node_labels.get(node, node),
                                },
                            )
                        elif etype == "on_chain_end":
                            data = event.get("data", {})
                            output = data.get("output", {})
                            payload: dict[str, Any] = {
                                "type": "node_end",
                                "node": node,
                                "label": topology.node_labels.get(node, node),
                            }
                            # Para process_item extraímos o resumo do item
                            if node == "process_item" and isinstance(output, dict):
                                items = output.get("results", [])
                                if items:
                                    item = items[-1]
                                    if hasattr(item, "model_dump"):
                                        item = item.model_dump()
                                    payload["item"] = _safe_serialize(
                                        {
                                            "id": item.get("id"),
                                            "concept": item.get("concept", {}),
                                            "dropped": item.get("dropped"),
                                            "attempts": item.get("attempts"),
                                            "cost_usd": item.get("cost_usd"),
                                            "qc": item.get("qc"),
                                            "artifacts": _extract_artifacts(item),
                                        }
                                    )
                            await _server_attr("_emit")(run_id, payload)
                            item_update = _build_item_update(
                                run_id,
                                node,
                                data,
                                run_state.setdefault("item_snapshots", {}),
                                topology=topology,
                            )
                            if item_update:
                                persisted_items = [
                                    _safe_serialize(_complete_item_payload(snapshot))
                                    for snapshot in run_state["item_snapshots"].values()
                                    if isinstance(snapshot, dict)
                                ]
                                if _run_repository is not None:
                                    await _run_repository.save(
                                        run_id,
                                        phase="running",
                                        state={
                                            "run_id": run_id,
                                            "offer": offer,
                                            "platform": platform,
                                        },
                                        summary=runner.summarize(
                                            {
                                                "run_id": run_id,
                                                "results": persisted_items,
                                            }
                                        ),
                                        items=persisted_items,
                                    )
                                await _server_attr("_emit")(run_id, item_update)

                    # Captura o estado final do grafo raiz
                    if etype == "on_chain_end" and event.get("name") == "LangGraph":
                        out = event.get("data", {}).get("output", {})
                        if isinstance(out, dict):
                            final_output = out

                # Verifica se há interrupt pendente
                snap = await graph.aget_state(cfg)
                all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
                if snap.next and all_interrupts:
                    intr_payload = all_interrupts[0].value  # {"type": ...}
                    if intr_payload.get("type") == "review_creative_plan":
                        concepts = [_safe_serialize(c) for c in intr_payload.get("concepts", [])]
                        creators = [_normalize_creator(c) for c in intr_payload.get("creators", [])]
                        review_payload = {
                            "concepts": concepts,
                            "creators": creators,
                        }
                        await _server_attr("_emit")(
                            run_id,
                            {
                                "type": "awaiting_review",
                                "run_id": run_id,
                                **review_payload,
                            },
                        )
                        review_future = asyncio.get_event_loop().create_future()
                        run_state_ref = runs_registry.REGISTRY.get(run_id)
                        if run_state_ref is not None:
                            run_state_ref["review"] = review_future
                            run_state_ref["pending_review"] = review_payload
                        persisted_state = _to_plain(dict(snap.values or {}))
                        persisted_state["pending_review"] = review_payload
                        if _run_repository is not None:
                            await _run_repository.save(
                                run_id,
                                phase="review",
                                state=persisted_state,
                                summary=runner.summarize(
                                    {
                                        **dict(snap.values or {}),
                                        "run_id": run_id,
                                    }
                                ),
                                items=[],
                            )
                        review_decision = await review_future
                        if review_decision.get("action") == "approve":
                            approved_creators = review_decision.get("creators") or creators
                            async with creator_store.open_repository(store_path) as creator_repo:
                                await creator_repo.record_creators(
                                    run_id,
                                    approved_creators,
                                    approved_ids=[
                                        str(c.get("id")) for c in approved_creators if c.get("id")
                                    ],
                                    creator_prompt=creator_prompt,
                                    video_prompt=video_prompt,
                                    offer=offer,
                                )
                        resume_input = Command(resume=review_decision)
                        continue
                    raise RuntimeError(f"unsupported human gate: {intr_payload.get('type')!r}")
                # Em fluxos com subgrafo + interrupts, o último evento "LangGraph"
                # observado em astream_events pode ser um output intermediário. O
                # snapshot raiz é a fonte correta para o resumo público do run.
                if snap.values:
                    final_output = dict(snap.values)
                break

        summary = runner.summarize({**final_output, "run_id": run_id}) if final_output else {}
        if _run_repository is not None:
            await _run_repository.save(
                run_id,
                phase="done",
                state=_to_plain(final_output),
                summary=_safe_serialize(summary),
                items=[
                    _item_payload_from_result(item) for item in (final_output.get("results") or [])
                ],
            )
        await _server_attr("_emit")(
            run_id, {"type": "run_end", "run_id": run_id, "summary": summary}
        )

    except Exception as exc:  # noqa: BLE001
        # Grava o erro no estado runtime além de emitir no SSE, para que a falha
        # persista (fase "error" + mensagem) em reconexões e na lista de campanhas,
        # não só no stream ao vivo.
        state = runs_registry.REGISTRY.get(run_id)
        if state is not None:
            state["error"] = str(exc)
        snapshots = (state or {}).get("item_snapshots") or {}
        items = (
            [
                _safe_serialize(_complete_item_payload(snapshot))
                for snapshot in snapshots.values()
                if isinstance(snapshot, dict)
            ]
            if isinstance(snapshots, dict)
            else []
        )
        if _run_repository is not None:
            await _run_repository.save(
                run_id,
                phase="error",
                state={
                    "run_id": run_id,
                    "offer": offer,
                    "platform": platform,
                },
                summary={},
                items=items,
                error=str(exc),
            )
        await _server_attr("_emit")(run_id, {"type": "error", "message": str(exc)})

    finally:
        stream_bus.clear_token_callback()
        state = runs_registry.REGISTRY.get(run_id)
        if state:
            state["done"] = True
            for q in list(state["queues"]):
                q.put_nowait(None)  # sentinel: fecha o stream SSE
