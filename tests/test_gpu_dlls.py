import os
from pathlib import Path

import pytest

from ns_common import gpu_dlls


def _make(site: Path, *parts: str) -> Path:
    d = site.joinpath(*parts)
    d.mkdir(parents=True)
    return d


def test_ensure_on_path_prepends_existing_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cublas = _make(tmp_path, "nvidia", "cublas", "bin")
    cudnn = _make(tmp_path, "nvidia", "cudnn", "bin")
    monkeypatch.setenv("PATH", "C:\existing")
    added = gpu_dlls.ensure_on_path(tmp_path)
    assert added == [cublas, cudnn]
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[:3] == [str(cublas), str(cudnn), "C:\existing"]


def test_ensure_on_path_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make(tmp_path, "nvidia", "cublas", "bin")
    monkeypatch.setenv("PATH", "C:\existing")
    assert len(gpu_dlls.ensure_on_path(tmp_path)) == 1
    before = os.environ["PATH"]
    assert gpu_dlls.ensure_on_path(tmp_path) == []
    assert os.environ["PATH"] == before


def test_ensure_on_path_skips_missing_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make(tmp_path, "nvidia", "cudnn", "bin")
    monkeypatch.setenv("PATH", "C:\existing")
    added = gpu_dlls.ensure_on_path(tmp_path)
    assert [p.parts[-2] for p in added] == ["cudnn"]


def test_ensure_on_path_without_wheels_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "C:\existing")
    assert gpu_dlls.ensure_on_path(tmp_path) == []
    assert os.environ["PATH"] == "C:\existing"
