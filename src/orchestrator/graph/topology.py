"""Fonte única de verdade da topologia de nodes da pipeline (V2).

O builder (``graph/builder.py``) registra os nodes; este módulo declara os metadados
canônicos de cada node (nível do grafo, estágio público, rótulo, participação em
item updates) e deriva **todas** as visões consumidas por REST/SSE/progresso:

- ``PIPELINE_NODES``/``NODE_LABELS`` → ``web/server.py`` (emissão de node_start/
  node_end e rótulos de SSE);
- ``NODE_STAGE``/``ITEM_PROGRESS_NODES``/``TERMINAL_NODE``/``STAGES`` →
  ``orchestrator.progress`` (read model de progresso e timeline).

Adicionar um novo node exige editar apenas o registro no builder e a tabela
``NODE_SPECS`` abaixo (um lugar para metadados). ``validate_registrations`` é
executada pelo builder em tempo de construção do grafo e falha rápido se os
registros e esta tabela divergirem.

Nomes ``graph="legacy"`` nunca são registrados pelo builder atual; existem só para
replay de runs antigos/importados (ex.: o node ``script`` do inventário V1), que
precisam continuar passando pelos mesmos gates de SSE.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = [
    "DEFAULT_TOPOLOGY",
    "DEFAULT_TIERS",
    "ITEM_PROGRESS_NODES",
    "ITEM_UPDATE_NODES",
    "NODE_LABELS",
    "NODE_SPECS",
    "NODE_STAGE",
    "PIPELINE_NODES",
    "PipelineTopology",
    "STAGES",
    "TALKING_HEAD_NODES",
    "TERMINAL_NODE",
    "TopologyError",
    "stage_of",
    "validate_registrations",
    "validate_topology",
    "topology_for_tiers",
]


# --------------------------------------------------------------------------- #
# Estágios públicos de produto (ordem canônica usada em REST/SSE/front)
# --------------------------------------------------------------------------- #

STAGES: tuple[dict[str, Any], ...] = (
    {"id": "setup", "label": "Configuração", "parent_id": None},
    {"id": "creative_plan", "label": "Plano criativo", "parent_id": None},
    {"id": "concepts", "label": "Criando conceitos", "parent_id": "creative_plan"},
    {"id": "scripts", "label": "Escrevendo roteiros", "parent_id": "creative_plan"},
    {
        "id": "creator_profiles",
        "label": "Definindo creators",
        "parent_id": "creative_plan",
    },
    {
        "id": "creator_previews",
        "label": "Gerando previews",
        "parent_id": "creative_plan",
    },
    {"id": "review", "label": "Revisão", "parent_id": None},
    {"id": "production", "label": "Produção e QC", "parent_id": None},
    {
        "id": "talking_head",
        "label": "Talking-head",
        "parent_id": "production",
    },
    {
        "id": "product_demo",
        "label": "Product demo",
        "parent_id": "production",
    },
    {"id": "qc", "label": "Controle de qualidade", "parent_id": "production"},
    {"id": "assembly", "label": "Montagem", "parent_id": None},
)

_STAGE_IDS = frozenset(stage["id"] for stage in STAGES)


@dataclass(frozen=True)
class NodeSpec:
    """Metadados canônicos de um node da pipeline.

    Atributos:

    - ``graph``: onde o node vive — ``batch`` (grafo de topo), ``item`` (subgrafo
      per-item) ou ``legacy`` (nome aceito só em replay de runs antigos).
    - ``stage``: estágio público de produto (id de :data:`STAGES`) ou ``None``
      quando o node não vira evento de progresso.
    - ``label``: rótulo humano exposto em SSE; ``None`` significa que o node é
      interno e NÃO emite ``node_start``/``node_end`` nem recebe rótulo.
    - ``tracks_item``: alimenta a identidade de item no read model de progresso
      (``progress._ITEM_NODES``).
    - ``updates_item``: gera snapshots incrementais de ``item_update`` no servidor
      (``web/server.ITEM_UPDATE_NODES``).
    """

    name: str
    graph: str
    stage: str | None
    label: str | None
    tracks_item: bool = False
    updates_item: bool = False


NODE_SPECS: tuple[NodeSpec, ...] = (
    # Grafo de topo, na ordem de registro do builder.
    NodeSpec("concepts", "batch", "concepts", "Conceitos"),
    NodeSpec("scripts", "batch", "scripts", "Scripts"),
    NodeSpec("creator_profiles", "batch", "creator_profiles", "Perfis de creators"),
    NodeSpec("roster", "batch", "creator_previews", "Previews de creators"),
    # Interno: materializa previews de voz entre roster e review; não emite
    # node_start/node_end nem vira estágio público.
    NodeSpec("voice_candidates", "batch", None, None),
    NodeSpec("review", "batch", "review", "Revisão do plano criativo"),
    NodeSpec("finalize_voices", "batch", None, None),
    NodeSpec("process_item", "batch", "production", "Item", tracks_item=True, updates_item=True),
    NodeSpec("feedback", "batch", None, "Feedback"),
    # Subgrafo per-item (os tiers são dinâmicos por config; defaults abaixo).
    NodeSpec("voiceover", "item", "assembly", None, tracks_item=True),
    NodeSpec("ltx", "item", "talking_head", "Talking-Head (LTX)", True, True),
    NodeSpec("kling", "item", "talking_head", "Talking-Head (Kling)", True, True),
    NodeSpec("seedance", "item", "talking_head", "Talking-Head (Seedance)", True, True),
    NodeSpec("product_demo", "item", "product_demo", "Product Demo", True, True),
    NodeSpec("qc", "item", "qc", "QC", True, True),
    NodeSpec("assembly", "item", "assembly", "Montagem", True, True),
    NodeSpec("upscale", "item", "assembly", "Upscale (vídeo)", True, True),
    NodeSpec("drop", "item", "qc", "Descartado", True, True),
    # Nome legado (runs V1 importados): nunca registrado pelo builder atual.
    NodeSpec("script", "legacy", None, "Script", updates_item=True),
)

#: Tiers padrão da pipeline (config-base/pipeline.yaml).
DEFAULT_TIERS: tuple[str, ...] = ("ltx", "kling", "seedance")


class TopologyError(RuntimeError):
    """Topologia divergente entre registros do builder e metadados canônicos."""


#: Node cuja conclusão marca o estágio público como completo (exceto
#: ``talking_head``, completado por qualquer tier — ver :data:`TALKING_HEAD_NODES`).
TERMINAL_NODE: dict[str, str] = {
    "concepts": "concepts",
    "scripts": "scripts",
    "creator_profiles": "creator_profiles",
    "creator_previews": "roster",
    "review": "review",
    "production": "process_item",
    "product_demo": "product_demo",
    "qc": "qc",
    "assembly": "upscale",
}


@dataclass(frozen=True)
class PipelineTopology:
    """Visão imutável da topologia para uma configuração de tiers."""

    node_specs: tuple[NodeSpec, ...]
    stages: tuple[dict[str, Any], ...]
    pipeline_nodes: frozenset[str]
    node_labels: MappingProxyType
    node_stage: MappingProxyType
    item_progress_nodes: frozenset[str]
    item_update_nodes: frozenset[str]
    terminal_node: MappingProxyType
    talking_head_nodes: frozenset[str]

    @property
    def specs(self) -> tuple[NodeSpec, ...]:
        return self.node_specs

    def stage_of(self, node: str) -> str:
        """Estágio público do node; levanta ``KeyError`` para desconhecido."""
        return self.node_stage[node]

    def validate_topology(self) -> list[str]:
        """Checagem de consistência interna desta visão."""
        problems: list[str] = []
        specs_by_name: dict[str, NodeSpec] = {}
        for spec in self.node_specs:
            if spec.name in specs_by_name:
                problems.append(f"node duplicado na topologia: {spec.name!r}")
            specs_by_name[spec.name] = spec
            if spec.graph not in {"batch", "item", "legacy"}:
                problems.append(f"{spec.name}: nível de grafo inválido {spec.graph!r}")
            if spec.stage is not None and spec.stage not in _STAGE_IDS:
                problems.append(f"{spec.name}: estágio desconhecido {spec.stage!r}")
            if spec.label is not None and not spec.label.strip():
                problems.append(f"{spec.name}: rótulo público vazio")
            if (
                (spec.tracks_item or spec.updates_item)
                and spec.graph == "legacy"
                and spec.stage is not None
            ):
                problems.append(f"{spec.name}: node legado não deveria mapear estágio novo")

        for stage_id, terminal in self.terminal_node.items():
            if stage_id not in _STAGE_IDS:
                problems.append(f"TERMINAL_NODE: estágio desconhecido {stage_id!r}")
            if self.node_stage.get(terminal) != stage_id:
                problems.append(
                    f"TERMINAL_NODE[{stage_id!r}]={terminal!r} não mapeia de volta ao estágio"
                )
        covered = set(self.terminal_node) | {"talking_head", "setup"}
        for stage in self.stages:
            has_children = any(other["parent_id"] == stage["id"] for other in self.stages)
            if not has_children and stage["id"] not in covered:
                problems.append(f"estágio folha {stage['id']!r} sem node terminal declarado")
        for tier in self.talking_head_nodes:
            if self.node_stage.get(tier) != "talking_head":
                problems.append(f"tier {tier!r} não mapeia para 'talking_head'")
        return problems

    def validate_registrations(
        self,
        batch_nodes: set[str] | frozenset[str] | None = None,
        item_nodes: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Confere registros de builder com esta visão runtime."""
        problems: list[str] = []
        registered = set(batch_nodes or ())
        item_registered = set(item_nodes or ())
        specs_by_name = {spec.name: spec for spec in self.node_specs}
        unknown = (registered | item_registered) - specs_by_name.keys()
        for name in sorted(unknown):
            problems.append(
                f"node {name!r} registrado no grafo mas ausente de graph/topology.py "
                "(adicione um NodeSpec)"
            )
        if batch_nodes is not None:
            missing_batch = sorted(
                spec.name
                for spec in self.node_specs
                if spec.graph == "batch" and spec.name not in registered
            )
            for name in missing_batch:
                problems.append(f"node {name!r} declarado como 'batch' mas não registrado no grafo")
        if item_nodes is not None:
            required_item = {spec.name for spec in self.node_specs if spec.graph == "item"}
            for name in sorted(required_item - item_registered):
                problems.append(
                    f"node {name!r} declarado como 'item' mas não registrado no subgrafo"
                )
        if problems:
            raise TopologyError("topologia divergente:\n" + "\n".join(problems))


def _build_specs(specs: tuple[NodeSpec, ...]) -> dict[str, NodeSpec]:
    result: dict[str, NodeSpec] = {}
    for spec in specs:
        if spec.name in result:
            raise TopologyError(f"node duplicado na topologia: {spec.name!r}")
        result[spec.name] = spec
    return result


def _make_topology(specs: tuple[NodeSpec, ...], tiers: tuple[str, ...]) -> PipelineTopology:
    node_labels = {spec.name: spec.label for spec in specs if spec.label is not None}
    node_stage = {spec.name: spec.stage for spec in specs if spec.stage is not None}
    return PipelineTopology(
        node_specs=specs,
        stages=STAGES,
        pipeline_nodes=frozenset(node_labels),
        node_labels=MappingProxyType(node_labels),
        node_stage=MappingProxyType(node_stage),
        item_progress_nodes=frozenset(spec.name for spec in specs if spec.tracks_item),
        item_update_nodes=frozenset(spec.name for spec in specs if spec.updates_item),
        terminal_node=MappingProxyType(dict(TERMINAL_NODE)),
        talking_head_nodes=frozenset(tiers),
    )


_BASE_SPECS_BY_NAME = _build_specs(NODE_SPECS)


def topology_for_tiers(tiers: list[str] | tuple[str, ...]) -> PipelineTopology:
    """Resolve uma topologia para exatamente os tiers configurados."""
    configured = tuple(str(tier) for tier in tiers)
    if len(set(configured)) != len(configured):
        raise TopologyError("tiers duplicados na configuração")
    fixed_names = set(_BASE_SPECS_BY_NAME) - set(DEFAULT_TIERS)
    conflicts = fixed_names & set(configured)
    if conflicts:
        raise TopologyError(f"tier conflita com node fixo: {sorted(conflicts)!r}")
    specs = tuple(
        _BASE_SPECS_BY_NAME[spec.name] for spec in NODE_SPECS if spec.name not in DEFAULT_TIERS
    )
    tier_specs = tuple(
        _BASE_SPECS_BY_NAME[tier]
        if tier in _BASE_SPECS_BY_NAME
        else NodeSpec(tier, "item", "talking_head", f"Talking-Head ({tier})", True, True)
        for tier in configured
    )
    return _make_topology(specs + tier_specs, configured)


DEFAULT_TOPOLOGY = _make_topology(NODE_SPECS, DEFAULT_TIERS)

# Aliases de compatibilidade: consumidores antigos seguem vendo a topologia default.
PIPELINE_NODES = DEFAULT_TOPOLOGY.pipeline_nodes
NODE_LABELS = DEFAULT_TOPOLOGY.node_labels
NODE_STAGE = DEFAULT_TOPOLOGY.node_stage
ITEM_PROGRESS_NODES = DEFAULT_TOPOLOGY.item_progress_nodes
ITEM_UPDATE_NODES = DEFAULT_TOPOLOGY.item_update_nodes
TALKING_HEAD_NODES = DEFAULT_TOPOLOGY.talking_head_nodes
TERMINAL_NODE = DEFAULT_TOPOLOGY.terminal_node


def stage_of(node: str) -> str:
    return DEFAULT_TOPOLOGY.stage_of(node)


def validate_topology() -> list[str]:
    return DEFAULT_TOPOLOGY.validate_topology()


def validate_registrations(
    batch_nodes: set[str] | frozenset[str] | None = None,
    item_nodes: set[str] | frozenset[str] | None = None,
    tiers: list[str] | tuple[str, ...] = DEFAULT_TIERS,
) -> None:
    topology_for_tiers(tiers).validate_registrations(batch_nodes, item_nodes)
