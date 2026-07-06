from pathlib import Path

from orchestrator.config import default_videos_path


def test_default_videos_path_uses_orch_videos_env(monkeypatch):
    monkeypatch.setenv("ORCH_VIDEOS", "/tmp/orch-videos")

    assert default_videos_path() == Path("/tmp/orch-videos")


def test_default_videos_path_falls_back_to_orchestrator_videos(monkeypatch):
    monkeypatch.delenv("ORCH_VIDEOS", raising=False)

    assert default_videos_path() == Path(".orchestrator/videos")
