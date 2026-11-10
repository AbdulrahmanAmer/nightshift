"""s07_configs: runtime.json, opencode.json, channel profile and secrets (component C1, spec 5).

runtime.json was written as a baseline before tuning; here it is validated, not overwritten. API keys move
into the Windows keyring and are blanked in every answers.json.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from installer.stage2.answers import InstallerAnswers, blank_secrets_in_file, default_voice_name, slugify
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.runtime_cfg import ensure_baseline, update_runtime
from installer.stage2.secrets import KEY_NAMES
from ns_common.atomic import write_text_atomic
from ns_common.config import load_channel, load_runtime, save_channel
from ns_common.models import Channel, Runtime, Voice

# D-34: OpenCode's own prompt and tool definitions come to about 15,300 tokens, which leaves a 16K
# install about 1,000 tokens for the first question. The budget: the first request of a one-line
# question is at most half of the context (8,192 tokens at 16,384). The tools below are switched off
# and the build agent's prompt is replaced by the short one that follows. launchers/_common.ps1
# (Sync-NsOpenCodePort) carries the same values for a file written by an older release;
# tests/test_opencode_budget.py keeps the two copies equal.
# Nightshift Code refuses to start below MIN_CODE_CONTEXT (launchers/nightshift-code.ps1)
MIN_CODE_CONTEXT = 16384
CODE_OUTPUT_TOKENS = 4096
TOOLS_OFF = ("todowrite", "todoread", "webfetch", "task", "patch")
LOCAL_MODEL_NAME = "Local model"    # when runtime.json names no model file

OPENCODE_PROMPT = """You are Nightshift Code, a coding assistant that runs entirely on this computer with a small local model. Nothing you read or write leaves this machine.

Your context is small, so work in small steps and keep every answer short.

Where you work
- Start in the Projects folder of the person using you. Each folder in it is a project. Do not leave it unless you are asked to.
- Never touch the Nightshift program folders or its settings unless you are asked to.

How you work
- Read before you edit. Open a file, look at the part that matters, and only then change it. Do not guess what a file contains.
- Look at a few lines at a time. Search with grep or glob instead of opening whole files or whole folders.
- Change the least you can. Make one edit, then say what you changed and why in a sentence or two.
- Do not rewrite a file that only needs a small change. Do not add features nobody asked for.
- Run a command only when it helps answer the question. Say what you ran and what it showed.
- If a request is unclear, ask one short question before you start.
- If you do not know something, say so. Do not invent file names, functions or results.

How you answer
- Plain, short sentences. No long introductions and no summaries of what you were just told.
- Show code in small blocks, only the lines that matter.
- When a task is done, say so and name the files you changed.
- If a task is too big for the context you have, say so and offer the first small step instead of starting something you cannot finish."""  # noqa: E501 (the prompt is plain text; its lines are not wrapped)

OPENCODE_CONFIG: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "enabled_providers": ["local"],
    "provider": {
        "local": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Nightshift Local Qwen",
            "options": {"baseURL": "http://127.0.0.1:8080/v1", "apiKey": "local"},
            "models": {
                "code": {
                    "name": LOCAL_MODEL_NAME,
                    "limit": {"context": MIN_CODE_CONTEXT, "output": CODE_OUTPUT_TOKENS},
                }
            },
        }
    },
    "model": "local/code",
    "small_model": "local/code",
    "tools": {name: False for name in TOOLS_OFF},
    "agent": {"build": {"prompt": OPENCODE_PROMPT}},
    "autoupdate": False,
    "share": "disabled",
}


def opencode_model_name(model_path: str) -> str:
    """The model's name in OpenCode: the installed file without folder and extension, or a plain fallback."""
    stem = PureWindowsPath(model_path).stem if model_path else ""
    return f"{stem} (local)" if stem else LOCAL_MODEL_NAME


def opencode_config(
    host: str = "127.0.0.1", port: int = 8080, ctx: int = MIN_CODE_CONTEXT, model_path: str = ""
) -> dict[str, Any]:
    """OPENCODE_CONFIG for the llama-server and model in runtime.json instead of the baked defaults.

    OpenCode talks straight to llama-server, so a port changed by ns tune or by hand must reach its
    baseURL, and ``limit.context`` must be the context llama-server was started with so OpenCode compacts
    before the server refuses. The launcher repeats this on every start (Sync-NsOpenCodePort); this keeps
    a fresh write correct too.
    """
    cfg = copy.deepcopy(OPENCODE_CONFIG)
    local = cfg["provider"]["local"]
    local["options"]["baseURL"] = f"http://{host}:{port}/v1"
    local["models"]["code"]["name"] = opencode_model_name(model_path)
    local["models"]["code"]["limit"]["context"] = ctx
    return cfg


def apply_engine_test(ctx: StepContext, rt: Runtime) -> None:
    """Put the engine-test answers into ``rt`` on every run.

    ``build_runtime`` writes them only when runtime.json does not exist yet, so a re-run or an upgrade over an
    existing runtime.json would keep the old values and the answer would silently do nothing. The footage
    and tiny-story switches follow their answers both ways; the fake voice engine is set only when asked,
    because without the answer nothing here knows which real engine the earlier steps chose.
    """
    rt.fake_footage = ctx.fake_footage
    rt.tiny_story = ctx.tiny_story
    if ctx.fake_voice:
        rt.tts.engine = "fake"


def channel_from_answers(ans: InstallerAnswers) -> Channel:
    """Channel profile for the first channel (spec 5 s07)."""
    voice = ans.voice
    if voice.mode != "preset" and not voice.name:
        name = default_voice_name(ans.channel.display_name)
        voice = Voice.model_validate(voice.model_dump() | {"name": name})
    return Channel(
        slug=slugify(ans.channel.display_name),
        display_name=ans.channel.display_name,
        language=ans.channel.language,
        niche=ans.channel.niche,
        seed_keywords=list(ans.channel.seed_keywords),
        voice=voice,
        masters_per_week=ans.masters_per_week,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def ui_prefs_file(ctx: StepContext) -> Path:
    """config/ui_prefs.json: the greeting name and default theme chosen in the setup window."""
    return ctx.paths.config / "ui_prefs.json"


def write_ui_prefs(ctx: StepContext, ans: InstallerAnswers) -> None:
    """Save owner_name and ui_theme for the dashboard (CR-C1-UI-8: C9 reads this file)."""
    f = ui_prefs_file(ctx)
    f.parent.mkdir(parents=True, exist_ok=True)
    prefs = {"owner_name": ans.owner_name, "theme": ans.ui_theme}
    write_text_atomic(f, json.dumps(prefs, indent=2) + "\n")


def pending_voice_file(ctx: StepContext) -> Path:
    """config/pending_voice.json: the designed or cloned voice to create on the first run (gap 5)."""
    return ctx.paths.config / "pending_voice.json"


def queue_voice(ctx: StepContext, ans: InstallerAnswers, channel: Channel) -> bool:
    """Queue the designed or cloned voice chosen in setup; `studio` creates it on its first real run.

    It cannot be made here: the TTS engine (qwentts or the PyTorch fallback) is only settled by the smoke test
    after this step, and a voice made by one engine is refused by the other. The request is plain
    JSON read by `ns_studio.voice_setup` (the studio venv imports no installer code). Returns False for a
    preset voice or when the voice already exists.
    """
    voice = channel.voice
    if voice.mode == "preset" or not voice.name:
        return False
    if (ctx.paths.models_tts_voices / voice.name / "voice.json").is_file():
        return False
    request: dict[str, Any] = {"channel": channel.slug, "mode": voice.mode, "name": voice.name}
    if voice.mode == "design":
        request["description"] = voice.description
    else:
        request.update(
            ref_audio_path=ans.voice_ref_audio_path,
            ref_text=ans.voice_ref_text,
            rights_confirmed=bool(ans.voice_rights_confirmed),
        )
    f = pending_voice_file(ctx)
    f.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(f, json.dumps(request, indent=2) + "\n")
    return True


def answer_files(ctx: StepContext) -> list[Path]:
    """Every answers.json that may still hold a key."""
    files = [ctx.app_path / "answers.json", ctx.paths.config / "answers.json"]
    if ctx.answers_path is not None:
        files.append(ctx.answers_path)
    return list(dict.fromkeys(files))


def _file_has_keys(f: Path) -> bool:
    if not f.is_file():
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and any(data.get(k) for k in KEY_NAMES)


class S07Configs(Step):
    """Write the configuration files and move the secrets."""

    id = "s07_configs"
    title = "Settings and secrets"
    clears_on_run = ("smoke", "smoke_version")          # the smoke test reads the configs written here

    def check(self, ctx: StepContext) -> bool:
        """True when every config exists and no answers file still holds a key."""
        ans = ctx.ensure_answers()
        slug = slugify(ans.channel.display_name)
        if not (ctx.paths.runtime_file.exists() and ctx.paths.channel_file(slug).exists()):
            return False
        if not ui_prefs_file(ctx).exists():
            return False
        if ans.install_code and not ctx.paths.opencode_file.exists():
            return False
        return not any(_file_has_keys(f) for f in answer_files(ctx))

    def run(self, ctx: StepContext) -> None:
        """Complete runtime.json, write opencode.json and the channel, store and blank the secrets."""
        ans = ctx.ensure_answers()
        ensure_baseline(ctx)
        update_runtime(ctx, lambda rt: apply_engine_test(ctx, rt))

        channel = channel_from_answers(ans)
        if ctx.paths.channel_file(channel.slug).exists():
            ctx.ui.say(f"Channel '{channel.slug}' already exists; left unchanged.")
        else:
            save_channel(ctx.paths, channel)
            ctx.ui.say(f"Channel '{channel.slug}' created.")

        write_ui_prefs(ctx, ans)

        if ans.install_code:
            llama = load_runtime(ctx.paths).llama
            ctx.paths.opencode_file.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(
                ctx.paths.opencode_file,
                json.dumps(
                    opencode_config(llama.host, llama.port, llama.flags.ctx, llama.model_path),
                    indent=2) + "\n")

        for key in KEY_NAMES:
            value = getattr(ans, key)
            if value:
                ctx.store.set(key, value)
        self._keep_the_owners_copy(ctx)
        for f in answer_files(ctx):
            if blank_secrets_in_file(f):
                ctx.ui.say(f"Removed the API keys from {f.name} (they are in the Windows keyring now).")
        ans.pexels_api_key = None
        ans.pixabay_api_key = None

        if ans.restore_from:
            res = ctx.run_tool("ns", ["restore", ans.restore_from], timeout=900)
            if res.ok:
                ctx.ui.say(f"Settings restored from {ans.restore_from}.")
            else:
                ctx.ui.say("NS-E703: could not restore your earlier settings; using fresh ones.", "yellow")
                ctx.log.warn("restore_failed", f"exit {res.returncode}")
        if queue_voice(ctx, ans, channel):
            ctx.ui.say(
                f"Your {channel.voice.mode} voice '{channel.voice.name}' is created the first time "
                "Nightshift runs. To make it now: studio voice apply-pending."
            )

    @staticmethod
    def _keep_the_owners_copy(ctx: StepContext) -> None:
        """A key in an answers file that the key store does not hold is stored before the file is cleared.

        s01 stores the keys of the answers in use. This is the safety net for any other way of getting here
        (review of PR 136): clearing the file would otherwise delete the owner's only copy while saying it is
        in the keyring.
        """
        for f in answer_files(ctx):
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else None
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for key in KEY_NAMES:
                value = data.get(key)
                if isinstance(value, str) and value.strip() and not ctx.store.get(key):
                    ctx.store.set(key, value)
                    ctx.log.info("key_stored", name=key, source=f.name)

    def verify(self, ctx: StepContext) -> None:
        """Configs validate; keys are in the store; no answers file keeps a key."""
        ans = ctx.ensure_answers()
        load_runtime(ctx.paths)
        load_channel(ctx.paths, slugify(ans.channel.display_name))
        if ans.install_code and not ctx.paths.opencode_file.exists():
            raise make_error("NS-E110", "config/opencode.json was not written")
        leaking = [f.name for f in answer_files(ctx) if _file_has_keys(f)]
        if leaking:
            raise make_error("NS-E501", f"API keys are still written in {', '.join(leaking)}")
