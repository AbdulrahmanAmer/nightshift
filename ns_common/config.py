from __future__ import annotations

import json
from pathlib import Path

import yaml

from ns_common.errors import NsError
from ns_common.models import Channel, InstallState, Manifest, Runtime
from ns_common.paths import Paths, repo_root


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_runtime(paths: Paths) -> Runtime:
    if not paths.runtime_file.exists():
        raise NsError("NS-E203", f"{paths.runtime_file} missing; run ns tune or the installer")
    return Runtime.model_validate_json(paths.runtime_file.read_text(encoding="utf-8"))


def save_runtime(paths: Paths, rt: Runtime) -> Path:
    return _write_json(paths.runtime_file, rt.model_dump(mode="json"))


def load_channel(paths: Paths, slug: str) -> Channel:
    f = paths.channel_file(slug)
    if not f.exists():
        raise FileNotFoundError(f)
    return Channel.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))


def save_channel(paths: Paths, ch: Channel) -> Path:
    f = paths.channel_file(ch.slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        yaml.safe_dump(ch.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return f


def list_channels(paths: Paths) -> list[str]:
    if not paths.channels_dir.exists():
        return []
    return sorted(p.stem for p in paths.channels_dir.glob("*.yaml"))


def load_install_state(paths: Paths) -> InstallState:
    if not paths.install_state_file.exists():
        return InstallState()
    return InstallState.model_validate_json(paths.install_state_file.read_text(encoding="utf-8"))


def save_install_state(paths: Paths, st: InstallState) -> Path:
    return _write_json(paths.install_state_file, st.model_dump(mode="json"))


def load_manifest(path: Path | None = None) -> Manifest:
    f = path or (repo_root() / "manifest.json")
    return Manifest.model_validate_json(f.read_text(encoding="utf-8"))
