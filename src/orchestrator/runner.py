"""Orquestração de alto nível: roda/retoma/inspeciona um run do grafo."""
from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

from orchestrator.feedback_store import load_latest_feedback
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.graph.state import Item
from orchestrator.registry import build_adapter_from_providers
from orchestrator.tracing import run_trace_config


def _build_config(
    pipeline: dict[str, Any],
    providers: dict[str, Any],
    run_id: str,
    platform: str,
    feedback_store: Optional[str | Path] = None,
) -> dict[str, Any]:
    adapter = build_adapter_from_providers(providers, pipeline)
    configurable: dict[str, Any] = {
        "adapter": adapter,
        "pipeline": pipeline,
        "run": {"platform": platform},
        "thread_id": run_id,
    }
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
) -> tuple[str, dict[str, Any]]:
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    cfg = _build_config(pipeline, providers, run_id, platform, feedback_store)
    cfg.update(run_trace_config(run_id, offer=offer, platform=platform, batch=batch))
    # Step 10 -> Step 1: lê o feedback do ciclo anterior (se houver) e o injeta no
    # estado inicial, fechando o loop (concepts pode usar isso como viés no futuro).
    prior = load_latest_feedback(feedback_store) if feedback_store is not None else None
    prior_styles = (prior or {}).get("winning_styles", [])
    init = {
        "run_id": run_id,
        "config": {"offer": offer, "batch_size": batch, "prior_winning_styles": prior_styles},
    }
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        out = await app.ainvoke(init, cfg)
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
) -> tuple[str, dict[str, Any]]:
    cfg = _build_config(pipeline, providers, run_id, platform, feedback_store)
    cfg.update(run_trace_config(run_id, platform=platform))
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        out = await app.ainvoke(None, cfg)  # None => retoma do checkpoint
    return run_id, out


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
            item = item.model_copy(update={"error": f"assembly: {_clean_task_error(task_error)}"})
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
    in_flight = [r for r in results if r.assembled is None and not r.dropped]
    tier_cost: dict[str, float] = {}
    for r in results:
        for clip in r.clips:
            t = str(clip.meta.get("tier", "?"))
            tier_cost[t] = round(tier_cost.get(t, 0.0) + float(clip.meta.get("cost_usd", 0.0)), 4)
    return {
        "run_id": out.get("run_id"),
        "produced": len(results),
        "approved": len(approved),
        "dropped": len(dropped),
        "in_flight": len(in_flight),
        "total_attempts": sum(r.attempts for r in results),
        "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
        "cost_by_tier": tier_cost,
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
        f"  custo mock : ${s['total_cost_usd']:.2f}  {s['cost_by_tier']}",
        f"  hooks top  : {s['winning_styles']}",
    ]
    return "\n".join(lines)
