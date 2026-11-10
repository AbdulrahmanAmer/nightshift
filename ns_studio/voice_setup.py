"""Designed and cloned voices from the command line and from setup (component C7, gap 5).

`studio voice design|clone` use the same service calls as the dashboard's Library > Voices
(`POST /v1/voices/design` and `/v1/voices/clone` of `ns_tts`, spec J6): the studio holds the `tts` tenant
through the acquirer and the voice lands in the voice library folder (voice.json, speaker.bin, sample.wav).

Setup (`installer/stage2/steps/s07_configs.py`) cannot create the voice itself: the TTS engine is only chosen
by the smoke test after it (a voice made by qwentts is useless to the PyTorch fallback, NS-E311). It writes
`config\\pending_voice.json` instead, and the first real `studio run` (or `studio voice apply-pending`)
creates the voice from it. With `--fake-services=tts` the voice is made in process with `ns_tts`'s FakeEngine.

A request queued by 0.1.13 or 0.1.14 under a name the library refuses ("Night Narrator") is made under
the suggested name and the channel is pointed at it (`apply_pending_report`, defect D-24); what the owner
reads is one sentence per state, the same on the CLI, in the dashboard job and in the morning note.
"""

from __future__ import annotations

import importlib
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import yaml

from ns_common.atomic import write_text_atomic
from ns_common.config import load_channel, save_channel
from ns_common.errors import NsError
from ns_common.languages import qwen_language
from ns_common.logging import NsLogger
from ns_common.models import VOICE_NAME_PATTERN, VOICE_NAME_RULE, Channel, suggest_voice_name
from ns_common.paths import Paths
from ns_studio.context import JobContext
from ns_studio.errors import StudioError
from ns_studio.jobs import Env

PENDING_NAME = "pending_voice.json"
NAME_RE = re.compile(VOICE_NAME_PATTERN)
Post = Callable[[str, str, dict[str, Any]], dict[str, Any]]


def pending_file(paths: Paths) -> Path:
    """`config\\pending_voice.json`: the voice setup asked for, until it exists."""
    return paths.config / PENDING_NAME


def voice_exists(paths: Paths, name: str) -> bool:
    """True when `models\\tts\\voices\\<name>` holds a stored voice."""
    return (paths.models_tts_voices / name / "voice.json").is_file()


def read_pending(paths: Paths) -> dict[str, Any] | None:
    """The queued request, or None (no file, unreadable, or not an object)."""
    try:
        data = json.loads(pending_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("name") else None


def _check_name(paths: Paths, name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise NsError("NS-E310", VOICE_NAME_RULE)
    if voice_exists(paths, name):
        raise NsError("NS-E310", f"a voice called '{name}' already exists")


def http_post(base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST to the TTS service; a missing model, a refusal and a dead service become coded errors."""
    try:
        r = httpx.post(f"{base_url.rstrip('/')}{route}", json=body, timeout=1800)
    except httpx.HTTPError as e:
        raise NsError("NS-E310", f"voice service: {e}") from e
    if r.status_code == 409 and "missing_model" in r.text:
        try:
            answer = r.json()
        except ValueError:
            answer = {}
        code = answer.get("error") if isinstance(answer, dict) else None
        if code in ("NS-E314", "NS-E202", "NS-E315"):  # the service says which model is absent
            raise NsError(str(code), str(answer.get("detail", "")))
        raise NsError(
            "NS-E310",
            "the model for this voice tool is not downloaded; open Settings > Models > Get voice design "
            "(or run: ns models get voicedesign)",
        )
    if r.status_code >= 400:
        try:
            detail = str(r.json().get("detail") or r.json().get("error") or r.text)
        except ValueError:
            detail = r.text
        raise NsError("NS-E310", f"voice service answered {r.status_code}: {detail[:300]}")
    out = r.json()
    return out if isinstance(out, dict) else {}


def _local_engine_make(env: Env, kind: str, **kw: Any) -> None:
    """In-process voice for `--fake-services=tts` (the same library layout the real service writes)."""
    from ns_tts.engine import FakeEngine
    from ns_tts.voices import VoiceLibrary

    lib = VoiceLibrary(env.paths.models_tts_voices)
    language = qwen_language(kw["language"])[0]  # the service maps ISO codes the same way
    try:
        if kind == "design":
            lib.add_design(kw["name"], kw["description"], engine=FakeEngine(), language=language)
        else:
            lib.add_clone(
                kw["name"],
                kw["ref_audio_path"],
                kw["ref_text"],
                True,
                engine=FakeEngine(),
                language=language,
            )
    except ValueError as e:
        raise NsError("NS-E310", str(e)) from e


def _make(
    env: Env, route: str, body: dict[str, Any], post: Post | None, owner: str | None = None
) -> None:
    """Hold the `tts` tenant, call the service, release.

    `owner` is the GPU owner name: a job passes its own `studio:<job id>`, so the dashboard's Cancel (which
    frees the owners of the job) frees this lease too; alone it is `studio:voice-<name>`.
    """
    owner = owner or f"studio:voice-{body['name']}"
    endpoint = env.services.acquirer.acquire("tts", owner, timeout_s=600)
    try:
        (post or http_post)(endpoint.base_url, route, body)
    finally:
        env.services.acquirer.release("tts", owner)


def design_voice(
    env: Env, name: str, description: str, *, language: str = "English", post: Post | None = None,
    owner: str | None = None,
) -> Path:
    """New voice from a plain-English description (VoiceDesign). Returns the folder of the stored voice."""
    _check_name(env.paths, name)
    if not description.strip():
        raise NsError("NS-E310", "the voice description is empty")
    if "tts" in env.fake and post is None:
        _local_engine_make(env, "design", name=name, description=description, language=language)
    else:
        body = {"name": name, "description": description, "language": language}
        _make(env, "/v1/voices/design", body, post, owner)
    return env.paths.models_tts_voices / name


def clone_voice(
    env: Env,
    name: str,
    ref_audio: Path,
    ref_text: str,
    *,
    rights_confirmed: bool,
    language: str = "English",
    post: Post | None = None,
    owner: str | None = None,
) -> Path:
    """New voice cloned from a 3 to 30 s recording and the words said in it (`NS-E313` without consent,
    `NS-E312` when the recording is too short or noisy)."""
    if not rights_confirmed:
        raise NsError("NS-E313")
    _check_name(env.paths, name)
    if not ref_audio.is_file():
        raise NsError("NS-E312", f"no recording at {ref_audio}")
    if not ref_text.strip():
        raise NsError("NS-E312", "give the words that are spoken in the recording")
    from ns_tts.sample import prepare_sample

    with tempfile.TemporaryDirectory(prefix="ns-voice-") as td:
        wav, _report = prepare_sample(ref_audio, Path(td))
        body = {
            "name": name,
            "ref_audio_path": str(wav),
            "ref_text": ref_text.strip(),
            "rights_confirmed": True,
            "language": language,
        }
        if "tts" in env.fake and post is None:
            _local_engine_make(env, "clone", **{k: v for k, v in body.items() if k != "rights_confirmed"})
        else:
            _make(env, "/v1/voices/clone", body, post, owner)
    return env.paths.models_tts_voices / name


def _channel_language(env: Env, slug: object) -> str:
    """The language of the channel that queued the voice (setup writes no language), else English."""
    try:
        return load_channel(env.paths, str(slug)).language if slug else "en"
    except (OSError, ValueError, yaml.YAMLError):
        return "en"


class Applied(NamedTuple):
    """What became of the request: the voice made (`name`), the name setup asked for when that was not usable
    (`renamed_from`), the channel that now uses the voice (`channel`), or, with no `name`, the sentence that
    says why the request was dropped without making a voice (`dropped`)."""

    name: str | None
    renamed_from: str | None = None
    channel: str | None = None
    dropped: str = ""

    def said(self) -> str:
        """The one sentence the owner reads on every route: the CLI line, the dashboard job's message and
        the morning note of a run nobody watched."""
        if self.name is None:
            return self.dropped
        if self.renamed_from is None:
            return f"voice {self.name} created"
        said = f"voice {self.name} created (setup asked for '{self.renamed_from}'; {_why(self.renamed_from)})"
        return f"{said}; channel {self.channel} now uses it" if self.channel else said


class _Profile(NamedTuple):
    """The request's channel as it is on disk now: `ok` with the profile, `gone` (no such file, or the
    request names no channel) or `unreadable` with the reason (a typo, a file another program holds)."""

    state: str
    channel: Channel | None = None
    problem: str = ""

    def voice_name(self) -> str | None:
        """The stored voice the channel asks for; None for a built-in voice or no channel."""
        if self.channel is None or self.channel.voice.mode == "preset":
            return None
        return self.channel.voice.name


#: Said after every "press Create it now": the two ways to run the request again by hand.
AGAIN = "press Create it now in Library > Voices (or run: studio voice apply-pending)"
#: The second line of a refusal on the CLI and on the Queue card (the NS-E310 text speaks of the engine).
OWN_HINT = "The voice engine is not at fault: the message says what to do."
NOTE_TITLE = "Nightshift"


def _why(asked: str) -> str:
    return "voice names cannot have spaces" if any(c.isspace() for c in asked) else VOICE_NAME_RULE


def _refusal(code: str, detail: str, kind: str = "wait") -> NsError:
    """A coded stop whose sentence is whole (what happened and the way out): shown as it is on every route.
    `kind` "choose" is the one only the owner can end (the channel needs another voice)."""
    return NsError(code, detail, pending_voice=kind, hint=OWN_HINT)


def problem_said(e: Exception) -> str:
    """The sentence for a request that could not be finished, the same in the `studio run` warning and in
    the morning note: a refusal says itself, any other failure gets the way to try again."""
    if isinstance(e, NsError) and e.data.get("pending_voice"):
        return f"{e.code}: {e.detail}"
    return f"the voice chosen during setup is not created yet ({e}); {AGAIN}"


def leave_note(paths: Paths, log: NsLogger, message: str) -> bool:
    """Leave `message` as a morning note (`run/morning_note.json`, shown on the Queue page), once: a note
    with the same words that still waits is not repeated. False only when the note could not be written now
    (the file is held); an install without the note store has nowhere to say it and counts as said."""
    try:
        notify = importlib.import_module("ns_supervisor.notify")
        if any(n.get("message") == message for n in notify.read_notes(paths)):
            return True
        notify.leave_note(paths, NOTE_TITLE, message)
    except (ImportError, AttributeError) as e:
        log.warn("pending_voice_note_unavailable", str(e))
        return True
    except (OSError, NsError) as e:
        log.warn("pending_voice_note_failed", str(e))
        return False
    log.info("pending_voice_noted", message=message)
    return True


def name_taken(paths: Paths, name: str) -> bool:
    """True when the voice library would refuse `name` as already used: a built-in voice or a folder of
    that name, in any letter case (`ns_tts.voices.VoiceLibrary._check_new`)."""
    try:
        presets = importlib.import_module("ns_tts.voices").PRESETS
    except (ImportError, AttributeError):
        presets = []
    taken = {str(p).lower() for p in presets}
    root = paths.models_tts_voices
    if root.is_dir():
        taken |= {f.name.lower() for f in root.iterdir()}
    return name.lower() in taken


def _profile(env: Env, slug: object) -> _Profile:
    if not isinstance(slug, str) or not slug:
        return _Profile("gone")
    try:
        return _Profile("ok", load_channel(env.paths, slug))
    except FileNotFoundError:
        return _Profile("gone")
    except (OSError, ValueError, yaml.YAMLError) as e:
        return _Profile("unreadable", None, " ".join(str(e).split())[:200])


def _readable_again(env: Env, slug: object) -> str:
    return (
        f"Make {env.paths.channel_file(str(slug))} readable again (correct the typo, or close the program "
        "that holds it; 'studio channel list' shows what is wrong)"
    )


def _forget(env: Env, done: str = "") -> None:
    """Remove the request. A file that cannot be removed now stays for the next run, which ends the same
    way (every step before this one is safe to repeat)."""
    f = pending_file(env.paths)
    try:
        f.unlink(missing_ok=True)
    except OSError as e:
        raise _refusal(
            "NS-E119",
            f"{done}the request file {f} could not be removed ({e}). Close the program that has it open; "
            f"the next run removes it, or {AGAIN}",
        ) from e


def _make_voice(env: Env, req: dict[str, Any], name: str, post: Post | None, owner: str | None) -> None:
    """Design or clone `name` from the request. When that fails and the voice is there after all (another
    route made it from the same request at the same moment), it is made: nothing failed."""
    language = str(req.get("language") or "") or _channel_language(env, req.get("channel"))
    try:
        if req.get("mode") == "clone":
            clone_voice(
                env,
                name,
                Path(str(req.get("ref_audio_path") or "")),
                str(req.get("ref_text") or ""),
                rights_confirmed=bool(req.get("rights_confirmed")),
                language=language,
                post=post,
                owner=owner,
            )
        else:
            design_voice(
                env, name, str(req.get("description") or ""), language=language, post=post, owner=owner
            )
    except (NsError, StudioError):
        if not voice_exists(env.paths, name):
            raise
        env.log.info("pending_voice_made_meanwhile", voice=name)


def _point_channel(env: Env, slug: object, asked: str, new: str) -> bool:
    """Make the request's channel use `new` when it still names `asked`; True when it uses `new` afterwards.

    The profile is read again here (making the voice took a minute): one the owner changed meanwhile and a
    channel that is gone are left alone. One that cannot be read or saved now stops with the request kept,
    so the channel is never left on the name that cannot exist with nothing queued to repoint it.
    """
    prof = _profile(env, slug)
    done = f"voice {new} is created (setup asked for '{asked}'; {_why(asked)}), but channel {slug} still"
    if prof.state == "unreadable":
        raise _refusal(
            "NS-E310",
            f"{done} names the old name in a profile that cannot be read ({prof.problem}). "
            f"{_readable_again(env, slug)}; the next run then finishes it, or {AGAIN}",
        )
    channel = prof.channel
    if channel is None:
        return False
    if prof.voice_name() == new:
        return True
    if prof.voice_name() != asked:
        return False
    voice = channel.voice.model_copy(update={"name": new})
    try:
        save_channel(env.paths, channel.model_copy(update={"voice": voice}))
    except NsError as e:  # NS-E119: another program holds the profile
        raise _refusal(
            e.code,
            f"{done} names '{asked}': its profile could not be saved ({e.detail}). Close the program "
            f"that has that file open; the next run then finishes it, or {AGAIN}",
        ) from e
    return True


def apply_pending(env: Env, *, post: Post | None = None, owner: str | None = None) -> str | None:
    """Create the voice setup queued, then forget the request. Returns its name, or None when nothing was
    queued, the voice already exists or the request was dropped. On a failure the request stays queued and
    the error propagates."""
    made = apply_pending_report(env, post=post, owner=owner)
    return made.name if made else None


def apply_pending_report(
    env: Env,
    *,
    post: Post | None = None,
    owner: str | None = None,
    tell: Callable[[str], bool] | None = None,
) -> Applied | None:
    """`apply_pending` that also says what became of a name the voice library refuses (queued by 0.1.13 and
    0.1.14 setup as "Night Narrator").

    Invariant, true after every step and so after a kill at any of them: the request is removed last, so a
    channel never names a voice that does not exist while nothing is queued to make or repoint it; a name is
    taken over only when this request made it (`renamed_from`); and no state refuses for ever (each refusal
    ends by something the owner can do in the product, and a request no channel asks for is dropped).

    `tell` is the route's way to say the rename to an owner who is not watching (the morning note); when it
    answers False the request is kept, so the next run says it. Returns None when nothing was queued or the
    voice asked for exists already.
    """
    req = read_pending(env.paths)
    if req is None:
        return None
    name = str(req["name"])
    old = req.get("renamed_from")
    old = old if isinstance(old, str) and old and NAME_RE.fullmatch(name) else None
    if old is not None or not NAME_RE.fullmatch(name):
        asked = old or name
        return _apply_old_shaped(
            env, req, asked, name if old else suggest_voice_name(asked), old is not None, post, owner, tell
        )
    if voice_exists(env.paths, name):  # made already (by the owner, or a run killed before it forgot)
        _forget(env)
        return None
    _make_voice(env, req, name, post, owner)
    _forget(env, f"voice {name} is created, but ")
    env.log.info("voice_created_from_setup", voice=name, mode=req.get("mode"))
    return Applied(name)


def _apply_old_shaped(
    env: Env,
    req: dict[str, Any],
    asked: str,
    new: str,
    rewritten: bool,
    post: Post | None,
    owner: str | None,
    tell: Callable[[str], bool] | None,
) -> Applied | None:
    """The request whose name (`asked`) the library refuses; `new` stands for it, and `rewritten` says the
    request already records that (`name` new, `renamed_from` asked: this request owns `new`).

    Steps, each safe to run again after a kill: stop or drop (nothing changed yet); rewrite the request
    (atomic); make the voice; point the channel; say it; remove the request.
    """
    slug = req.get("channel")
    if voice_exists(env.paths, asked):  # stored under that very name by an old library: nothing is owed
        _forget(env)
        return None
    prof = _profile(env, slug)
    if prof.state == "unreadable" and not (rewritten and voice_exists(env.paths, new)):
        raise _refusal(
            "NS-E310",
            f"the voice setup asked for ('{asked}') is waiting: the profile of channel {slug} cannot be "
            f"read ({prof.problem}). {_readable_again(env, slug)}, then {AGAIN}",
        )
    made = rewritten and voice_exists(env.paths, new)
    uses = prof.voice_name()
    if prof.state == "gone" or (prof.state == "ok" and uses != asked):
        if made:  # killed after the pointing, or the owner chose another voice meanwhile: say it, forget
            return _finish(env, req, asked, new, slug if uses == new else None, tell)
        if prof.state == "gone":
            why = f"channel {slug} is gone" if slug else "no channel asks for it"
        elif uses is None:
            why = f"channel {slug} now uses a built-in voice"
        else:
            why = f"channel {slug} now uses '{uses}'"
        _forget(env)
        env.log.info("pending_voice_dropped", voice=new, asked_for=asked, why=why)
        said = f"the voice setup asked for ('{asked}') is not needed any more: {why}; nothing was created"
        return Applied(None, dropped=said)
    if not made and name_taken(env.paths, new):
        raise _refusal(
            "NS-E310",
            f"setup asked for a voice '{asked}'; that name cannot be used ({_why(asked)}) and '{new}' is "
            f"already another voice. Open Channels > Edit channel for {slug}, choose a narrator under Voice "
            f"('{new}' or any other) and press Save channel (or run: studio channel edit {slug} --set "
            f"voice.name={new}); this request then goes away by itself",
            "choose",
        )
    if not rewritten:
        req = {**req, "name": new, "renamed_from": asked}
        write_text_atomic(pending_file(env.paths), json.dumps(req, indent=2, ensure_ascii=False) + "\n")
    if not made:
        _make_voice(env, req, new, post, owner)
    pointed = _point_channel(env, slug, asked, new)
    return _finish(env, req, asked, new, slug if pointed else None, tell)


def _finish(
    env: Env,
    req: dict[str, Any],
    asked: str,
    new: str,
    channel: object,
    tell: Callable[[str], bool] | None,
) -> Applied:
    """The voice is made and the channel is settled: say it, then (and only then) remove the request."""
    applied = Applied(new, asked, channel if isinstance(channel, str) else None)
    if tell is not None and not tell(applied.said()):
        env.log.warn("pending_voice_kept", "the rename could not be noted; the next run says it", voice=new)
        return applied
    _forget(env, f"voice {new} is created (setup asked for '{asked}'; {_why(asked)}), but ")
    env.log.info("voice_created_from_setup", voice=new, mode=req.get("mode"))
    env.log.info("pending_voice_renamed", voice=new, renamed_from=asked)
    return applied


def apply_for_run(
    env: Env, *, post: Post | None = None, owner: str | None = None
) -> tuple[Applied | None, Exception | None]:
    """The request, applied for a run nobody may be watching (`studio run`, the nightly, Run now, a series
    part): a rename and a failure are left as a morning note in the words the other routes use. Never
    raises a coded error: `(what was applied, the failure)`; the request of a failed try stays queued."""
    try:
        return apply_pending_report(
            env, post=post, owner=owner, tell=lambda said: leave_note(env.paths, env.log, said)
        ), None
    except (NsError, StudioError) as e:
        env.log.warn("pending_voice_failed", str(e))
        leave_note(env.paths, env.log, problem_said(e))
        return None, e


def apply_pending_voices(ctx: JobContext) -> str | None:
    """Create the voice setup queued, for any run that is about to speak: `studio run`, the dashboard's Run
    now and series parts all go through `run_job_guarded`, which calls this first.

    Best effort, like the CLI's own `_setup_voice`: a failure is logged and noted and the job goes on (the
    voice stage then fails with its own message). The one exception is the job that could only fail: its
    channel still names the name that cannot exist and only the owner can settle it, so it stops here with
    that sentence. The job's own profile was loaded before this ran, so its voice is brought up to date
    (`_use_stored_voice`). Does nothing when the job uses the fake TTS. Returns the voice name when one was
    created.
    """
    from ns_common.tts_client import FakeTts

    if isinstance(ctx.services.tts, FakeTts):
        return None
    req = read_pending(ctx.paths)
    made: Applied | None = None
    failed: Exception | None = None
    if req is not None:
        env = Env(ctx.paths, ctx.db, ctx.runtime, ctx.services, ctx.log, frozenset())
        made, failed = apply_for_run(env, owner=f"studio:{ctx.job_id}")
        if made and made.name:
            ctx.log.info("pending_voice_created", voice=made.name, job_id=ctx.job_id)
    _use_stored_voice(ctx)
    if isinstance(failed, NsError) and failed.data.get("pending_voice") == "choose" and req is not None:
        from ns_studio.media.voice import job_voice

        voice = job_voice(ctx)
        asked = req.get("renamed_from") or req["name"]
        if req.get("channel") == ctx.channel.slug and voice.mode != "preset" and voice.name == asked:
            raise failed
    return made.name if made else None


def _use_stored_voice(ctx: JobContext) -> None:
    """A job whose profile (loaded when the job was made, or when the night was planned) names a voice that
    is not stored takes the voice its profile on disk names now, or the one the request made for that name
    while the profile could not be saved. The first job after a rename, and a second job of the same night,
    then speak with the voice that exists."""
    voice = ctx.channel.voice
    if voice.mode == "preset" or not voice.name or voice_exists(ctx.paths, voice.name):
        return
    now = None
    try:
        on_disk = load_channel(ctx.paths, ctx.channel.slug).voice
    except (OSError, ValueError, yaml.YAMLError):
        on_disk = None
    if on_disk is not None and (
        on_disk.mode == "preset" or (on_disk.name and voice_exists(ctx.paths, on_disk.name))
    ):
        now = on_disk
    else:
        req = read_pending(ctx.paths)
        if (
            req is not None
            and req.get("channel") == ctx.channel.slug
            and req.get("renamed_from") == voice.name
            and voice_exists(ctx.paths, str(req["name"]))
        ):
            now = voice.model_copy(update={"name": str(req["name"])})
    if now is None or now == voice:
        return
    ctx.channel = ctx.channel.model_copy(update={"voice": now})
    ctx.log.info(
        "job_voice_updated", job_id=ctx.job_id, was=voice.name, voice=now.name or now.speaker
    )
