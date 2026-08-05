"""Orquestração de alto nível: roda/retoma/inspeciona um run do grafo."""
from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from langgraph.types import Command

import orchestrator.feedback_store as _feedback_store
from orchestrator.agent_catalog import AgentCatalog, default_agent_catalog
from orchestrator.config import (
    default_artifacts_db_path,
    default_media_path,
    default_videos_path,
)
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.graph.state import Item
from orchestrator.progress import ProgressEventTranslator
from orchestrator.registry import build_adapter_from_providers
from orchestrator.storage.db import (
    ArtifactDB,
    ArtifactRepository,
    open_artifact_repository,
)
from orchestrator.storage.factory import build_media_storage
from orchestrator.tracing import run_trace_config

ProgressEventSink = Callable[[dict[str, Any]], Awaitable[None]]


def _build_config(
    pipeline: dict[str, Any],
    providers: dict[str, Any],
    run_id: str,
    platform: str,
    feedback_store: Optional[str | Path] = None,
    agent_catalog: Optional[AgentCatalog] = None,
    artifact_repository: Optional[ArtifactRepository] = None,
    run_options: Optional[dict[str, Any]] = None,
    effect_ledger: Any | None = None,
    durable: bool = False,
) -> dict[str, Any]:
    adapter = build_adapter_from_providers(providers, pipeline)
    catalog = agent_catalog or default_agent_catalog()

    # Storage e DB de artifacts (D30) são resolvidos uma vez por run, como o adapter:
    # construí-los por chamada recriaria o client S3 a cada clip.
    if artifact_repository is None:
        artifact_repository = ArtifactDB(default_artifacts_db_path())
        artifact_repository.setup()

    configurable: dict[str, Any] = {
        "adapter": adapter,
        "pipeline": pipeline,
        "agent_catalog": catalog,
        "run": {"platform": platform},
        "thread_id": run_id,
        "media_storage": build_media_storage(
            providers, root=default_media_path(), web_prefix="/media",
        ),
        "videos_storage": build_media_storage(
            providers, root=default_videos_path(), web_prefix="/videos",
        ),
        "artifact_db": artifact_repository,
        "effect_ledger": effect_ledger,
        "durable": durable,
    }
    configurable["run"].update(run_options or {})
    if feedback_store is not None:
        configurable["feedback_store"] = str(feedback_store)
    return {
        "configurable": configurable,
        "max_concurrency": int(pipeline.get("batch", {}).get("max_concurrency", 8)),
        "recursion_limit": 100,
    }


async def run_pipeline(
    pipeline: dict[str, Any],
    providers: dict[str, Any],
    *,
    db_path: str | Path,
    run_id: Optional[str] = None,
    batch: Optional[int] = None,
    offer: str = "demo offer",
    platform: str = "tiktok",
    feedback_store: Optional[str | Path] = None,
    agent_catalog: Optional[AgentCatalog] = None,
    run_options: Optional[dict[str, Any]] = None,
    event_sink: Optional[ProgressEventSink] = None,
    effect_ledger: Any | None = None,
    durable: bool = False,
) -> tuple[str, dict[str, Any]]:
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    async with open_artifact_repository(default_artifacts_db_path()) as artifact_repository:
        cfg = _build_config(
            pipeline,
            providers,
            run_id,
            platform,
            feedback_store,
            agent_catalog,
            artifact_repository,
            run_options,
            effect_ledger,
            durable,
        )
        cfg.update(run_trace_config(run_id, offer=offer, platform=platform, batch=batch))
        # Step 10 -> Step 1: lê o feedback do ciclo anterior (se houver) e o injeta no
        # estado inicial, fechando o loop (concepts pode usar isso como viés no futuro).
        prior = None
        if feedback_store is not None:
            async with _feedback_store.open_repository(feedback_store) as repository:
                prior = await repository.load_latest_feedback()
        prior_styles = (prior or {}).get("winning_styles", [])
        campaign = (run_options or {}).get("campaign")
        init = {
            "run_id": run_id,
            "config": {
                "offer": offer,
                "batch_size": batch,
                "prior_winning_styles": prior_styles,
            },
        }
        if isinstance(campaign, dict):
            init["campaign"] = campaign
        async with open_checkpointer(db_path) as cp:
            app = build_graph(pipeline, checkpointer=cp)
            out = await _invoke_with_progress(app, init, cfg, event_sink)
        return run_id, out


async def run_cycles(
    pipeline: dict[str, Any],
    providers: dict[str, Any],
    *,
    db_path: str | Path,
    cycles: int,
    feedback_store: Optional[str | Path],
    batch: Optional[int] = None,
    offer: str = "demo offer",
    platform: str = "tiktok",
    run_id_prefix: Optional[str] = None,
    agent_catalog: Optional[AgentCatalog] = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Roda *cycles* runs encadeados, fechando o loop a cada iteração.

    Cada ciclo é um run independente (thread_id próprio, checkpoint separado) mas
    compartilha o mesmo ``feedback_store``: ``run_pipeline`` já lê o feedback mais
    recente (vencedores do ciclo anterior viram viés) e o node de feedback grava o
    agregado no fim. Encadear é, portanto, chamar ``run_pipeline`` em sequência.
    """
    if cycles < 1:
        raise ValueError("cycles deve ser >= 1")
    if feedback_store is None:
        raise ValueError("run_cycles exige um feedback_store para encadear os ciclos")
    prefix = run_id_prefix or f"loop-{uuid.uuid4().hex[:8]}"
    results: list[tuple[str, dict[str, Any]]] = []
    for i in range(1, cycles + 1):
        rid, out = await run_pipeline(
            pipeline, providers, db_path=db_path, run_id=f"{prefix}-c{i}",
            batch=batch, offer=offer, platform=platform, feedback_store=feedback_store,
            agent_catalog=agent_catalog,
        )
        results.append((rid, out))
    return results


async def resume_pipeline(
    pipeline: dict[str, Any],
    providers: dict[str, Any],
    *,
    db_path: str | Path,
    run_id: str,
    platform: str = "tiktok",
    feedback_store: Optional[str | Path] = None,
    agent_catalog: Optional[AgentCatalog] = None,
    resume_value: Any = None,
    run_options: Optional[dict[str, Any]] = None,
    event_sink: Optional[ProgressEventSink] = None,
    effect_ledger: Any | None = None,
    durable: bool = False,
) -> tuple[str, dict[str, Any]]:
    async with open_artifact_repository(default_artifacts_db_path()) as artifact_repository:
        cfg = _build_config(
            pipeline,
            providers,
            run_id,
            platform,
            feedback_store,
            agent_catalog,
            artifact_repository,
            run_options,
            effect_ledger,
            durable,
        )
        cfg.update(run_trace_config(run_id, platform=platform))
        async with open_checkpointer(db_path) as cp:
            app = build_graph(pipeline, checkpointer=cp)
            resume_input = (
                Command(resume=resume_value)
                if resume_value is not None
                else None
            )
            out = await _invoke_with_progress(app, resume_input, cfg, event_sink)
        return run_id, out


async def _invoke_with_progress(
    app: Any,
    input_value: Any,
    config: dict[str, Any],
    event_sink: Optional[ProgressEventSink],
) -> dict[str, Any]:
    if event_sink is None:
        return await app.ainvoke(input_value, config)

    translator = ProgressEventTranslator()
    final_output: dict[str, Any] = {}
    async for event in app.astream_events(input_value, config, version="v2"):
        progress_event = translator.translate(event)
        if progress_event is not None:
            await event_sink(progress_event)
        if event.get("event") == "on_chain_end" and event.get("name") == "LangGraph":
            output = event.get("data", {}).get("output")
            if isinstance(output, dict):
                final_output = output

    snapshot = await app.aget_state(config)
    if snapshot is not None and snapshot.values:
        return dict(snapshot.values)
    return final_output


async def get_interrupt(
    pipeline: dict[str, Any],
    *,
    db_path: str | Path,
    run_id: str,
) -> Optional[dict[str, Any]]:
    """Devolve o primeiro gate pendente do checkpoint, se existir."""
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        snapshot = await app.aget_state(
            {"configurable": {"thread_id": run_id}}
        )
    if snapshot is None:
        return None
    for task in snapshot.tasks or []:
        interrupts = getattr(task, "interrupts", ())
        if interrupts:
            value = interrupts[0].value
            return value if isinstance(value, dict) else {"type": "unknown"}
    return None


async def get_status(
    pipeline: dict[str, Any], *, db_path: str | Path, run_id: str
) -> Optional[dict[str, Any]]:
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        snap = await app.aget_state({"configurable": {"thread_id": run_id}})
    return snap.values if snap and snap.values else None


def _clean_task_error(err: Any) -> str:
    """Extrai a mensagem útil do erro de uma task (str repr ``ExcType('msg\\n...')``).

    Descarta o stack trace (Python ou o do bridge Node, que chega com ``\\n    at ...``
    literais) e o wrapper ``ExcType('...')``, deixando só a primeira linha da mensagem.
    """
    text = str(err or "").strip()
    if not text:
        return "task falhou"
    text = text.replace("\\n", "\n")                    # \n literais do repr -> quebra real
    first = text.split("\n", 1)[0].strip()              # corta o stack multi-linha
    first = re.split(r"\s+at\s+\S+\s*\(", first)[0].strip()  # corta stack inline "   at fn ("
    first = re.sub(r"^[A-Za-z_][\w.]*\((['\"])", "", first)  # tira o "RuntimeError('"
    first = re.sub(r"(['\"])\)?$", "", first)                # tira o "')" final, se houver
    return first or "task falhou"


async def get_pending_items(
    pipeline: dict[str, Any], *, db_path: str | Path, run_id: str
) -> list[Item]:
    """Itens em voo/falhos que ainda **não** entraram em ``results``.

    Um item que quebra num node fora do try/except (ex.: crash na montagem, processo
    morto) nunca é escrito no canal ``results`` — some da UI mesmo com clips reais no
    disco. Aqui recuperamos o estado do subgrafo per-item direto do checkpoint
    (``aget_state(subgraphs=True)`` expõe cada ``process_item`` pendente com seu ``Item``
    e o erro da task), para a UI voltar a mostrá-los sem re-rodar.
    """
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        snap = await app.aget_state(
            {"configurable": {"thread_id": run_id}}, subgraphs=True
        )
    if snap is None:
        return []
    items: list[Item] = []
    for task in snap.tasks or []:
        state = getattr(task, "state", None)
        values = getattr(state, "values", None)
        if not isinstance(values, dict) or not values.get("id"):
            continue
        item = Item.model_validate(values)
        task_error = getattr(task, "error", None)
        if item.error is None and task_error is not None:
            item = item.model_copy(update={"error": f"production: {_clean_task_error(task_error)}"})
        # Só surfamos o que tem algo a mostrar: clips gerados ou um erro registrado.
        if item.clips or item.error:
            items.append(item)
    return items


def list_runs(db_path: str | Path) -> list[str]:
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path))
        rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    return sorted({r[0] for r in rows})


def as_items(results: Any) -> list[Item]:
    out: list[Item] = []
    for r in results or []:
        out.append(r if isinstance(r, Item) else Item.model_validate(r))
    return out


def summarize(out: dict[str, Any]) -> dict[str, Any]:
    """Relatório a partir do estado final (ou de um snapshot de status)."""
    results = as_items(out.get("results"))
    approved = [r for r in results if r.assembled is not None and not r.dropped]
    dropped = [r for r in results if r.dropped]
    in_flight = [
        r
        for r in results
        if r.assembled is None and not r.dropped and r.error is None
    ]
    tier_cost: dict[str, float] = {}
    stage_cost = {
        "voice_design": 0.0,
        "video": 0.0,
        "voiceover": 0.0,
        "assembly": 0.0,
    }
    seen_voice_designs: set[tuple[str, str, int]] = set()
    for creator in out.get("roster") or []:
        if not isinstance(creator, dict):
            continue
        batches = list(creator.get("voice_design_history") or [])
        batches.append(creator.get("voice_design_batch"))
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            key = (
                str(creator.get("id") or ""),
                str(batch.get("description_hash") or ""),
                int(batch.get("reroll_count") or 0),
            )
            if key in seen_voice_designs:
                continue
            seen_voice_designs.add(key)
            stage_cost["voice_design"] += float(batch.get("cost_usd") or 0.0)
    for r in results:
        for clip in r.clips:
            t = str(clip.meta.get("tier", "?"))
            clip_cost = float(clip.meta.get("cost_usd", 0.0))
            superseded_cost = sum(
                float(take.get("cost_usd") or 0.0)
                for take in clip.meta.get("superseded_takes", [])
                if isinstance(take, dict)
            )
            full_clip_cost = clip_cost + superseded_cost
            tier_cost[t] = round(tier_cost.get(t, 0.0) + full_clip_cost, 4)
            stage_cost["video"] += full_clip_cost
        if r.voiceover is not None:
            stage_cost["voiceover"] += float(
                r.voiceover.meta.get("cost_usd", 0.0)
            )
        if r.assembled is not None:
            stage_cost["assembly"] += float(
                r.assembled.meta.get("cost_usd", 0.0)
            )
    return {
        "run_id": out.get("run_id"),
        "produced": len(results),
        "approved": len(approved),
        "dropped": len(dropped),
        "in_flight": len(in_flight),
        "total_attempts": sum(r.attempts for r in results),
        "total_cost_usd": round(
            sum(r.cost_usd for r in results) + stage_cost["voice_design"],
            4,
        ),
        "cost_by_tier": tier_cost,
        "cost_by_stage": {
            stage: round(cost, 6)
            for stage, cost in stage_cost.items()
        },
        "winning_styles": (out.get("feedback") or {}).get("winning_styles", []),
    }


def format_report(out: dict[str, Any]) -> str:
    s = summarize(out)
    lines = [
        f"run {s['run_id']}",
        f"  produzidos : {s['produced']}",
        f"  aprovados  : {s['approved']}",
        f"  descartados: {s['dropped']}",
        f"  em andamento: {s['in_flight']}",
        f"  tentativas : {s['total_attempts']}",
        f"  custo total: ${s['total_cost_usd']:.4f}  {s['cost_by_stage']}",
        f"  hooks top  : {s['winning_styles']}",
    ]
    return "\n".join(lines)
