"""The studio extra caps faster-whisper and PyAV together (faster-whisper 1.2.x breaks on PyAV 19)."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def studio_requirements() -> dict[str, Requirement]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    reqs = [Requirement(r) for r in data["project"]["optional-dependencies"]["studio"]]
    return {r.name.lower(): r for r in reqs}


def test_studio_extra_caps_av_below_19():
    av = studio_requirements()["av"]
    assert av.specifier.contains(Version("18.9"))
    assert not av.specifier.contains(Version("19.0.0"))
    assert not av.specifier.contains(Version("19.0.2"))


def test_studio_extra_caps_faster_whisper():
    fw = studio_requirements()["faster-whisper"]
    assert fw.specifier.contains(Version("1.2.1"))
    assert not fw.specifier.contains(Version("1.3.0"))


def test_every_studio_venv_installs_the_studio_extra():
    # s04, the setup-venv bootstrap step and `ns update` all take their extras from this table
    from ns_common.venv_extras import VENV_EXTRAS

    assert "studio" in VENV_EXTRAS["studio"]
