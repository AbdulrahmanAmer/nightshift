"""A reinstall over kept data (component C1, defect D-14): which answers win, and what is rebuilt from them.

"Uninstall, keep my data" leaves ``config`` (the saved ``answers.json``, ``runtime.json``, the channel yaml
and its voice) and ``models``. A later setup may bring new answers, from a file next to INSTALL.cmd, from
``--answers`` or from the setup window. The rule, in one place:

* The new answers win for every choice they name; a choice they leave out stays as the earlier install had
  it. ``config/answers.json`` is then replaced by the result.
* ``runtime.json`` is made from the answers only once, so a changed tier used to download the new model while
  ``runtime.json`` kept the old ``model_path`` and its tuned flags. A changed tier or custom model rebuilds
  the llama part (model, kv, n_cpu_moe, speculation, context) and forgets that tuning ran. Ports, the video
  encoder, the whisper device and the voice engine the smoke test chose are the machine's, and stay.
* The channel yaml and the voice hold what the owner changed in the dashboard since. They are replaced only
  where the new answers name something different from what the kept answers recorded, field by field. A new
  channel name makes a new channel; the old one stays and is named.
* Every kept file that the new answers could have replaced but did not is named on screen, with how to
  change it.

The window writes its choices over ``config/answers.json`` before the wizard starts, so it first keeps the old
file as ``answers.kept.json`` (``answers_view.keep_earlier_answers``). The wizard compares against that file,
and removes it when the choices are applied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from installer.stage2.answers import AnswersError, InstallerAnswers, load_raw, slugify
from installer.stage2.context import StepContext
from installer.stage2.runtime_cfg import build_runtime
from installer.stage2.web.answers_view import KEPT_NAME
from ns_common.config import load_channel, load_runtime, save_channel, save_runtime
from ns_common.models import Voice

#: Choices compared as they are (their defaults come from ``InstallerAnswers``).
SIMPLE_KEYS = (
    "llm_tier",
    "llm_custom",
    "masters_per_week",
    "run_time",
    "install_code",
    "fake_voice",
    "fake_footage",
    "owner_name",
    "ui_theme",
)
CHANNEL_KEYS = ("display_name", "niche", "language", "seed_keywords")
#: What a file that does not name them inherits from the kept answers. Where the install lives, where the
#: models come from and the API keys are not inherited: they belong to this run.
INHERITED = (
    *SIMPLE_KEYS,
    "voice",
    "include_voicedesign",
    "channel",
    "voice_ref_audio_path",
    "voice_ref_text",
    "voice_rights_confirmed",
)


@dataclass(frozen=True)
class Change:
    """One choice that the new answers set to something other than the kept answers."""

    key: str
    old: Any
    new: Any

    def text(self) -> str:
        """``key: old -> new`` in words."""
        return f"{self.key}: {_show(self.old)} -> {_show(self.new)}"


def _show(value: Any) -> str:
    if isinstance(value, dict) and "mode" in value:
        who = value.get("speaker") or value.get("name") or value.get("description") or ""
        return f"{value['mode']} {who}".strip()
    if isinstance(value, dict):
        return str(value.get("file") or value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "none"
    return "none" if value is None else str(value)


# ---------------------------------------------------------------------------------------------------------
# the kept answers, and what differs
# ---------------------------------------------------------------------------------------------------------
def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def kept_answers(ctx: StepContext) -> tuple[dict[str, Any], str] | None:
    """The answers the earlier install's files were made from, with a phrase for the new source.

    The window keeps the old file as ``answers.kept.json``. Otherwise the saved ``config/answers.json`` counts
    when the answers in use come from another file (``--answers``, or next to INSTALL.cmd).
    """
    window = ctx.paths.config / KEPT_NAME
    saved = ctx.paths.config / "answers.json"
    src = ctx.answers_path
    if window.is_file():
        found, who = window, "The choices you made in the setup window"
    elif saved.is_file() and src is not None and not _same_file(src, saved):
        found, who = saved, f"The answers in {src}"
    else:
        return None
    try:
        return load_raw(found), who
    except AnswersError:
        return None


def _view(ctx: StepContext, d: dict[str, Any]) -> dict[str, Any]:
    """The choices of an answers dict as comparable values: defaults filled in, a dev install's limits on."""
    ans = ctx.constrain(InstallerAnswers.model_construct(**{k: v for k, v in d.items() if k in SIMPLE_KEYS}))
    out: dict[str, Any] = {k: getattr(ans, k) for k in SIMPLE_KEYS}
    if out["llm_custom"]:
        out["llm_tier"] = "small"  # s01 saves a custom model with the plain tier
    try:
        out["voice"] = Voice.model_validate(d.get("voice") or {}).model_dump()
    except ValueError:
        out["voice"] = d.get("voice")
    voice = out["voice"]
    if ctx.dev_mode:
        out["include_voicedesign"] = False
    elif d.get("include_voicedesign") is not None:
        out["include_voicedesign"] = bool(d["include_voicedesign"])
    else:
        out["include_voicedesign"] = isinstance(voice, dict) and voice.get("mode") == "design"
    channel: dict[str, Any] = d["channel"] if isinstance(d.get("channel"), dict) else {}
    for key in CHANNEL_KEYS:
        out[f"channel.{key}"] = channel.get(
            key, [] if key == "seed_keywords" else "en" if key == "language" else None
        )
    return out


def _named(new: dict[str, Any], key: str) -> bool:
    if key.startswith("channel."):
        channel = new.get("channel")
        return isinstance(channel, dict) and key[8:] in channel
    return key in new


def changes(ctx: StepContext, kept: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """The choices ``new`` names that differ from the kept answers. What it leaves out is not a change."""
    old, cur = _view(ctx, kept), _view(ctx, new)
    return [Change(k, old[k], cur[k]) for k in cur if _named(new, k) and old[k] != cur[k]]


def inherit(ctx: StepContext, kept: dict[str, Any]) -> None:
    """Fill the choices the new answers leave out from the kept ones (in ``ctx.raw_answers``)."""
    raw = ctx.raw_answers
    for key in INHERITED:
        if key in kept and key not in raw:
            raw[key] = kept[key]
    if isinstance(kept.get("channel"), dict) and isinstance(raw.get("channel"), dict):
        raw["channel"] = {**kept["channel"], **raw["channel"]}


def forget_kept(ctx: StepContext) -> None:
    """The window's copy of the earlier answers is used up."""
    (ctx.paths.config / KEPT_NAME).unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------------------
# what is rebuilt from the changed answers
# ---------------------------------------------------------------------------------------------------------
def apply(ctx: StepContext, kept: dict[str, Any], ans: InstallerAnswers, diff: list[Change]) -> list[str]:
    """Bring runtime.json, the channel yaml, the voice and the UI preferences in line with the answers that
    changed. Returns one plain line per thing that was done or deliberately kept."""
    keys = {c.key for c in diff}
    return [*_runtime(ctx, keys, bool(kept)), *_channel(ctx, kept, ans, keys), *_prefs(ctx, ans, keys)]


def _manifest_models(ctx: StepContext) -> set[str]:
    """File names of the language models that setup itself puts in runtime.json (any tier, any profile)."""
    files = [f for pm in ctx.manifest.profiles.values() for f in (pm.llm, pm.llm_balanced, pm.llm_best)]
    return {Path(f.file).name for f in files if f is not None}


def _runtime(ctx: StepContext, keys: set[str], compared: bool) -> list[str]:
    if not ctx.paths.runtime_file.exists():
        return []
    rt, fresh = load_runtime(ctx.paths), build_runtime(ctx)
    if (rt.profile, rt.dev_mode) != (fresh.profile, fresh.dev_mode):
        save_runtime(ctx.paths, fresh)  # made for another kind of install: all of it
        ctx.clear_flags("tuned")
        return [
            "config\\runtime.json was made for another kind of install (real or dev, GPU or not); "
            "it is built again from your answers."
        ]
    lines: list[str] = []
    was = Path(rt.llama.model_path).name
    # With no kept answers to compare (the saved file was deleted to start over) the model is the evidence:
    # one of setup's own files, for another tier than the answers name, is the earlier install's.
    asked = bool(keys & {"llm_tier", "llm_custom"}) or (not compared and was in _manifest_models(ctx))
    now = Path(fresh.llama.model_path).name
    if asked and was != now:
        rt.llama.model_path, rt.llama.flags = fresh.llama.model_path, fresh.llama.flags
        ctx.clear_flags("tuned")
        lines.append(
            f"Language model: config\\runtime.json now uses {now} (it had {was}) with that model's "
            f"settings, and tuning runs again. {was} stays in models\\llm; delete it by hand to free space."
        )
    if keys & {"fake_voice", "fake_footage"}:
        rt.fake_footage, rt.tts.engine = fresh.fake_footage, fresh.tts.engine
        lines.append("Engine test: config\\runtime.json follows your fake_voice and fake_footage answers.")
    if not lines:
        return [
            "Kept config\\runtime.json (ports, tuning, engines). To make setup build it again from your "
            "answers, delete it and run setup again."
        ]
    save_runtime(ctx.paths, rt)
    return lines


def _s07() -> Any:
    """The configs step's helpers (imported late: ``steps`` imports this module through s01)."""
    from installer.stage2.steps import s07_configs

    return s07_configs


def _channel(ctx: StepContext, kept: dict[str, Any], ans: InstallerAnswers, keys: set[str]) -> list[str]:
    slug = slugify(ans.channel.display_name)
    lines: list[str] = []
    old_slug = slugify(str((kept.get("channel") or {}).get("display_name") or ""))
    if "channel.display_name" in keys and old_slug != slug and ctx.paths.channel_file(old_slug).exists():
        lines.append(
            f"Your answers name a new channel, '{slug}'. Channel '{old_slug}' was kept and still "
            "runs; pause or delete it in the dashboard (Channels) if you no longer want it."
        )
    if not ctx.paths.channel_file(slug).exists():
        return lines  # settings and secrets (s07) make it
    channel, wanted = load_channel(ctx.paths, slug), _s07().channel_from_answers(ans)
    done: list[str] = []
    for key, field in (
        ("channel.niche", "niche"),
        ("channel.language", "language"),
        ("channel.seed_keywords", "seed_keywords"),
        ("masters_per_week", "masters_per_week"),
        ("voice", "voice"),
    ):
        if key in keys:
            setattr(channel, field, getattr(wanted, field))
            done.append(field.replace("_", " "))
    if done:
        save_channel(ctx.paths, channel)
        lines.append(
            f"Channel '{slug}': your answers changed {', '.join(done)}; "
            f"config\\channels\\{slug}.yaml has them. Everything else in it (your dashboard edits) "
            "was kept."
        )
    else:
        lines.append(
            f"Kept channel '{slug}' and its voice as they are (config\\channels\\{slug}.yaml); "
            "to make setup write them again, delete that file and run setup again."
        )
    if "voice" in keys:
        lines += _voice(ctx, ans, channel)
    return lines


def _voice(ctx: StepContext, ans: InstallerAnswers, channel: Any) -> list[str]:
    s07, voice = _s07(), channel.voice
    if voice.mode == "preset":
        try:
            pending = s07.pending_voice_file(ctx)
            if json.loads(pending.read_text(encoding="utf-8")).get("channel") == channel.slug:
                pending.unlink()  # a designed voice nobody needs any more
        except (OSError, ValueError, AttributeError):
            pass
        return []
    if s07.queue_voice(ctx, ans, channel):
        return [
            f"Your {voice.mode} voice '{voice.name}' is created the first time Nightshift runs. "
            "To make it now: studio voice apply-pending."
        ]
    return [
        f"A voice named '{voice.name}' already exists in models\\tts\\voices, so it is used as it is. "
        "To make a different one, give the voice another name in your answers."
    ]


def _prefs(ctx: StepContext, ans: InstallerAnswers, keys: set[str]) -> list[str]:
    if not keys & {"owner_name", "ui_theme"}:
        return []
    f = ctx.paths.config / "ui_prefs.json"
    try:
        prefs = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # s07 writes it from the answers
    if "owner_name" in keys:
        prefs["owner_name"] = ans.owner_name
    if "ui_theme" in keys:
        prefs["theme"] = ans.ui_theme
    f.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
    return ["Your name and theme in config\\ui_prefs.json follow your answers now."]


# ---------------------------------------------------------------------------------------------------------
# what was kept when nothing was applied
# ---------------------------------------------------------------------------------------------------------
def kept_lines(ctx: StepContext, ans: InstallerAnswers) -> list[str]:
    """When the answers were not applied over kept data: say which earlier files setup is using.

    Nothing changed, so ``apply`` with no changes only repairs a runtime.json made for another kind of install
    and names the kept runtime.json and channel.
    """
    if not ctx.paths.runtime_file.exists():
        return []
    src = ctx.answers_path
    if src is not None and not _same_file(src, ctx.paths.config / "answers.json"):
        first = f"The answers in {src} name the same choices as your earlier install; nothing was changed."
    else:
        first = (
            "Using the answers from your earlier install (config\\answers.json). To change a choice, "
            "put an answers.json that names it next to INSTALL.cmd; to be asked again, delete "
            "config\\answers.json."
        )
    return [first, *apply(ctx, {}, ans, [])]
