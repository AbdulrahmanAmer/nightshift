"""s01_questions: home folder, channel, voice, API keys, schedule and options (component C1, spec 5).

Every question is answered from answers.json when the key is present; missing ones are asked when a person
is at the keyboard and fall back to their defaults (or fail for required values) when nobody is.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from installer.stage2.answers import (
    AnswersError,
    InstallerAnswers,
    build_answers,
    load_raw,
    slugify,
    write_answers,
)
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.secrets import KEY_NAMES, PEXELS_KEY, PIXABAY_KEY
from installer.stage2.sysinfo import (
    choose_default_home,
    drive_type,
    drives_from_preflight,
    local_drives,
    recommended_tier,
    required_free_gb,
)
from installer.stage2.winenv import HOME_VAR, get_user_env, set_user_env

PRESET_VOICES = ("Ryan", "Aiden", "Serena", "Vivian")
PEXELS_URL = "https://www.pexels.com/api/"
PIXABAY_URL = "https://pixabay.com/api/docs/"
RUN_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------------------------------------------------------------------------------------------------------
# live key validation (1-result query)
# ---------------------------------------------------------------------------------------------------------
def validate_pexels(client: httpx.Client, key: str) -> None:
    """Pexels search with one result. Raises NS-E501 (rejected) or NS-E503 (network)."""
    try:
        r = client.get(
            "https://api.pexels.com/v1/search", params={"query": "nature", "per_page": 1},
            headers={"Authorization": key},
        )
    except httpx.HTTPError as e:
        raise make_error("NS-E503", f"Pexels: {type(e).__name__}") from None
    if r.status_code == 429:
        return  # rate limited means the key was recognised
    if r.status_code != 200:
        raise make_error("NS-E501", f"Pexels answered HTTP {r.status_code}")


def validate_pixabay(client: httpx.Client, key: str) -> None:
    """Pixabay search with a small result page. Raises NS-E502 (rejected) or NS-E503 (network)."""
    try:
        r = client.get("https://pixabay.com/api/", params={"key": key, "q": "nature", "per_page": 3})
    except httpx.HTTPError as e:
        raise make_error("NS-E503", f"Pixabay: {type(e).__name__}") from None
    if r.status_code == 429:
        return
    if r.status_code != 200:
        raise make_error("NS-E502", f"Pixabay answered HTTP {r.status_code}")


# ---------------------------------------------------------------------------------------------------------
# voice preview
# ---------------------------------------------------------------------------------------------------------
def preview_voice(ctx: StepContext, name: str) -> None:
    """Play a sample of a preset voice. Simulate mode uses the FakeTts tone-burst sample."""
    sample = "This is how your narrator sounds. The night shift begins."
    if ctx.simulate:
        from ns_common.tts_client import FakeTts

        out = Path(tempfile.gettempdir()) / f"nightshift-voice-{name}.wav"
        FakeTts().speak(sample, name, 1.0, out)
        ctx.ui.say(f"  playing sample: {name}")
        ctx.ui.play(out)
        return
    ctx.ui.say(f"  {name}: sample playback needs the voice engine, which is installed in a later step.")


# ---------------------------------------------------------------------------------------------------------
# the step
# ---------------------------------------------------------------------------------------------------------
class S01Questions(Step):
    """Ask (or read) every setup choice and write the blanked answers file."""

    id = "s01_questions"
    title = "Questions"

    # ---- helpers ----
    @staticmethod
    def _answers_file(ctx: StepContext) -> Path:
        return ctx.paths.config / "answers.json"

    @staticmethod
    def _asked(ctx: StepContext, key: str, prompt: str, default: str | None = None) -> str | None:
        """Value from answers.json, else asked when someone is there, else the default."""
        val = ctx.raw_answers.get(key)
        if val not in (None, ""):
            return str(val)
        if ctx.ui.interactive:
            return ctx.ui.ask(prompt, default)
        return default

    def check(self, ctx: StepContext) -> bool:
        """True when a valid answers file from an earlier run exists and its secrets are stored."""
        if ctx.ui.bridge and not ctx.state.is_done(self.id):
            return False        # the setup window writes a first answers.json before this step has run
        f = self._answers_file(ctx)
        if not f.is_file():
            return False
        try:
            ans = build_answers(load_raw(f))
        except AnswersError:
            return False
        if not ctx.simulate and not all(ctx.store.get(k) for k in KEY_NAMES):
            return False
        ctx.answers = ans
        return True

    # ---- home ----
    def _pick_home(self, ctx: StepContext, tier: str, custom_bytes: int | None = None) -> Path:
        need = required_free_gb(tier, custom_bytes)
        provided = ctx.raw_answers.get("home_path")
        while True:
            if provided:
                home = Path(str(provided))
            elif ctx.home_forced or (ctx.paths.venvs / "studio").exists():
                home = ctx.paths.home
            else:
                drives = drives_from_preflight(ctx.preflight) or local_drives()
                default = choose_default_home(drives, need) or ctx.paths.home
                free = ctx.free_gb_fn(default)
                if ctx.ui.interactive:
                    reply = ctx.ui.ask(f"Install folder ({free:.0f} GB free there)", str(default))
                    home = Path(reply.strip().strip('"'))
                else:
                    home = default
            free = ctx.free_gb_fn(home)
            if free >= need:
                return home
            detail = f"{free:.0f} GB free at {home}, {need} GB needed"
            if provided or ctx.home_forced or not ctx.ui.interactive:
                raise make_error("NS-E104", detail)
            ctx.ui.say(f"Not enough space: {detail}. Pick another drive.", "red")

    # ---- voice ----
    def _voice(self, ctx: StepContext, channel_name: str) -> dict[str, Any]:
        raw = ctx.raw_answers
        if isinstance(raw.get("voice"), dict) and raw["voice"]:
            return dict(raw["voice"])
        if not ctx.ui.interactive:
            return {"mode": "preset", "speaker": PRESET_VOICES[0]}
        mode = ctx.ui.choose("Narrator voice: preset, design (describe it) or clone (from a recording)?",
                             ["preset", "design", "clone"], default="preset")
        if mode == "preset":
            ctx.ui.say("Listen to the four preset voices:")
            for name in PRESET_VOICES:
                preview_voice(ctx, name)
            speaker = ctx.ui.choose("Which voice do you want?", list(PRESET_VOICES), default=PRESET_VOICES[0])
            return {"mode": "preset", "speaker": speaker}
        name = f"{slugify(channel_name)}-voice"
        if mode == "design":
            desc = ctx.ui.ask("Describe the narrator in a sentence (age, tone, pace)")
            return {"mode": "design", "speaker": None, "description": desc, "name": name}
        return {"mode": "clone", "speaker": None, "name": name}

    # ---- keys ----
    def _keys(self, ctx: StepContext) -> None:
        if ctx.simulate:
            ctx.ui.say("Simulation: API key checks are skipped.", "dim")
            for name in KEY_NAMES:
                if ctx.raw_answers.get(name):
                    ctx.store.set(name, str(ctx.raw_answers[name]))
            return
        specs = [
            (PEXELS_KEY, "Pexels", PEXELS_URL, validate_pexels, "NS-E501"),
            (PIXABAY_KEY, "Pixabay", PIXABAY_URL, validate_pixabay, "NS-E502"),
        ]
        client = ctx.http_factory()
        try:
            for name, label, url, validator, code in specs:
                value = ctx.raw_answers.get(name) or ctx.store.get(name)
                if value:
                    validator(client, str(value))
                else:
                    if not ctx.ui.interactive:
                        raise make_error(code, f"{label} key is missing from answers.json")
                    ctx.ui.say(f"{label} key (free): {url}")
                    for attempt in range(3):
                        value = ctx.ui.ask(f"Paste your {label} API key", secret=True).strip()
                        try:
                            validator(client, value)
                            break
                        except Exception as e:
                            ctx.ui.say(f"That key did not work: {e}", "red")
                            if attempt == 2:
                                raise
                ctx.store.set(name, str(value))
                ctx.raw_answers[name] = None  # never keep a key in the working copy
                ctx.log.info("key_stored", name=name)
        finally:
            client.close()

    # ---- run ----
    def run(self, ctx: StepContext) -> None:
        """Resolve every answer, prepare the home folder and write config/answers.json (no secrets)."""
        raw = ctx.raw_answers
        merged: dict[str, Any] = {k: v for k, v in raw.items() if k not in KEY_NAMES}

        ram = float((ctx.preflight or {}).get("ram_gb") or 32)
        custom = raw.get("llm_custom") or None
        size = custom.get("size") if isinstance(custom, dict) else None
        custom_bytes = size if isinstance(size, int) and size > 0 else None
        # Balanced is the recommended default; a PC under 32 GB of RAM starts on Small (the fallback).
        default_tier = recommended_tier(ram)
        if custom:
            tier = "small"          # llm_custom replaces the tier's model; no RAM rule applies
        else:
            if default_tier == "small" and raw.get("llm_tier") in (None, ""):
                ctx.ui.say(f"This PC has {ram:.0f} GB of memory, so Small is selected. Balanced needs 32 GB.")
            asked = self._asked(ctx, "llm_tier", "LLM size: small, balanced or best?", default_tier)
            tier = str(asked or default_tier)
        if tier not in ("small", "balanced", "best"):
            raise AnswersError(f"llm_tier must be small, balanced or best, not {tier!r}")
        if tier != "small" and ram < 31:
            raise AnswersError(f"llm_tier '{tier}' needs 32 GB of RAM; this PC has {ram:.0f} GB")
        merged["llm_tier"] = tier

        home = self._pick_home(ctx, tier, custom_bytes)
        ctx.relocate(home)
        ctx.paths.ensure()
        if drive_type(ctx.preflight, home) == "HDD":
            ctx.warn("NS-W106", str(home))
        if not set_user_env(ctx.runner, HOME_VAR, str(home)):
            raise make_error("NS-E104", "could not set the NIGHTSHIFT_HOME user variable")
        merged["home_path"] = str(home)

        chan = dict(raw.get("channel") or {})
        name = chan.get("display_name") or self._asked(ctx, "_channel_name", "Channel name")
        niche = chan.get("niche") or self._asked(
            ctx, "_channel_niche", "Channel niche (for example horror fiction)"
        )
        if not name or not niche:
            raise AnswersError("channel.display_name and channel.niche are required")
        language = chan.get("language") or self._asked(ctx, "_channel_language", "Story language", "en")
        language = language or "en"
        seeds = chan.get("seed_keywords")
        if seeds is None:
            text = self._asked(ctx, "_channel_seeds", "Seed keywords, comma separated (optional)", "") or ""
            seeds = [s.strip() for s in text.split(",") if s.strip()]
        merged["channel"] = {"display_name": name, "niche": niche, "language": language,
                             "seed_keywords": list(seeds)}

        merged["voice"] = self._voice(ctx, str(name))
        if merged["voice"].get("mode") == "clone" and not raw.get("voice_ref_audio_path"):
            if not ctx.ui.interactive:
                raise AnswersError("a cloned voice needs voice_ref_audio_path in answers.json")
            merged["voice_ref_audio_path"] = ctx.ui.ask("Path of the 3 to 10 second voice recording")
            merged["voice_ref_text"] = ctx.ui.ask("What is said in the recording?")
            merged["voice_rights_confirmed"] = ctx.ui.confirm("I own or have the rights to this voice", False)

        mpw = self._asked(ctx, "masters_per_week", "Long videos per week (1 to 3)", "1")
        merged["masters_per_week"] = int(mpw or 1)
        run_time = self._asked(ctx, "run_time", "Nightly run time (24 hour clock)", "03:00") or "03:00"
        while not RUN_TIME_RE.match(run_time):
            if not ctx.ui.interactive:
                raise AnswersError("run_time must be HH:MM (24 hour clock)")
            run_time = ctx.ui.ask("Please use HH:MM, for example 03:00", "03:00")
        merged["run_time"] = run_time

        if "install_code" in raw and raw["install_code"] is not None:
            merged["install_code"] = bool(raw["install_code"])
        else:
            merged["install_code"] = ctx.ui.confirm("Install Nightshift Code (offline assistant)?", True)

        source = self._asked(ctx, "models_source", "Folder with the model files (blank to download)", "")
        if source and not Path(source).is_dir():
            raise AnswersError(f"models_source folder not found: {source}")
        merged["models_source"] = source or None
        merged["allow_cpu"] = bool(raw.get("allow_cpu", False))
        merged["restore_from"] = raw.get("restore_from")

        for key in ("voice_ref_audio_path", "voice_ref_text", "voice_rights_confirmed"):
            if key in raw and key not in merged:
                merged[key] = raw[key]
        ans = build_answers({**merged, PEXELS_KEY: None, PIXABAY_KEY: None})

        self._keys(ctx)
        ctx.answers = ans
        write_answers(self._answers_file(ctx), ans)
        ctx.log.info("answers_saved", channel=slugify(ans.channel.display_name), tier=tier)
        self._summary(ctx, ans)

    @staticmethod
    def _summary(ctx: StepContext, ans: InstallerAnswers) -> None:
        ctx.ui.table(
            "Your choices", ["Setting", "Value"],
            [
                ["Install folder", str(ctx.paths.home)],
                ["Channel", f"{ans.channel.display_name} ({ans.channel.niche}, {ans.channel.language})"],
                ["Voice", f"{ans.voice.mode}: {ans.voice.speaker or ans.voice.description}"],
                ["Long videos per week", str(ans.masters_per_week)],
                ["Nightly run time", ans.run_time],
                ["Nightshift Code", "yes" if ans.install_code else "no"],
                ["LLM size", f"custom: {ans.llm_custom.file}" if ans.llm_custom else ans.llm_tier],
                ["Model files", ans.models_source or "download"],
            ],
        )

    def verify(self, ctx: StepContext) -> None:
        """Answers are saved, the home tree exists and the user variable points at it."""
        ans = ctx.ensure_answers()
        if not self._answers_file(ctx).is_file():
            raise AnswersError("config/answers.json was not written")
        if not ctx.paths.config.is_dir():
            raise make_error("NS-E104", f"{ctx.paths.home} was not created")
        current = get_user_env(ctx.runner, HOME_VAR)
        if current != str(ctx.paths.home):
            raise make_error("NS-E104", f"NIGHTSHIFT_HOME is {current!r}, expected {str(ctx.paths.home)!r}")
        if not ctx.simulate:
            for key in KEY_NAMES:
                if not ctx.store.get(key):
                    raise make_error("NS-E501" if key == PEXELS_KEY else "NS-E502", "key not stored")
        ctx.log.info("answers_verified", tier=ans.llm_tier)
