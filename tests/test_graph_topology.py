"""Fonte única de verdade da topologia de nodes (builder → progress/server).

O inventário de nodes não pode ficar triplicado (builder, server, progress): estes
testas garantem que ``orchestrator.graph.topology`` é o único lugar onde a topologia
é declarada e que as visões de progress.py/web/server.py são derivadas dela.
"""

from __future__ import annotations

import pytest

from orchestrator.graph import topology
from orchestrator.graph.builder import build_graph, build_item_graph


def test_runtime_topology_includes_configured_custom_tier_only():
    runtime = topology.topology_for_tiers(["pruna"])

    assert runtime.node_stage["pruna"] == "talking_head"
    assert runtime.node_labels["pruna"] == "Talking-Head (pruna)"
    assert "pruna" in runtime.item_progress_nodes
    assert "pruna" in runtime.item_update_nodes
    assert "ltx" not in runtime.node_stage
    assert "kling" not in runtime.node_labels
    assert "seedance" not in runtime.item_progress_nodes


def _registered_names(app) -> set[str]:
    return {name for name in app.get_graph().nodes if not name.startswith("__")}


# --------------------------------------------------------------------------- #
# Módulo canônico existe e deriva as visões públicas
# --------------------------------------------------------------------------- #


def test_topology_module_exports_canonical_views():
    for name in (
        "NODE_SPECS",
        "STAGES",
        "NODE_STAGE",
        "PIPELINE_NODES",
        "NODE_LABELS",
        "ITEM_PROGRESS_NODES",
        "ITEM_UPDATE_NODES",
        "TERMINAL_NODE",
        "TALKING_HEAD_NODES",
        "stage_of",
        "validate_topology",
        "validate_registrations",
    ):
        assert hasattr(topology, name), f"topology não exporta {name}"


def test_progress_views_are_derived_from_topology_not_copied():
    from orchestrator import progress

    assert progress.STAGES is topology.STAGES
    assert progress.NODE_STAGE is topology.NODE_STAGE
    assert progress._ITEM_NODES is topology.ITEM_PROGRESS_NODES
    assert progress._TERMINAL_NODE is topology.TERMINAL_NODE


def test_server_views_are_derived_from_topology_not_copied():
    from orchestrator.web import server

    assert server.PIPELINE_NODES is topology.PIPELINE_NODES
    assert server.ITEM_UPDATE_NODES is topology.ITEM_UPDATE_NODES
    assert server.NODE_LABELS is topology.NODE_LABELS


def test_stage_of_raises_for_unknown_node():
    with pytest.raises(KeyError):
        topology.stage_of("node-que-nao-existe")
    assert topology.stage_of("qc") == "qc"


# --------------------------------------------------------------------------- #
# Cobertura dos registros reais do builder
# --------------------------------------------------------------------------- #


def test_builder_registrations_are_covered_by_topology(pipeline_cfg):
    batch_nodes = _registered_names(build_graph(pipeline_cfg))
    item_nodes = _registered_names(build_item_graph(pipeline_cfg))
    tiers = [t["name"] for t in pipeline_cfg["tiers"]]

    # Não levanta: todo node registrado pelo builder é conhecido pela topologia.
    topology.validate_registrations(batch_nodes, item_nodes, tiers)


def test_registered_nodes_match_specs_exactly_per_graph_level(pipeline_cfg):
    tiers = {t["name"] for t in pipeline_cfg["tiers"]}
    batch_nodes = _registered_names(build_graph(pipeline_cfg))
    item_nodes = _registered_names(build_item_graph(pipeline_cfg))

    assert batch_nodes == {s.name for s in topology.NODE_SPECS if s.graph == "batch"}
    assert item_nodes == ({s.name for s in topology.NODE_SPECS if s.graph == "item"} | tiers)


def test_internal_batch_nodes_stay_silent_in_public_views():
    # Nodes internos do grafo de topo não emitem progresso nem rótulo de SSE.
    # ``voice_candidates`` já existia no builder, mas faltava na tabela canônica —
    # a validação no build passou a falhar rápido quando o inventário diverge.
    silent = {
        s.name
        for s in topology.NODE_SPECS
        if s.graph == "batch" and s.stage is None and s.label is None
    }
    assert {"finalize_voices", "voice_candidates"} <= silent
    assert not (silent & set(topology.PIPELINE_NODES))
    assert not (silent & set(topology.NODE_STAGE))
    assert not (silent & set(topology.NODE_LABELS))
    # ``feedback`` emite rótulo de SSE, mas não vira estágio público.
    feedback_spec = next(s for s in topology.NODE_SPECS if s.name == "feedback")
    assert feedback_spec.stage is None
    assert topology.NODE_LABELS["feedback"] == "Feedback"


def test_validate_registrations_rejects_unknown_node(pipeline_cfg):
    batch_nodes = _registered_names(build_graph(pipeline_cfg)) | {"node_fantasma"}
    item_nodes = _registered_names(build_item_graph(pipeline_cfg))

    with pytest.raises(topology.TopologyError, match="node_fantasma"):
        topology.validate_registrations(batch_nodes, item_nodes, ["ltx", "kling", "seedance"])


def test_validate_registrations_accepts_configured_custom_tier(pipeline_cfg):
    batch_nodes = _registered_names(build_graph(pipeline_cfg))
    item_nodes = _registered_names(build_item_graph({**pipeline_cfg, "tiers": [{"name": "pruna"}]}))

    topology.validate_registrations(batch_nodes, item_nodes, ["pruna"])


def test_validate_registrations_rejects_unconfigured_custom_tier(pipeline_cfg):
    batch_nodes = _registered_names(build_graph(pipeline_cfg))
    item_nodes = _registered_names(build_item_graph({**pipeline_cfg, "tiers": [{"name": "pruna"}]}))

    with pytest.raises(topology.TopologyError, match="pruna"):
        topology.validate_registrations(batch_nodes, item_nodes, ["ltx"])


def test_validate_registrations_rejects_missing_registered_node(pipeline_cfg):
    batch_nodes = _registered_names(build_graph(pipeline_cfg)) - {"feedback"}
    item_nodes = _registered_names(build_item_graph(pipeline_cfg))

    with pytest.raises(topology.TopologyError, match="feedback"):
        topology.validate_registrations(batch_nodes, item_nodes, ["ltx", "kling", "seedance"])


def test_validate_registrations_requires_only_configured_tiers(pipeline_cfg):
    # Uma config pode definir um subconjunto dos tiers padrão (ex.: só ``ltx``);
    # os tiers não configurados NÃO podem ser exigidos como registrados.
    reduced = {**pipeline_cfg, "tiers": [pipeline_cfg["tiers"][0]]}
    app = build_graph(reduced)
    item_nodes = _registered_names(build_item_graph(reduced))

    assert {"kling", "seedance"}.isdisjoint(item_nodes)
    topology.validate_registrations(_registered_names(app), item_nodes, ["ltx"])


def test_validate_topology_internal_consistency():
    problems = topology.validate_topology()
    assert problems == []


# --------------------------------------------------------------------------- #
# Invariantes entre visões derivadas
# --------------------------------------------------------------------------- #


def test_labels_cover_every_public_node():
    assert set(topology.NODE_LABELS) == set(topology.PIPELINE_NODES)
    assert all(label.strip() for label in topology.NODE_LABELS.values())


def test_node_stages_reference_declared_stages():
    stage_ids = {stage["id"] for stage in topology.STAGES}
    unknown = set(topology.NODE_STAGE.values()) - stage_ids
    assert unknown == set()


def test_terminal_nodes_are_consistent_with_node_stage():
    for stage_id, terminal in topology.TERMINAL_NODE.items():
        assert topology.NODE_STAGE.get(terminal) == stage_id, (
            f"terminal {terminal!r} do estágio {stage_id!r} não mapeia de volta"
        )


def test_talking_head_tiers_share_the_stage():
    for tier in topology.TALKING_HEAD_NODES:
        assert topology.stage_of(tier) == "talking_head"


def test_item_update_membership_is_explicit_per_view():
    # A diferença histórica é intencional e agora explícita na spec:
    # voiceover alimenta o read model de progresso; script é nome legado que só
    # gera item_update em streams replayados.
    assert "voiceover" in topology.ITEM_PROGRESS_NODES
    assert "voiceover" not in topology.ITEM_UPDATE_NODES
    assert "script" in topology.ITEM_UPDATE_NODES
    assert "script" not in topology.ITEM_PROGRESS_NODES
