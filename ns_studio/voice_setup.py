"""Designed and cloned voices from the command line and from setup (component C7, gap 5).

`studio voice design|clone` use the same service calls as the dashboard's Library > Voices
(`POST /v1/voices/design` and `/v1/voices/clone` of `ns_tts`, spec J6): the studio holds the `tts` tenant
through the acquirer and the voice lands in the voice library folder (voice.json, speaker.bin, sample.wav).

Setup (`installer/stage2/steps/s07_configs.py`) cannot create the voice itself: the TTS engine is only chosen
by the smoke test after it (a voice made by qwentts is useless to the PyTorch fallback, NS-E311). It writes
`config\\pending_voice.json` instead, and the first real `studio run` (or `studio voice apply-pending`)
creates the voice from it. With `--fake-services=tts` the voice is made in process with `ns_tts`'s FakeEngine.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml

from ns_common.config import load_channel
from ns_common.errors import NsError
from ns_common.languages import qwen_language
from ns_common.models import VOICE_NAME_PATTERN, VOICE_NAME_RULE
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


def apply_pending_voices(ctx: JobContext) -> str | None:
    """Create the voice setup queued, for any run that is about to speak: `studio run`, the dashboard's Run
    now and series parts all go through `run_job_guarded`, which calls this first.

    Best effort, like the CLI's own `_setup_voice`: a failure is logged and the job goes on (the voice stage
    then fails with its own message). Does nothing when no voice is queued or the job uses the fake TTS.
    Returns the voice name when one was created.
    """
    from ns_common.tts_client import FakeTts

    if read_pending(ctx.paths) is None or isinstance(ctx.services.tts, FakeTts):
        return None
    env = Env(ctx.paths, ctx.db, ctx.runtime, ctx.services, ctx.log, frozenset())
    try:
        made = apply_pending(env, owner=f"studio:{ctx.job_id}")
    except (NsError, StudioError) as e:
        ctx.log.warn("pending_voice_failed", str(e), job_id=ctx.job_id)
        return None
    if made:
        ctx.log.info("pending_voice_created", voice=made, job_id=ctx.job_id)
    return made


def apply_pending(env: Env, *, post: Post | None = None, owner: str | None = None) -> str | None:
    """Create the voice setup queued, then forget the request. Returns its name, or None when nothing was
    queued or the voice already exists. On a failure the request stays queued and the error propagates."""
    req = read_pending(env.paths)
    if req is None:
        return None
    name = str(req["name"])
    if voice_exists(env.paths, name):
        pending_file(env.paths).unlink(missing_ok=True)
        return None
    language = str(req.get("language") or "") or _channel_language(env, req.get("channel"))
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
    pending_file(env.paths).unlink(missing_ok=True)
    env.log.info("voice_created_from_setup", voice=name, mode=req.get("mode"))
    return name
