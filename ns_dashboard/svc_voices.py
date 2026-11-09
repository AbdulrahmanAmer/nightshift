"""Voice design and cloning for the Library (component C9, CR-C3-UI-7).

`POST /api/voices/design` and `POST /api/voices/clone` validate the request, check a clone sample with the
`ns_tts.sample` utilities (`NS-E312` when it is too short or noisy) and hand the work to the
`VoiceMakerAdapter`: the real one calls the TTS service through the supervisor, the fake uses `FakeEngine`.
A clone without `consent: true` answers `NS-E313` (422). Both answer `{voice_id, preview_wav_path}` once the
voice is stored under `models/tts/voices/<name>/` and its `preview.wav` (the test line) exists. The calls are
synchronous: the response contract has no job id.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from ns_common.errors import NsError, describe
from ns_common.models import VOICE_NAME_PATTERN, VoiceMakeResponse
from ns_dashboard.adapters import MadeVoice
from ns_dashboard.errors import ApiError
from ns_dashboard.state import AppState

NAME_RE = re.compile(VOICE_NAME_PATTERN)
MAX_DESCRIPTION = 500
MAX_TEST_LINE = 300


def _text(body: dict[str, Any], key: str, label: str, limit: int | None = None) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise ApiError(422, "invalid_voice", f"{label} cannot be empty.")
    if limit is not None and len(value) > limit:
        raise ApiError(422, "invalid_voice", f"{label} can be at most {limit} characters.")
    return value


def sample_error(e: NsError) -> ApiError:
    """`NS-E312` as a 422 in the dashboard envelope, keeping the code, the reason and the hint."""
    return ApiError(422, e.code, str(e), hint=e.definition.hint)


class VoicesService:
    """Creates voices in the Library bank."""

    def __init__(self, state: AppState) -> None:
        self.s = state

    def _accepting(self) -> None:
        if self.s.draining:
            raise ApiError(503, "shutting_down", "Nightshift is shutting down.")

    def _new_name(self, body: dict[str, Any]) -> str:
        from ns_tts.voices import PRESETS

        name = str(body.get("name") or "").strip()
        if not NAME_RE.fullmatch(name):
            raise ApiError(
                422,
                "invalid_voice",
                "Voice names are 1 to 40 letters, digits, - or _, starting with a letter or digit.",
            )
        taken = {p.lower() for p in PRESETS}
        root = self.s.paths.models_tts_voices
        if root.is_dir():
            taken |= {f.name.lower() for f in root.iterdir()}
        if name.lower() in taken:
            raise ApiError(
                409, "voice_exists", f"A voice called '{name}' already exists.", hint="Pick another name."
            )
        return name

    def _answer(self, kind: str, made: MadeVoice) -> dict[str, Any]:
        self.s.log.info("voice_made", kind=kind, voice=made.voice_id)
        return VoiceMakeResponse(
            voice_id=made.voice_id, preview_wav_path=made.preview_wav_path
        ).model_dump(mode="json")

    def pending(self) -> dict[str, Any]:
        """`{pending}`: the voice that setup asked for and that does not exist yet, or null."""
        return {"pending": self.s.adapters.voice_maker.pending()}

    def apply_pending(self) -> dict[str, Any]:
        """Queue the job that creates the voice setup asked for (kind `apply_voice`) and wake the worker.

        The job shows progress and can be cancelled from the Queue. Asking again while one is queued or
        running answers with that job. `{created: null, pending, job_id}`; `job_id` is null when nothing was
        queued by setup. A missing VoiceDesign model ends the job with `NS-E314` and the request stays queued.
        """
        self._accepting()
        pending = self.s.adapters.voice_maker.pending()
        if pending is None:
            return {"created": None, "pending": None, "job_id": None}
        busy = self.s.db.one(
            "SELECT job_id FROM dashboard_jobs WHERE kind = 'apply_voice' AND status IN ('queued', 'running')"
        )
        if busy is not None:
            return {"created": None, "pending": pending, "job_id": str(busy["job_id"])}
        rec = self.s.store.create("apply_voice", pending.get("channel"), {"name": pending["name"]})
        self.s.worker.enqueue(rec.job_id)
        self.s.hub.publish("dashboard_job", rec.to_dict())
        self.s.log.info("voice_setup_queued", voice=pending["name"], job_id=rec.job_id)
        return {"created": None, "pending": pending, "job_id": rec.job_id}

    def check_design(self, body: dict[str, Any]) -> None:
        """Refuse `design` for what is wrong with the request, making no voice (the Assistant's card gate)."""
        self._design_fields(body)

    def _design_fields(self, body: dict[str, Any]) -> tuple[str, str, str]:
        self._accepting()
        name = self._new_name(body)
        description = _text(body, "description", "The description", MAX_DESCRIPTION)
        test_line = _text(body, "test_line", "The test line", MAX_TEST_LINE)
        return name, description, test_line

    def design(self, body: dict[str, Any]) -> dict[str, Any]:
        """New voice from a plain-English description (VoiceDesign), previewed with `test_line`."""
        name, description, test_line = self._design_fields(body)
        made = self.s.adapters.voice_maker.design(name, description, test_line)
        return self._answer("design", made)

    def check_clone(self, body: dict[str, Any]) -> None:
        """Refuse `clone` for what is wrong with the request, making no voice (the Assistant's card gate).

        The recording is only looked for, not analysed: its length and noise are judged when it is used.
        """
        self._clone_fields(body)

    def _clone_fields(self, body: dict[str, Any]) -> tuple[str, str, Path]:
        self._accepting()
        if body.get("consent") is not True:
            d = describe("NS-E313")
            raise ApiError(422, "NS-E313", d.message, hint=d.hint)
        name = self._new_name(body)
        test_line = _text(body, "test_line", "The test line", MAX_TEST_LINE)
        raw = str(body.get("sample_path") or "").strip()
        sample = Path(raw).expanduser() if raw else None
        if sample is None:
            raise ApiError(400, "no_sample", "Choose a recording of the voice.")
        if not sample.is_file():
            raise ApiError(400, "bad_path", f"'{raw}' is not a recording on this PC.")
        return name, test_line, sample

    def clone(self, body: dict[str, Any]) -> dict[str, Any]:
        """New voice cloned from a recording on this PC; needs `consent: true`."""
        name, test_line, sample = self._clone_fields(body)
        from ns_tts.sample import prepare_sample

        with tempfile.TemporaryDirectory(prefix="ns-voice-") as td:
            try:
                wav, report = prepare_sample(sample, Path(td))
            except NsError as e:
                raise sample_error(e) from e
            self.s.log.info("voice_sample_ok", seconds=round(report.seconds, 1), snr_db=report.snr_db)
            try:
                made = self.s.adapters.voice_maker.clone(name, wav, test_line)
            except NsError as e:
                if e.code == "NS-E312":
                    raise sample_error(e) from e
                raise
        return self._answer("clone", made)
