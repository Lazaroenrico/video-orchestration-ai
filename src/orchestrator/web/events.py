"""Normalização de eventos e projeções web-specific do dashboard.

Helpers puros que convertem o estado interno da pipeline no contrato público
da UI (artefatos, creators, snapshots de item, fases de runtime). Sem estado
próprio; o que depende de storage assinado ou das montagens de mídia permanece
no composition root (:mod:`orchestrator.web.server`).
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

from psycopg import OperationalError

from orchestrator.common.plain import to_plain as _to_plain
from orchestrator.creators import normalize_creator_payload
from orchestrator.graph.topology import DEFAULT_TOPOLOGY, PipelineTopology

# Erros de banco considerados "indisponibilidade transitória" pelo servidor web
# (handlers 503 e stream SSE persistido). ``asyncio.TimeoutError`` entra só na
# variante "ready" do composition root.
DATABASE_UNAVAILABLE_ERRORS = (OperationalError,)


def _persisted_event_payload(event: Any) -> dict[str, Any]:
    data = event.data if isinstance(event.data, dict) else {}
    if event.event_type in {"awaiting_approval", "awaiting_review"}:
        creators = data.get("creators")
        if isinstance(creators, list):
            data = {
                **data,
                "creators": [
                    _normalize_creator(creator) for creator in creators if isinstance(creator, dict)
                ],
            }
    return {
        **data,
        "type": event.event_type,
        "event_id": str(event.seq),
        "occurred_at": event.created_at.isoformat(),
    }


def _media_type_for_uri(uri: str) -> str:
    lower = uri.lower()
    if lower.startswith("data:image/"):
        return "image"
    if lower.startswith("data:video/"):
        return "video"
    if lower.startswith("data:audio/"):
        return "audio"
    path = urlparse(uri).path.lower()
    if path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")):
        return "image"
    if path.endswith((".mp4", ".mov", ".webm", ".m4v")):
        return "video"
    if path.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return "audio"
    return "reference"


def _is_renderable_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https"}:
        return _media_type_for_uri(uri) != "reference"
    if uri.startswith("data:"):
        return _media_type_for_uri(uri) in {"image", "video", "audio"}
    # Ponteiro canônico do R2 (D30): vira signed URL https na saída (``_sign_payload``),
    # então a UI consegue tocá-lo. Outros schemes seguem sendo referência opaca.
    if parsed.scheme == "r2":
        return _media_type_for_uri(uri) != "reference"
    if parsed.scheme:
        return False
    # Path local já servido pelo web app, absoluto ou relativo.
    if uri.startswith("/") or uri.startswith("./") or uri.startswith("../"):
        return _media_type_for_uri(uri) != "reference"
    return False


def _normalize_artifact(art: Any) -> Optional[dict[str, Any]]:
    """Normaliza um Artifact para o contrato público da UI."""
    art = _to_plain(art)
    if not isinstance(art, dict) or not art.get("uri"):
        return None
    uri = str(art["uri"])
    media_type = _media_type_for_uri(uri)
    return {
        "kind": art.get("kind", "artifact"),
        "uri": uri,
        "media_type": media_type,
        "renderable": _is_renderable_uri(uri),
    }


def _normalize_creator(creator: dict[str, Any]) -> dict[str, Any]:
    """Normaliza creator mantendo aliases legados durante a migração da UI."""
    return normalize_creator_payload(creator)


def _normalize_creator_history(creator: dict[str, Any]) -> dict[str, Any]:
    """Normaliza creator do histórico sem perder metadados salvos no store."""
    return {**creator, **_normalize_creator(creator)}


def _playable_voice_uri(creator: dict[str, Any]) -> Optional[str]:
    """URI de voz que o browser consegue tocar (path local /media, http(s) ou data:audio).

    Refs opacas (``voice_id`` do ElevenLabs, ``voice-0`` do mock) não tocam na web.
    """
    for key in (
        "voice_preview_uri",
        "voice_preview",
        "preview_uri",
        "voice_ref",
        "voice",
        "voice_id",
    ):
        uri = creator.get(key)
        if (
            isinstance(uri, str)
            and uri
            and _is_renderable_uri(uri)
            and _media_type_for_uri(uri) == "audio"
        ):
            return uri
    return None


def _has_complete_media(creator: dict[str, Any]) -> bool:
    """True só para uma pessoa completa: imagem renderizável + voz tocável.

    Entradas que só têm prompt/metadata (a "inspiração") ficam fora da galeria.
    """
    image = creator.get("image_uri") or creator.get("image") or creator.get("upscaled_base")
    has_image = (
        isinstance(image, str)
        and bool(image)
        and _is_renderable_uri(image)
        and _media_type_for_uri(image) == "image"
    )
    return has_image and _playable_voice_uri(creator) is not None


def _creator_id(creator: dict[str, Any]) -> Optional[str]:
    raw = creator.get("id") or creator.get("creator_id")
    return str(raw) if raw is not None else None


def _artifact_dict(art: Any) -> Optional[dict[str, Any]]:
    """Normaliza um Artifact (model ou dict) para o contrato público da UI."""
    if hasattr(art, "model_dump"):
        art = art.model_dump()
    return _normalize_artifact(art)


def _extract_artifacts(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Lista clips, locução e montagem final com kind e uri."""
    arts: list[dict[str, Any]] = []
    for clip in item.get("clips", []) or []:
        norm = _artifact_dict(clip)
        if norm:
            arts.append(norm)
    voiceover = _artifact_dict(item.get("voiceover"))
    if voiceover:
        arts.append(voiceover)
    final = _artifact_dict(item.get("assembled"))
    if final:
        arts.append(final)
    return arts


def _normalize_qc(qc: Any) -> Optional[dict[str, Any]]:
    qc = _to_plain(qc)
    if not isinstance(qc, dict):
        return None
    return {
        "passed": bool(qc.get("passed")),
        "score": qc.get("score"),
        "reasons": list(qc.get("reasons") or []),
    }


def _snapshot_from_item(item: Any) -> dict[str, Any]:
    item = _to_plain(item)
    if not isinstance(item, dict):
        return {}
    snap: dict[str, Any] = {}
    for key in (
        "id",
        "creator_ref",
        "concept",
        "script",
        "tier",
        "attempts",
        "cost_usd",
        "dropped",
        "error",
        "failure",
    ):
        if key in item:
            snap[key] = _safe_serialize(item[key])
    if item.get("qc") is not None:
        snap["qc"] = _normalize_qc(item["qc"])
    artifacts = _extract_artifacts(item)
    if artifacts:
        snap["artifacts"] = artifacts
    assembled = _normalize_artifact(item.get("assembled"))
    if assembled:
        snap["assembled"] = assembled
    return snap


def _merge_artifacts(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for art in (existing or []) + (incoming or []):
        key = (str(art.get("kind")), str(art.get("uri")))
        if art.get("uri") and key not in seen:
            merged.append(art)
            seen.add(key)
    return merged


def _merge_item_snapshot(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = {**base, **{k: v for k, v in incoming.items() if k != "artifacts"}}
    if "artifacts" in base or "artifacts" in incoming:
        merged["artifacts"] = _merge_artifacts(base.get("artifacts"), incoming.get("artifacts"))
    return merged


def _item_id_from(data: dict[str, Any], current: dict[str, Any]) -> Optional[str]:
    for candidate in (data.get("input"), data.get("output"), current):
        plain = _to_plain(candidate)
        if isinstance(plain, dict) and plain.get("id"):
            return str(plain["id"])
    output = _to_plain(data.get("output"))
    if isinstance(output, dict):
        results = output.get("results") or []
        if results:
            item = _to_plain(results[-1])
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
    return None


def _complete_item_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot.get("id"),
        "creator_ref": snapshot.get("creator_ref"),
        "concept": snapshot.get("concept") or {},
        "script": snapshot.get("script"),
        "tier": snapshot.get("tier"),
        "attempts": snapshot.get("attempts", 0),
        "cost_usd": snapshot.get("cost_usd", 0.0),
        "qc": snapshot.get("qc"),
        "artifacts": snapshot.get("artifacts") or [],
        "assembled": snapshot.get("assembled"),
        "dropped": snapshot.get("dropped", False),
        "error": snapshot.get("error"),
        "failure": snapshot.get("failure"),
    }


def _item_payload_from_result(item: Any) -> dict[str, Any]:
    return _safe_serialize(_complete_item_payload(_snapshot_from_item(item)))


def _runtime_phase(
    state: dict[str, Any] | None,
    summary: dict[str, Any] | None,
) -> str:
    if state is not None:
        # Falha de run inteiro vence tudo: o `finally` marca done=True mesmo num
        # crash, então o erro precisa ser checado antes para não virar "done".
        if state.get("error"):
            return "error"
        concept_future = state.get("concept_edit")
        if concept_future is not None and not getattr(concept_future, "done", lambda: False)():
            return "editing"
        review_future = state.get("review")
        if review_future is not None and not getattr(review_future, "done", lambda: False)():
            return "review"
        approval_future = state.get("approval")
        if approval_future is not None and not getattr(approval_future, "done", lambda: False)():
            return "awaiting"
        if state.get("done"):
            return "done"
        return "running"
    if summary is None:
        return "idle"
    if int(summary.get("in_flight") or 0) > 0:
        return "running"
    return "done"


def _build_item_update(
    run_id: str,
    node: str,
    data: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
    *,
    topology: PipelineTopology = DEFAULT_TOPOLOGY,
) -> Optional[dict[str, Any]]:
    """Cria ``item_update`` incremental a partir de um ``node_end`` LangGraph."""
    if node not in topology.item_update_nodes:
        return None
    output = _to_plain(data.get("output"))
    if node == "process_item" and isinstance(output, dict):
        results = output.get("results") or []
        if not results:
            return None
        incoming = _snapshot_from_item(results[-1])
    else:
        current = _snapshot_from_item(data.get("input"))
        item_id = _item_id_from(data, current)
        if not item_id:
            return None
        incoming = _merge_item_snapshot(current, _snapshot_from_item(output))
        incoming["id"] = item_id

    item_id = str(incoming.get("id") or "")
    if not item_id:
        return None
    previous = snapshots.get(item_id, {})
    snapshot = _merge_item_snapshot(previous, incoming)
    snapshots[item_id] = snapshot
    return {
        "type": "item_update",
        "run_id": run_id,
        "node": node,
        "label": topology.node_labels.get(node, node),
        "item": _safe_serialize(_complete_item_payload(snapshot)),
    }


def _safe_serialize(obj: Any, depth: int = 0) -> Any:
    """Serializa de forma segura objetos do estado para JSON."""
    if depth > 3:
        return str(obj)
    if isinstance(obj, dict):
        return {k: _safe_serialize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_serialize(i, depth + 1) for i in obj[:20]]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)
