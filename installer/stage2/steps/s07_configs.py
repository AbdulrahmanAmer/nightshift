"""s07_configs: runtime.json, opencode.json, channel profile and secrets (component C1, spec 5).

runtime.json was written as a baseline before tuning; here it is validated, not overwritten. API keys move
into the Windows keyring and are blanked in every answers.json.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from installer.stage2.answers import InstallerAnswers, blank_secrets_in_file, default_voice_name, slugify
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.runtime_cfg import ensure_baseline, update_runtime
from installer.stage2.secrets import KEY_NAMES
from ns_common.config import load_channel, load_runtime, save_channel
from ns_common.models import Channel, Runtime, Voice

OPENCODE_CONFIG: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "enabled_providers": ["local"],
    "provider": {
        "local": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Nightshift Local Qwen",
            "options": {"baseURL": "http://127.0.0.1:8080/v1", "apiKey": "local"},
            "models": {"code": {"name": "Qwen3.6 35B-A3B (local)"}},
        }
    },
    "model": "local/code",
    "small_model": "local/code",
    "autoupdate": False,
    "share": "disabled",
}


def opencode_config(host: str = "127.0.0.1", port: int = 8080) -> dict[str, Any]:
    """OPENCODE_CONFIG pointed at the llama-server in runtime.json instead of the baked default.

    OpenCode talks straight to llama-server, so a port changed by ns tune or by hand must reach its baseURL.
    The launcher repeats this on every start (Sync-NsOpenCodePort); this keeps a fresh write correct too.
    """
    cfg = copy.deepcopy(OPENCODE_CONFIG)
    cfg["provider"]["local"]["options"]["baseURL"] = f"http://{host}:{port}/v1"
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
    f.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")


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
    f.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
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
            ctx.paths.opencode_file.write_text(
                json.dumps(opencode_config(llama.host, llama.port), indent=2) + "\n", encoding="utf-8")

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
