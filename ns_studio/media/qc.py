"""Quality control of rendered videos per spec §7.7 and the QC stage (component C6)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.models import QcCheck, QcResult
from ns_studio.context import JobContext
from ns_studio.media.audio import Loudness, measure_loudness, speech_onset_s
from ns_studio.stages import Tenant

SHORT_MAX_S = 179.0
BUCKET_TOLERANCE = 0.15
LONG_RANGE_S = (510.0, 900.0)
DRIFT_LIMIT_MS = 120.0
LUFS_TARGET, LUFS_TOL, TP_MAX = -14.0, 1.0, -1.0
FIRST_CAPTION_MAX_S = 0.05
SPEECH_ONSET_MAX_S = 0.3
_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")
Kind = Literal["short", "long"]
OPENS_LIMIT = "ffprobe ok, 1 video + 1 audio, faststart"


def duration_check(d: float, kind: Kind, bucket_s: float | None) -> QcCheck:
    """Shorts: within bucket +-15 % and <= 179 s. Masters: 8:30 to 15:00."""
    if kind == "long":
        lo, hi = LONG_RANGE_S
        return QcCheck(passed=lo <= d <= hi, value=round(d, 2), limit=f"{lo:.0f}-{hi:.0f} s")
    ok = d <= SHORT_MAX_S
    limit = f"<= {SHORT_MAX_S:.0f} s"
    if bucket_s:
        lo, hi = bucket_s * (1 - BUCKET_TOLERANCE), bucket_s * (1 + BUCKET_TOLERANCE)
        ok = ok and lo <= d <= hi
        limit = f"{lo:.1f}-{min(hi, SHORT_MAX_S):.1f} s (bucket {bucket_s:.0f} +-15 %), {limit}"
    return QcCheck(passed=ok, value=round(d, 2), limit=limit)


def loudness_check(m: Loudness) -> QcCheck:
    """-14 +-1 LUFS integrated and true peak <= -1 dBTP."""
    ok = (
        math.isfinite(m.lufs)
        and abs(m.lufs - LUFS_TARGET) <= LUFS_TOL
        and math.isfinite(m.true_peak)
        and m.true_peak <= TP_MAX
    )
    return QcCheck(
        passed=ok,
        value=f"{m.lufs:.1f} LUFS / {m.true_peak:.1f} dBTP",
        limit=f"{LUFS_TARGET:.0f}+-{LUFS_TOL:.0f} LUFS, TP <= {TP_MAX:.0f} dBTP",
    )


def drift_check(drift_ms: float | None) -> QcCheck:
    """Caption drift from the captions stage report: <= 120 ms."""
    ok = drift_ms is not None and drift_ms <= DRIFT_LIMIT_MS
    return QcCheck(passed=ok, value=None if drift_ms is None else round(drift_ms, 1), limit="<= 120 ms")


def parse_blackdetect(stderr: str) -> list[tuple[float, float, float]]:
    """(start, end, duration) of every black interval reported by blackdetect."""
    return [(float(a), float(b), float(c)) for a, b, c in _BLACK_RE.findall(stderr)]


def black_check(video: Path) -> QcCheck:
    """No interval of 0.3 s or more at >= 98 % black pixels."""
    proc = ff.run(
        ["-i", str(video), "-an", "-vf", "blackdetect=d=0.3:pic_th=0.98:pix_th=0.10", "-f", "null", "-"]
    )
    found = parse_blackdetect(proc.stderr)
    return QcCheck(passed=not found, value=len(found), limit="0 intervals > 0.3 s")


def opens_check(video: Path) -> QcCheck:
    """ffprobe opens it, 1 video + 1 audio stream, `moov` before `mdat` (faststart)."""
    try:
        streams = ff.probe(video)["streams"]
    except NsError as e:
        return QcCheck(passed=False, value=str(e.detail)[:120], limit=OPENS_LIMIT)
    v = sum(1 for s in streams if s.get("codec_type") == "video")
    a = sum(1 for s in streams if s.get("codec_type") == "audio")
    fast = ff.has_faststart(video)
    return QcCheck(
        passed=v == 1 and a == 1 and fast,
        value=f"{v} video, {a} audio, faststart={fast}",
        limit=OPENS_LIMIT,
    )


def hook_check(video: Path, first_caption_s: float | None) -> QcCheck:
    """First caption at 0.0 s and speech onset within 300 ms (Shorts only)."""
    onset = speech_onset_s(video)
    cap = first_caption_s if first_caption_s is not None else math.inf
    ok = cap <= FIRST_CAPTION_MAX_S and onset <= SPEECH_ONSET_MAX_S
    return QcCheck(
        passed=ok,
        value=f"caption {cap:.2f} s, speech {onset:.2f} s",
        limit="caption at 0.0 s, speech onset <= 0.3 s",
    )


def aspect_check(video: Path, kind: Kind) -> QcCheck:
    """1080x1920 for Shorts, 1920x1080 for masters."""
    want = (1080, 1920) if kind == "short" else (1920, 1080)
    got = ff.video_size(video)
    return QcCheck(passed=got == want, value=f"{got[0]}x{got[1]}", limit=f"{want[0]}x{want[1]}")


def run_qc(
    item: str,
    video: Path,
    kind: Kind,
    *,
    bucket_s: float | None = None,
    drift_ms: float | None = None,
    first_caption_s: float | None = None,
) -> QcResult:
    """All spec §7.7 checks for one video; `reason` lists the failed check names."""
    checks: dict[str, QcCheck] = {}
    checks["file_opens"] = opens_check(video)
    try:
        checks["duration"] = duration_check(ff.duration_s(video), kind, bucket_s)
        checks["loudness"] = loudness_check(measure_loudness(video))
        checks["caption_drift_ms"] = drift_check(drift_ms)
        checks["black_frames"] = black_check(video)
        if kind == "short":
            checks["hook_first_1500ms"] = hook_check(video, first_caption_s)
        checks["aspect"] = aspect_check(video, kind)
    except NsError as e:
        checks["file_opens"] = QcCheck(passed=False, value=str(e.detail)[:120], limit="video opens")
    failed = [k for k, c in checks.items() if not c.passed]
    return QcResult(item=item, passed=not failed, checks=checks, reason=", ".join(failed) or None)


class QcStage:
    """Stage `qc`: run §7.7 checks on every rendered Short; NS-E404 when any check fails.

    Results are written to `qc/<item>.json` (schema `qc`) and `qc/report.json` before the error is raised.
    """

    name: str = "qc"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("render/report.json", "captions/report.json")
    outputs: tuple[str, ...] = ("qc/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Check every item in `render/report.json`."""
        rendered: dict[str, Any] = ctx.read_json("render/report.json")
        captions: dict[str, Any] = ctx.read_json("captions/report.json")
        bucket = float(ctx.combo.length_bucket) if ctx.combo.length_bucket else None
        summary: dict[str, Any] = {}
        failed: list[str] = []
        for item, info in rendered.items():
            cap = captions.get(item, {})
            result = run_qc(
                item,
                Path(info["path"]),
                "short",
                bucket_s=bucket,
                drift_ms=cap.get("drift_ms"),
                first_caption_s=cap.get("first_caption_s"),
            )
            ctx.write_json(f"qc/{item}.json", result.model_dump(mode="json"))
            summary[item] = {"passed": result.passed, "reason": result.reason}
            if not result.passed:
                failed.append(f"{item}: {result.reason}")
        ctx.write_json("qc/report.json", summary)
        ctx.log.info("qc_done", job_id=ctx.job_id, results=summary)
        if failed:
            raise NsError("NS-E404", "; ".join(failed))
