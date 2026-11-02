"""Voice design and cloning for the Library (component C9, CR-C3-UI-7).

`POST /api/voices/design` and `POST /api/voices/clone` validate the request, check a clone sample with the
`ns_tts.sample` utilities (`NS-E312` when it is too short or noisy) and hand the work to the
`VoiceMakerAdapter`: the real one calls the TTS service through the supervisor, the fake uses `FakeEngine`.
Both answer `{voice_id, preview_wav_path}` once the voice is stored under `models/tts/voices/<name>/` and
its `preview.wav` (the test line) exists. The calls are synchronous: the response contract has no job id.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from ns_common.errors import NsError
from ns_common.models import VoiceMakeResponse
from ns_dashboard.adapters import MadeVoice
from ns_dashboard.errors import ApiError
from ns_dashboard.state import AppState

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
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
        if not NAME_RE.match(name):
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

    def design(self, body: dict[str, Any]) -> dict[str, Any]:
        """New voice from a plain-English description (VoiceDesign), previewed with `test_line`."""
        self._accepting()
        name = self._new_name(body)
        description = _text(body, "description", "The description", MAX_DESCRIPTION)
        test_line = _text(body, "test_line", "The test line", MAX_TEST_LINE)
        made = self.s.adapters.voice_maker.design(name, description, test_line)
        return self._answer("design", made)

    def clone(self, body: dict[str, Any]) -> dict[str, Any]:
        """New voice cloned from a recording on this PC; needs `consent: true`."""
        self._accepting()
        if body.get("consent") is not True:
            raise ApiError(
                422,
                "consent_required",
                "Please confirm that you have the right to clone this voice.",
                hint="Only clone your own voice or one you have written permission to use.",
            )
        name = self._new_name(body)
        test_line = _text(body, "test_line", "The test line", MAX_TEST_LINE)
        raw = str(body.get("sample_path") or "").strip()
        sample = Path(raw).expanduser() if raw else None
        if sample is None:
            raise ApiError(400, "no_sample", "Choose a recording of the voice.")
        if not sample.is_file():
            raise ApiError(400, "bad_path", f"'{raw}' is not a recording on this PC.")
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
