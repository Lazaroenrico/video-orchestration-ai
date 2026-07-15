"""Helpers comuns aos nodes."""
from __future__ import annotations

from typing import Any

from orchestrator.graph.state import Item


def get_pipeline(config: dict[str, Any]) -> dict[str, Any]:
    """Knobs da pipeline (pipeline.yaml) injetados via config."""
    return config["configurable"]["pipeline"]


def tier_names(pipeline: dict[str, Any]) -> list[str]:
    return [t["name"] for t in pipeline["tiers"]]


def as_item(state: Any) -> Item:
    """O estado do subgrafo pode chegar como Item (pydantic) ou dict."""
    return state if isinstance(state, Item) else Item.model_validate(state)
