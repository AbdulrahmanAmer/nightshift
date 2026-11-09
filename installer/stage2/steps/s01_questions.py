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

from installer.stage2 import reinstall
from installer.stage2.answers import (
    AnswersError,
    InstallerAnswers,
    build_answers,
    load_raw,
    slugify,
    wants_voicedesign,
    write_answers,
)
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.devmode import MIN_FREE_GB
from installer.stage2.downloader import human_bytes
from installer.stage2.errors import make_error
from installer.stage2.secrets import KEY_NAMES, PEXELS_KEY, PIXABAY_KEY, any_key_stored
from installer.stage2.sysinfo import (
    choose_default_home,
    drive_type,
    drives_from_preflight,
    local_drives,
    recommended_tier,
    required_free_gb,
)
from installer.stage2.web.pathcheck import FOLDER_FOREIGN, folder_kind, foreign_warning, layout_problem
from installer.stage2.winenv import HOME_VAR, get_user_env, set_user_env
from ns_common.errors import NsError
from ns_common.pathrules import ascii_problem

PRESET_VOICES = ("Ryan", "Aiden", "Serena", "Vivian")
PEXELS_URL = "https://www.pexels.com/api/"
PIXABAY_URL = "https://pixabay.com/api/docs/"
NO_KEY_NOTICE = "No stock footage key yet. Setup continues; add a Pixabay key (free) later in Settings"
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
def voicedesign_row(ctx: StepContext, ans: InstallerAnswers) -> str:
    """What the summary says about the VoiceDesign talker: fetched now (with its size) or later."""
    if not wants_voicedesign(ans):
        return "later: Settings, Models"
    size = sum(f.bytes or 0 for f in ctx.manifest.profiles[ctx.profile].tts_optional)
    return f"yes (+{human_bytes(size)} to download)"


class S01Questions(Step):
    """Ask (or read) every setup choice and write the blanked answers file."""

    id = "s01_questions"
    title = "Questions"
    clears_on_run = ("smoke", "smoke_version")          # other answers (tier, voice, code): test again

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

    _ran = False        # run() did the work in this process (verify() then has the lines already)

    def check(self, ctx: StepContext) -> bool:
        """True when a valid answers file from an earlier run exists, its secrets are stored and the answers
        in use (``--answers``, next to INSTALL.cmd, the window's choices) do not change it (D-14)."""
        if ctx.ui.bridge and not ctx.state.is_done(self.id):
            return False        # the setup window writes a first answers.json before this step has run
        f = self._answers_file(ctx)
        if not f.is_file():
            return False
        try:
            ans = build_answers(load_raw(f))
        except AnswersError:
            return False
        earlier = reinstall.kept_answers(ctx)
        if earlier is not None and reinstall.changes(ctx, earlier[0], ctx.raw_answers):
            return False        # the new answers win; run() says so and rebuilds what comes from them
        if reinstall.unfinished_choices(ctx):
            return False  # a window's choices were saved, never applied; the file in use replaces them
        if reinstall.new_keys(ctx):
            return False  # a key the key store does not hold: run() checks and stores it (D-14)
        reinstall.forget_kept(ctx)  # nothing differs: the window's copy of the earlier answers is used up
        ctx.answers = ctx.constrain(ans)
        return True

    # ---- home ----
    @staticmethod
    def _taken(ctx: StepContext, candidate: Path) -> bool:
        """A default folder that must not be offered: someone's other files are in it, or the extracted
        program (D-21). An earlier install or its kept data is offered: it is the way back to them."""
        return (folder_kind(str(candidate)) == FOLDER_FOREIGN
                or layout_problem(str(candidate), ctx.app_path) is not None)

    def _pick_home(self, ctx: StepContext, tier: str, custom_bytes: int | None = None) -> Path:
        need = MIN_FREE_GB if ctx.dev_mode else required_free_gb(tier, custom_bytes)
        provided = ctx.raw_answers.get("home_path")
        while True:
            if provided:
                home = Path(str(provided))
            elif ctx.home_forced or (ctx.paths.venvs / "studio").exists():
                home = ctx.paths.home
            else:
                drives = drives_from_preflight(ctx.preflight) or local_drives()
                default = choose_default_home(drives, need, lambda p: self._taken(ctx, p)) or ctx.paths.home
                free = ctx.free_gb_fn(default)
                if ctx.ui.interactive:
                    reply = ctx.ui.ask(f"Install folder ({free:.0f} GB free there)", str(default))
                    home = Path(reply.strip().strip('"'))
                else:
                    home = default
            if (problem := ascii_problem(str(home))) is not None:
                if provided or ctx.home_forced or not ctx.ui.interactive:
                    raise make_error("NS-E117", f"{home}: {problem}")
                ctx.ui.say(f"{problem}. Pick another folder.", "red")
                continue
            if home != ctx.paths.home and folder_kind(str(home)) == FOLDER_FOREIGN:
                ctx.ui.say(foreign_warning(str(home)), "yellow")        # D-21: no word, was the defect
                typed = not (provided or ctx.home_forced) and ctx.ui.interactive
                if typed and not ctx.ui.confirm("Install there anyway?", False):
                    continue
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
            # Both keys are optional (Pexels stopped giving out new keys; Settings takes a key later).
            for name, label, url, validator, _code in specs:
                provided = ctx.raw_answers.get(name)
                value = provided or ctx.store.get(name)
                if value and not provided:
                    # a key saved by an earlier setup or in Settings: when the service rejects it now, setup
                    # must not stop (the first clean dev-mode install died on a stale key left in Credential
                    # Manager); it is forgotten and Settings takes a new one. A network error keeps it.
                    try:
                        validator(client, str(value))
                    except NsError as e:
                        if e.code == "NS-E503":
                            ctx.ui.say(f"Could not check the saved {label} key ({e.detail}); keeping it.",
                                       "yellow")
                            ctx.log.warn("key_unchecked", name=name, detail=e.detail)
                        else:
                            ctx.ui.say(f"The saved {label} key was not accepted ({e.detail}); it was "
                                       "removed. Add a key in Settings after setup.", "yellow")
                            ctx.log.warn("key_rejected_removed", name=name, detail=e.detail)
                            ctx.store.delete(name)
                            value = None
                elif value:
                    validator(client, str(value))
                elif ctx.ui.interactive:
                    ctx.ui.say(f"{label} key (free, optional, Enter to skip): {url}")
                    for attempt in range(3):
                        value = ctx.ui.ask(f"Paste your {label} API key", "", secret=True).strip()
                        if not value:
                            break
                        try:
                            validator(client, value)
                            break
                        except Exception as e:
                            ctx.ui.say(f"That key did not work: {e}", "red")
                            if attempt == 2:
                                raise
                if not value:
                    ctx.raw_answers[name] = None
                    ctx.log.info("key_skipped", name=name)
                    continue
                ctx.store.set(name, str(value))
                ctx.raw_answers[name] = None  # never keep a key in the working copy
                ctx.log.info("key_stored", name=name)
        finally:
            client.close()
        if not any_key_stored(ctx.store):
            # the answers are not resolved yet at this point, so read the raw one (a dev install ignores it)
            if ctx.raw_answers.get("fake_footage") is True and not ctx.dev_mode:
                ctx.ui.say("Engine test (fake_footage): story jobs use placeholder footage; "
                           "no stock-footage key is needed.")
            else:
                ctx.ui.say(NO_KEY_NOTICE, "yellow")
                ctx.log.warn("no_stock_key", hint="add a Pixabay key in Settings before the first video")

    # ---- run ----
    def run(self, ctx: StepContext) -> None:
        """Resolve every answer, prepare the home folder and write config/answers.json (no secrets)."""
        self._ran = True
        earlier = reinstall.kept_answers(ctx)
        diff = reinstall.changes(ctx, earlier[0], ctx.raw_answers) if earlier else []
        if earlier and diff:
            ctx.ui.say(f"{earlier[1]} differ from your earlier install's (config\\answers.json): "
                       f"{'; '.join(c.text() for c in diff)}. They win for every choice they name; the "
                       "choices they leave out stay as they were.", "yellow")
            ctx.log.info("answers_changed", changes=[c.key for c in diff])
        unfinished = reinstall.unfinished_choices(ctx)
        if unfinished:
            ctx.ui.say(f"The answers in {ctx.answers_path} replace choices saved by a setup window that did "
                       f"not finish ({'; '.join(c.text() for c in unfinished)}); the install follows the "
                       "answers in that file.", "yellow")
            ctx.log.info("unfinished_choices_replaced", changes=[c.key for c in unfinished])
        keys = reinstall.new_keys(ctx)
        if keys:        # never the value: the name of the service only
            ctx.ui.say("Your answers carry a " + " and a ".join(reinstall.KEY_LABELS.get(k, k) for k in keys)
                       + " API key: it is checked and kept in the Windows keyring, and removed from the "
                       "file afterwards.", "yellow")
            ctx.log.info("answers_keys_new", names=keys)
        if earlier:
            reinstall.inherit(ctx, earlier[0])
        raw = ctx.raw_answers
        merged: dict[str, Any] = {k: v for k, v in raw.items() if k not in KEY_NAMES}

        ram = float((ctx.preflight or {}).get("ram_gb") or 32)
        custom = None if ctx.dev_mode else raw.get("llm_custom") or None      # a dev install stubs Small only
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
        if ctx.dev_mode:
            tier = "small"          # stubs of the Small model; the RAM rule below does not apply
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

        flag = raw.get("include_voicedesign")
        merged["include_voicedesign"] = None if flag is None else bool(flag)

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
        ans = ctx.constrain(ans)
        ctx.answers = ans
        if ans.voice.mode == "design" and ans.include_voicedesign is False:
            ctx.warn("NS-W114", ans.voice.name or ans.voice.description or "")
        # What comes from the changed choices is applied BEFORE the answers are saved: when that fails, the
        # saved file still says what the files were made from, and a rerun sees the same change again.
        lines = reinstall.apply(ctx, earlier[0] if earlier else {}, ans, diff)
        write_answers(self._answers_file(ctx), ans)
        ctx.log.info("answers_saved", channel=slugify(ans.channel.display_name), tier=tier)
        self._summary(ctx, ans)
        for line in lines:
            ctx.ui.say(line)
        reinstall.forget_kept(ctx)

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
                ["Voice design model", voicedesign_row(ctx, ans)],
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
        ctx.log.info("answers_verified", tier=ans.llm_tier, stock_key=any_key_stored(ctx.store))
        if not self._ran:
            for line in reinstall.kept_lines(ctx, ans):
                ctx.ui.say(line)
