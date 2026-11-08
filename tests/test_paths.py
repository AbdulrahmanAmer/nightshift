from pathlib import Path

from ns_common.paths import Paths, contracts_dir, repo_root


def test_from_env_uses_nightshift_home(tmp_home: Path):
    p = Paths.from_env()
    assert p.home == tmp_home
    assert p.models_llm == tmp_home / "models" / "llm"
    assert p.runtime_file == tmp_home / "config" / "runtime.json"
    assert p.channel_file("after-dark") == tmp_home / "config" / "channels" / "after-dark.yaml"
    assert p.job_dir("after-dark-20261101-ab12") == tmp_home / "data" / "jobs" / "after-dark-20261101-ab12"


def test_default_home_is_localappdata(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert Paths.from_env().home == Path(r"C:\Users\x\AppData\Local\Nightshift")


def test_ensure_creates_all_dirs(tmp_home: Path):
    p = Paths.from_env()
    p.ensure()
    for d in (p.logs, p.cache_clips, p.models_tts_voices, p.backup_dir, p.channels_dir, p.rejected,
              p.exemplars, p.prompt_overrides):      # the two the studio reads were never created before
        assert d.is_dir()
    assert not (tmp_home / "stories").exists()        # nothing ever wrote there (outbox\<slug>\stories does)


def test_repo_root_contains_contracts():
    assert (repo_root() / "pyproject.toml").exists()
    assert contracts_dir().name == "contracts"


def test_new_folders_and_files(tmp_home: Path):
    p = Paths.from_env()
    assert p.bin_uv == tmp_home / "bin" / "uv"
    assert p.bin_qwentts == tmp_home / "bin" / "qwentts"
    assert p.models_cv == tmp_home / "models" / "cv"
    assert p.formats_dir == tmp_home / "config" / "formats"
    assert not hasattr(p, "stories") and not hasattr(p, "clips_index")    # removed: nothing read them
    assert p.assistant_db == tmp_home / "data" / "assistant.db"
    p.ensure()
    for d in (p.bin_uv, p.bin_qwentts, p.models_cv, p.exemplars):
        assert d.is_dir()
