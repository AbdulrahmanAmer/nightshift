from pathlib import Path

import pytest

from ns_common import config as c
from ns_common.errors import NsError
from ns_common.models import Channel, InstallState, Runtime
from ns_common.paths import Paths


def test_runtime_roundtrip(tmp_home: Path):
    p = Paths.from_env()
    p.ensure()
    with pytest.raises(NsError) as ei:
        c.load_runtime(p)
    assert ei.value.code == "NS-E203"
    c.save_runtime(p, Runtime.default("cpu-dev"))
    assert c.load_runtime(p).video.encoder == "libx264"


def test_channel_roundtrip_and_list(tmp_home: Path):
    p = Paths.from_env()
    p.ensure()
    ch = Channel(slug="after-dark", display_name="After Dark", niche="horror", language="en")
    f = c.save_channel(p, ch)
    assert f.name == "after-dark.yaml"
    text = f.read_text(encoding="utf-8")
    assert text.startswith("slug: after-dark")          # insertion order kept
    assert "long_minutes:\n- 10\n- 14" in text or "long_minutes: [10, 14]" in text
    assert c.load_channel(p, "after-dark") == ch
    c.save_channel(p, ch.model_copy(update={"slug": "calm-tales"}))
    assert c.list_channels(p) == ["after-dark", "calm-tales"]


def test_install_state_default_and_save(tmp_home: Path):
    p = Paths.from_env()
    p.ensure()
    assert c.load_install_state(p) == InstallState()
    st = InstallState(app_path="D:/NightShift")
    c.save_install_state(p, st)
    assert c.load_install_state(p).app_path == "D:/NightShift"


def test_load_manifest_from_repo():
    mf = c.load_manifest()
    assert "gpu" in mf.profiles and "cpu-dev" in mf.profiles
