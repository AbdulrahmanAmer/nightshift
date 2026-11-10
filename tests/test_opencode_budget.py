"""D-34: Nightshift Code's first question fits a 16K context (setup half; the launcher half: test_launchers).

OpenCode's default prompt and tool definitions are about 15,300 tokens. Setup writes a config that trims them,
sets the model's limit to the install's context, and names the model after the installed file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.runtime_cfg import ensure_baseline
from installer.stage2.steps.s07_configs import S07Configs
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_runtime, save_runtime

TRIMMED_TOOLS = ("todowrite", "todoread", "webfetch", "task", "patch")


def written_config(tmp_path: Path, *, ctx_tokens: int = 16384, model_path: str | None = None,
                   port: int | None = None) -> dict[str, Any]:
    """Run s07 on a test context whose runtime.json has the given llama context and model file."""
    app = tmp_path / "app"
    app.mkdir(parents=True)
    ctx = make_test_context(tmp_path, raw_answers=example_answers(install_code=True), resolve=True,
                            app_path=app, simulate=False)
    ensure_baseline(ctx)
    rt = load_runtime(ctx.paths)
    rt.llama.flags.ctx = ctx_tokens
    if model_path is not None:
        rt.llama.model_path = model_path
    if port is not None:
        rt.llama.port = port
    save_runtime(ctx.paths, rt)
    S07Configs().run(ctx)
    return json.loads(ctx.paths.opencode_file.read_text(encoding="utf-8"))


def code_model(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg["provider"]["local"]["models"]["code"]


def test_setup_writes_the_budget_into_opencode_json(tmp_path: Path) -> None:
    cfg = written_config(tmp_path, ctx_tokens=16384, port=9191)
    assert code_model(cfg).get("limit") == {"context": 16384, "output": 4096}
    assert cfg.get("tools") == {name: False for name in TRIMMED_TOOLS}
    prompt = cfg.get("agent", {}).get("build", {}).get("prompt")
    assert isinstance(prompt, str) and "Nightshift" in prompt
    assert 100 <= len(prompt.split()) <= 400            # "about 300 words": a short prompt, not a manual
    url = cfg["provider"]["local"]["options"]["baseURL"]
    assert url == "http://127.0.0.1:9191/v1"             # still follows llama.host and llama.port


def test_the_model_is_named_after_the_installed_file(tmp_path: Path) -> None:
    named = written_config(tmp_path / "a", model_path=r"D:\Night Home\models\llm\Qwen3-1.7B-Q8_0.gguf")
    assert code_model(named).get("name") == "Qwen3-1.7B-Q8_0 (local)"
    empty = written_config(tmp_path / "b", model_path="")
    assert code_model(empty).get("name") == "Local model"


@pytest.mark.parametrize("tokens", [16384, 32768])
def test_the_context_written_follows_the_install_both_ends(tmp_path: Path, tokens: int) -> None:
    cfg = written_config(tmp_path, ctx_tokens=tokens)
    assert code_model(cfg).get("limit") == {"context": tokens, "output": 4096}


def test_the_prompt_is_ascii_and_the_same_text_the_launcher_carries() -> None:
    """The launcher brings a 0.1.14 file up to the budget with its own copy of the prompt: they must match."""
    from installer.stage2.steps import s07_configs

    prompt = getattr(s07_configs, "OPENCODE_PROMPT", "")
    assert prompt and prompt.isascii()
    launcher = (Path(__file__).resolve().parents[1] / "launchers" / "_common.ps1").read_text(encoding="utf-8")
    assert "$script:NsOpenCodePrompt = @'" in launcher
    start = launcher.index("$script:NsOpenCodePrompt = @'")
    body = launcher[launcher.index("\n", start) + 1 : launcher.index("\n'@", start)]
    assert body.replace("\r\n", "\n") == prompt


def test_the_engine_status_says_the_context_llama_runs_with(tmp_path: Path) -> None:
    """D-34 review: Nightshift Code asks the running engine (GET /status), not runtime.json, which the engine
    read only when it started."""
    from ns_common.models import Runtime
    from ns_common.paths import Paths
    from ns_supervisor.core import Supervisor
    from ns_supervisor.gpu import NullGpuProbe
    from ns_supervisor.testing import StubTable, fast_config

    rt = Runtime.default("gpu")
    rt.llama.flags.ctx = 8192
    sup = Supervisor(Paths(tmp_path / "ns"), rt, config=fast_config(), resolver=StubTable(),
                     gpu_probe=NullGpuProbe())
    services = sup.status()["services"]
    assert services["llama"].get("ctx") == 8192
    assert services["embed"].get("ctx", "missing") is None
