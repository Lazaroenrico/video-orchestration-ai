"""QC live de integridade para artefatos de mídia reais.

Este adapter não tenta julgar qualidade criativa. Ele só bloqueia saídas que ainda
sejam mock/fallback ou que não tenham mídia de vídeo persistível antes da montagem.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlparse

from orchestrator.graph.state import Item, QCResult
from orchestrator.tracing import traced

_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")


class IntegrityQCAdapter:
    """Valida que o item aprovado contém clips reais e usáveis."""

    def __init__(self, required_clip_count: int = 2) -> None:
        self.required_clip_count = required_clip_count

    @traced("adapter.integrity_qc.qc_check", run_type="tool", step=7, provider="integrity_qc")
    async def qc_check(self, item: Item, fail_rate: float = 0.0) -> QCResult:
        reasons: list[str] = []
        clips = list(item.clips or [])
        if len(clips) < self.required_clip_count:
            reasons.append(f"missing_clips:{len(clips)}/{self.required_clip_count}")

        for idx, clip in enumerate(clips):
            prefix = f"clip_{idx}"
            if clip.kind != "clip":
                reasons.append(f"{prefix}_invalid_kind:{clip.kind}")
            if not _is_video_uri(clip.uri):
                reasons.append(f"{prefix}_invalid_video_uri")

            provider = str(clip.meta.get("provider") or "").strip().lower()
            if provider == "mock":
                reasons.append(f"{prefix}_mock_provider")

            fallback = clip.meta.get("fallback_reason")
            if fallback:
                reasons.append(f"{prefix}_fallback_reason:{fallback}")

        return QCResult(
            passed=not reasons,
            score=0.0 if reasons else 1.0,
            reasons=reasons,
        )


def _is_video_uri(uri: str) -> bool:
    if not uri:
        return False
    lowered = uri.lower()
    if lowered.startswith("data:video/"):
        return True
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https"}:
        # URLs de entrega (ex.: replicate.delivery) muitas vezes não têm extensão
        # no path; só reprovamos quando HÁ extensão e ela não é de vídeo. A
        # detecção de mock/fallback fica com meta.provider/fallback_reason.
        suffix = PurePosixPath(parsed.path).suffix.lower()
        return not suffix or suffix in _VIDEO_EXTENSIONS
    if parsed.scheme:
        return False
    return parsed.path.lower().endswith(_VIDEO_EXTENSIONS)


def build_integrity_qc_adapter(pipeline: dict) -> IntegrityQCAdapter:
    qc_cfg = pipeline.get("qc", {})
    return IntegrityQCAdapter(
        required_clip_count=int(qc_cfg.get("required_clip_count", 2))
    )
