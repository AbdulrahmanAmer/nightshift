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

TRIMMED_TOOLS = ("todowrite", "todoread", "webfetch", "task", "skill")


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
    assert 100 <= len(prompt.split()) <= 400            # a short prompt, not a manual
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


# What OpenCode 1.19.5 (manifest.json bins.opencode) needs to change a file, and the switches that take it
# away. Measured in the block B install proof with the pinned exe against a recording endpoint: the config
# with "patch": false sent bash, glob, grep, read, skill; the same config without it sent edit and write too.
# `edit`, `write` and `multiedit` are the same permission as `patch` in that version, and its edit tool
# refuses a file that `read` has not opened.
EDITING_TOOLS = {"read", "edit", "write"}
SWITCHES_THAT_REMOVE_EDITING = {"read", "edit", "write", "patch", "multiedit"}


def launcher_list(name: str) -> list[str]:
    """The quoted items of `$script:<name> = @(...)` in launchers/_common.ps1."""
    import re

    launcher = (Path(__file__).resolve().parents[1] / "launchers" / "_common.ps1").read_text(encoding="utf-8")
    found = re.search(r"^\$script:" + re.escape(name) + r" = @\(([^)]*)\)", launcher, re.MULTILINE)
    assert found, name
    return re.findall(r"'([^']*)'", found.group(1))


def test_no_switch_takes_away_a_tool_opencode_needs_to_edit_a_file(tmp_path: Path) -> None:
    """Block B proof, defect 1: "patch": false removed edit and write, so Nightshift Code could not change a
    file. Fails when any writer of the config (setup or the launcher's sync) switches off read, edit, write,
    patch or multiedit, or denies the edit permission."""
    from installer.stage2.steps import s07_configs

    assert set(getattr(s07_configs, "EDITING_TOOLS", ())) == EDITING_TOOLS
    assert set(getattr(s07_configs, "EDITING_SWITCHES", ())) == SWITCHES_THAT_REMOVE_EDITING
    cfg = written_config(tmp_path)
    for place in (cfg, cfg.get("agent", {}).get("build", {})):
        off = {name for name, on in place.get("tools", {}).items() if on is False}
        assert not off & SWITCHES_THAT_REMOVE_EDITING, off
        assert "permission" not in place
    assert not set(launcher_list("NsOpenCodeToolsOff")) & SWITCHES_THAT_REMOVE_EDITING
    assert set(launcher_list("NsOpenCodeEditingSwitches")) == SWITCHES_THAT_REMOVE_EDITING


def test_the_launcher_carries_the_same_switches_and_earlier_prompts_as_setup() -> None:
    import hashlib

    from installer.stage2.steps import s07_configs

    assert launcher_list("NsOpenCodeToolsOff") == list(s07_configs.TOOLS_OFF) == list(TRIMMED_TOOLS)
    assert launcher_list("NsOpenCodeToolsWithdrawn") == list(getattr(s07_configs, "TOOLS_OFF_WITHDRAWN", ()))
    assert not set(s07_configs.TOOLS_OFF) & set(s07_configs.TOOLS_OFF_WITHDRAWN)
    earlier = list(getattr(s07_configs, "SUPERSEDED_PROMPT_SHA256", ()))
    assert earlier and launcher_list("NsOpenCodeSupersededPrompts") == earlier
    # the current prompt is never on the list of earlier ones (the sync would rewrite the file at every start)
    assert hashlib.sha256(s07_configs.OPENCODE_PROMPT.encode("utf-8")).hexdigest() not in earlier


# The first build request of `opencode run "say hi"` with the config setup writes, sent by the pinned
# OpenCode 1.19.5 started from PowerShell (as the shortcut starts it) to a recording endpoint, 2026-11-10:
# 14,579 characters = each tool definition below + the system message (the Nightshift prompt, then 336
# characters OpenCode adds about the model and the folder) + 232 characters of envelope and the question.
# Tools Nightshift switches off are listed with the size they had in the 0.1.14 request.
MEASURED_TOOL_CHARS = {
    "bash": 5904, "edit": 1958, "glob": 1114, "grep": 1183, "read": 1734, "write": 1026,
    "skill": 676, "task": 3856, "todowrite": 2686, "webfetch": 1293,
}
MEASURED_SYSTEM_EXTRA_CHARS = 336
MEASURED_ENVELOPE_CHARS = 232
MEASURED_REQUEST_CHARS = 14579
# Two measures of tokens: 3.5 characters per token, and the 2.046 of the 0.1.14 request (31,343 characters,
# which llama.cpp counted as 15,317 tokens, defect D-34). The second is the strict one.
STRICT_CHARS_PER_TOKEN = 31343 / 15317
BUDGET_TOKENS = 8192                 # half of the smallest context Nightshift Code starts with
MIN_MARGIN_STRICT = 1000             # measured: 8,192 - 7,125 = 1,067
MIN_MARGIN_LOOSE = 4000              # measured: 8,192 - 4,165 = 4,027


def estimated_first_request_chars(tools_off: set[str], prompt: str) -> int:
    tools = sum(size for name, size in MEASURED_TOOL_CHARS.items() if name not in tools_off)
    return tools + len(prompt) + MEASURED_SYSTEM_EXTRA_CHARS + MEASURED_ENVELOPE_CHARS


def test_the_first_request_keeps_its_margin_under_both_measures(tmp_path: Path) -> None:
    """With edit and write back, the first request stays under half of a 16K context with room to spare.
    A longer prompt or a tool switched back on moves the estimate; both ends of the margin are checked."""
    cfg = written_config(tmp_path)
    off = {name for name, on in cfg.get("tools", {}).items() if on is False}
    assert not off - set(MEASURED_TOOL_CHARS) - {"todoread"}       # every switch has a measured size
    assert EDITING_TOOLS <= set(MEASURED_TOOL_CHARS) - off          # the estimate counts edit and write
    chars = estimated_first_request_chars(off, cfg["agent"]["build"]["prompt"])
    assert chars == MEASURED_REQUEST_CHARS                          # the estimate is the measured request
    strict = chars / STRICT_CHARS_PER_TOKEN
    loose = chars / 3.5
    assert BUDGET_TOKENS - strict >= MIN_MARGIN_STRICT, round(strict)
    assert BUDGET_TOKENS - loose >= MIN_MARGIN_LOOSE, round(loose)
    # the other end: the shape the proof refused (edit and write back, nothing else trimmed) has no margin
    earlier = estimated_first_request_chars({"todowrite", "todoread", "webfetch", "task"}, "x" * 1514)
    assert BUDGET_TOKENS - earlier / STRICT_CHARS_PER_TOKEN < MIN_MARGIN_STRICT


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


def test_the_tune_reads_the_context_nightshift_code_refuses_below_not_a_copy_of_it() -> None:
    """D-44: `ns tune` on a processor-only PC with Nightshift Code installed never goes below the number the
    Code launcher refuses below (D-34), which setup writes as the model's limit."""
    import re

    from installer.stage2.steps.s07_configs import MIN_CODE_CONTEXT
    from ns_supervisor import tune

    assert tune.CODE_MIN_CONTEXT == MIN_CODE_CONTEXT
    script = Path(__file__).resolve().parents[1] / "launchers" / "nightshift-code.ps1"
    launcher = script.read_text(encoding="ascii")
    found = re.search(r"^\$minCtx = (\d+)$", launcher, re.M)
    assert found is not None and found.group(1) == str(MIN_CODE_CONTEXT)
