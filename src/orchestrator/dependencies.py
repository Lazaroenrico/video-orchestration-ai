"""Single dependency graph for one pipeline run."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.agent_catalog import AgentCatalog, default_agent_catalog
from orchestrator.config import default_artifacts_db_path, default_media_path, default_videos_path
from orchestrator.language_runtime import LanguageRuntime
from orchestrator.registry import build_adapter_from_providers
from orchestrator.storage.db import ArtifactDB, ArtifactRepository
from orchestrator.storage.factory import build_media_storage


@dataclass(frozen=True)
class RunDependencies:
    """All collaborators shared by API, local runner, and durable worker.

    ``LanguageRuntime`` is deliberately separate from ``adapter``: the latter
    contains only domain/media providers, while all model credentials and native
    LangChain agents live in the former.
    """

    language_runtime: LanguageRuntime
    adapter: Any
    pipeline: dict[str, Any]
    providers: dict[str, Any]
    agent_catalog: AgentCatalog
    media_storage: Any
    videos_storage: Any
    artifact_repository: ArtifactRepository
    effect_ledger: Any | None = None
    durable: bool = False

    @classmethod
    def build(
        cls,
        pipeline: dict[str, Any],
        providers: dict[str, Any],
        *,
        agent_catalog: AgentCatalog | None = None,
        artifact_repository: ArtifactRepository | None = None,
        effect_ledger: Any | None = None,
        durable: bool = False,
    ) -> "RunDependencies":
        names = providers.get("adapters", {})
        language_provider = str(names.get("llm", "mock"))
        # Resolve language credentials first.  Invalid provider/auth fails before
        # any paid domain adapter or effect can be instantiated.
        language_runtime = LanguageRuntime.from_provider(language_provider, pipeline)
        if language_provider != "mock":
            language_runtime.model_for("bootstrap")

        adapter = build_adapter_from_providers(providers, pipeline)
        repository = artifact_repository
        if repository is None:
            repository = ArtifactDB(default_artifacts_db_path())
            repository.setup()
        return cls(
            language_runtime=language_runtime,
            adapter=adapter,
            pipeline=pipeline,
            providers=providers,
            agent_catalog=agent_catalog or default_agent_catalog(),
            media_storage=build_media_storage(
                providers, root=default_media_path(), web_prefix="/media"
            ),
            videos_storage=build_media_storage(
                providers, root=default_videos_path(), web_prefix="/videos"
            ),
            artifact_repository=repository,
            effect_ledger=effect_ledger,
            durable=durable,
        )

    def configurable(self, *, run_id: str, platform: str, run_options: dict[str, Any] | None = None) -> dict[str, Any]:
        run = {"platform": platform}
        run.update(run_options or {})
        return {
            "adapter": self.adapter,
            "language_runtime": self.language_runtime,
            "pipeline": self.pipeline,
            "providers": self.providers,
            "agent_catalog": self.agent_catalog,
            "run": run,
            "thread_id": run_id,
            "media_storage": self.media_storage,
            "videos_storage": self.videos_storage,
            "artifact_db": self.artifact_repository,
            "effect_ledger": self.effect_ledger,
            "durable": self.durable,
        }


__all__ = ["RunDependencies"]
