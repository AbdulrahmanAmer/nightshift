from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import services


def _rt(**llama_flags: object) -> Runtime:
    return Runtime.model_validate(
        {
            "llama": {"model_path": "C:/m/llm.gguf", "flags": llama_flags},
            "embed": {"model_path": "C:/m/embed.gguf"},
        }
    )


def _paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "ns")


def test_llama_command_matches_spec(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    d = services.build_service(_rt(), paths, "llama")
    assert d.cmd == [
        str(paths.bin_llama / "llama-server.exe"),
        "-m", "C:/m/llm.gguf",
        "--port", "8080",
        "--host", "127.0.0.1",
        "-ngl", "99",
        "-c", "16384",
        "--alias", "code,story",
    ]  # fmt: skip
    assert d.gpu is True and d.port == 8080
    assert d.health_url == "http://127.0.0.1:8080/health"
    assert d.variant == "default"
    assert d.env["NIGHTSHIFT_HOME"] == str(paths.home)


def test_llama_optional_flags(tmp_path: Path) -> None:
    rt = _rt(kv_type="q8_0", n_cpu_moe=8, spec_type="draft-mtp", spec_draft_n_max=3, threads=6)
    cmd = services.build_service(rt, _paths(tmp_path), "llama").cmd
    joined = " ".join(cmd)
    assert "-ctk q8_0 -ctv q8_0" in joined
    assert "--n-cpu-moe 8" in joined
    assert "--spec-type draft-mtp --spec-draft-n-max 3 -np 1" in joined
    assert "-t 6" in joined


def test_llama_plain_has_no_optional_flags(tmp_path: Path) -> None:
    cmd = services.build_service(_rt(), _paths(tmp_path), "llama").cmd
    for flag in ("-ctk", "-ctv", "--n-cpu-moe", "--spec-type", "-np", "-t"):
        assert flag not in cmd


def test_two_model_mode_variants(tmp_path: Path) -> None:
    rt = Runtime.model_validate(
        {"llama": {"model_path": "C:/m/code.gguf", "story_model_path": "C:/m/story.gguf"}}
    )
    paths = _paths(tmp_path)
    story = services.build_service(rt, paths, "llama", "story")
    code = services.build_service(rt, paths, "llama", "code")
    assert story.cmd[story.cmd.index("-m") + 1] == "C:/m/story.gguf"
    assert story.cmd[story.cmd.index("--alias") + 1] == "story"
    assert code.cmd[code.cmd.index("-m") + 1] == "C:/m/code.gguf"
    assert code.cmd[code.cmd.index("--alias") + 1] == "code"
    assert story.variant == "story" and code.variant == "code"
    assert services.llama_variant(_rt(), "code") == "default"


def test_embed_is_cpu_and_unlocked(tmp_path: Path) -> None:
    d = services.build_service(_rt(), _paths(tmp_path), "embed")
    assert "--embeddings" in d.cmd
    assert d.cmd[d.cmd.index("-ngl") + 1] == "0"
    assert d.cmd[d.cmd.index("--port") + 1] == "8081"
    assert d.gpu is False


def test_tts_and_dashboard(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tts = services.build_service(_rt(), paths, "tts")
    assert tts.cmd[1:] == ["-m", "ns_tts.server", "--port", "8091"]
    assert tts.gpu is True and tts.port == 8091
    dash = services.build_service(_rt(), paths, "dashboard")
    assert dash.cmd[1:] == ["-m", "ns_dashboard.server"]
    assert dash.port == 8100 and dash.gpu is False and dash.persistent is True


def test_pytorch_fallback_starts_with_the_tts_venv_python(tmp_path: Path) -> None:
    """Review row 3: torch and qwen_tts live in the tts venv, not in the supervisor venv."""
    paths = _paths(tmp_path)
    sup = paths.venvs / "supervisor" / "Scripts" / "python.exe"
    tts_py = paths.venvs / "tts" / "Scripts" / "python.exe"
    for exe in (sup, tts_py):
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
    reference = _rt().model_copy(update={"tts": _rt().tts.model_copy(update={"engine": "reference"})})
    svc = services.build_service(reference, paths, "tts")
    assert svc.cmd == [str(tts_py), "-m", "ns_tts.server", "--port", "8091"]
    for engine in ("qwentts", "faster"):  # the qwentts engine needs no torch: supervisor venv
        rt = _rt().model_copy(update={"tts": _rt().tts.model_copy(update={"engine": engine})})
        assert services.build_service(rt, paths, "tts").cmd[0] == str(sup)
    # the dashboard always uses the supervisor venv
    assert services.build_service(reference, paths, "dashboard").cmd[0] == str(sup)


def test_pytorch_fallback_without_a_tts_venv_falls_back_to_the_running_python(tmp_path: Path) -> None:
    reference = _rt().model_copy(update={"tts": _rt().tts.model_copy(update={"engine": "reference"})})
    cmd = services.build_service(reference, _paths(tmp_path), "tts").cmd
    assert cmd[0] == services.sys.executable


def test_venv_python_prefers_supervisor_venv(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    exe = paths.venvs / "supervisor" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    assert services.build_service(_rt(), paths, "tts").cmd[0] == str(exe)


def test_table_has_no_whisper_and_cpu_dev_is_not_gpu(tmp_path: Path) -> None:
    table = services.build_service_table(Runtime.default("cpu-dev"), _paths(tmp_path))
    assert set(table) == {"llama", "embed", "tts", "dashboard"}
    assert table["llama"].gpu is False and table["tts"].gpu is False


def test_unknown_service_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        services.build_service(_rt(), _paths(tmp_path), "whisper")
    assert ei.value.code == "NS-E202"
