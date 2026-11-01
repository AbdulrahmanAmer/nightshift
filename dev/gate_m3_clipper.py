"""M3 gate, Clipper part: a fake-services clip job on the synthetic fixture (component C8).

Asserts: at least 2 clips at 1080x1920, the facecam found within 5 % of its true position, captions
present (non-empty ASS), QC passed, upload sheets valid against the clip metadata model. Writes a
`GateReport` to `logs/gate-m3-clipper-<date>.json` and exits 0 only when every check passed.

Run from the repo root: `python dev/gate_m3_clipper.py`.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ns_common import ffmpeg  # noqa: E402
from ns_common.errors import NsError  # noqa: E402
from ns_common.models import Channel, GateCheck, GateReport  # noqa: E402
from ns_common.paths import Paths  # noqa: E402
from ns_studio import orchestrator  # noqa: E402
from ns_studio.clipper.models import ClipLayout, ClipMetadata, ClipParams  # noqa: E402
from ns_studio.clipper.stages_clip import create_clip_job  # noqa: E402
from ns_studio.context import FAKEABLE  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "clipper"
TOLERANCE = 0.05


def _load_fixture_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("clipper_make_sample", FIXTURE_DIR / "make_sample.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_gate(work: Path) -> GateReport:
    """Run the fake-services clip job under `work` and return the report."""
    fx = _load_fixture_module()
    paths = Paths(home=work / "home")
    paths.ensure()
    checks: list[GateCheck] = []

    def add(name: str, passed: bool, value: float | str | None = None, limit: str | None = None) -> None:
        checks.append(GateCheck(name=name, passed=bool(passed), value=value, limit=limit))

    sample = fx.make_sample(work / "vod")
    channel = Channel(slug="clip-gate", display_name="Clip Gate", niche="gaming walkthroughs")
    params = ClipParams(source=str(sample.video), channel=channel.slug, rights_confirmed=True)
    ctx = create_clip_job(paths, channel, params, fake=FAKEABLE, llm_fixtures=FIXTURE_DIR / "llm")
    t0 = time.perf_counter()
    benchmarks: dict[str, float] = {}
    try:
        ran = orchestrator.run_job(ctx)
        benchmarks["job_seconds"] = round(time.perf_counter() - t0, 1)
        add("job_completed", ran == orchestrator.FORMAT_STAGES["clip"], ",".join(ran))
    except NsError as e:
        add("job_completed", False, str(e))
        return _report(checks, benchmarks)

    mp4s = sorted((paths.outbox / channel.slug / "clips").glob("*/clip-*.mp4"))
    add("clips_at_least_2", len(mp4s) >= 2, len(mp4s), ">= 2")
    sizes = [ffmpeg.video_size(p) for p in mp4s]
    add("clips_1080x1920", bool(sizes) and all(s == (1080, 1920) for s in sizes), str(sizes[:1]), "1080x1920")

    layout = ClipLayout.model_validate(ctx.read_json("clip_layout.json"))
    truth = json.loads(sample.truth.read_text(encoding="utf-8"))
    cam, want = layout.facecam, truth["facecam"]
    err = 1.0
    if cam is not None:
        err = max(
            abs(cam.x - want["x"]) / truth["width"],
            abs(cam.w - want["w"]) / truth["width"],
            abs(cam.y - want["y"]) / truth["height"],
            abs(cam.h - want["h"]) / truth["height"],
        )
    add("facecam_within_5pct", cam is not None and err <= TOLERANCE, round(err, 4), "<= 0.05")
    add("template_gaming_facecam", layout.template == "gaming_facecam", layout.template)
    add(
        "templates_per_clip",
        layout.per_clip == {"1": "gaming_facecam", "2": "gaming_facecam"},
        json.dumps(layout.per_clip),
    )
    noted = "2" in layout.notes and "NS-W402" in layout.notes["2"]
    add("layout_fallback_noted", noted, json.dumps(layout.notes)[:120])

    analysis = ctx.read_json("analysis.json")
    lufs = analysis.get("loudness", {}).get("integrated_lufs")
    add("analysis_json", isinstance(analysis.get("scene_cuts"), list) and lufs is not None, lufs)
    plan = ctx.read_json("clip_plan.json")
    dropped = [c for c in plan if c["status"] == "dropped"]
    add("plan_records_drops", bool(dropped) and all(c["reasons"] for c in dropped), len(dropped))
    numbered = all(p.name.startswith(f"clip-{i:02d} ") for i, p in enumerate(mp4s, start=1))
    add(
        "outbox_layout",
        bool(mp4s) and numbered and all(p.parent.name == "sample" for p in mp4s),
        mp4s[0].parent.name if mp4s else None,
        "clips/<source name>/clip-NN <title>",
    )

    ass_files = sorted(ctx.job_dir.glob("captions/c*.ass"))
    texts = [p.read_text(encoding="utf-8") for p in ass_files]
    add("captions_present", len(texts) >= 2 and all("Dialogue:" in t for t in texts), len(texts))

    qc = ctx.read_json("qc/report.json")
    add("qc_passed", bool(qc) and all(v["passed"] for v in qc.values()), json.dumps(qc)[:200])

    valid = bool(mp4s)
    for mp4 in mp4s:
        try:
            ClipMetadata.model_validate_json(mp4.with_suffix(".json").read_text(encoding="utf-8"))
            valid = valid and mp4.with_suffix(".txt").stat().st_size > 0
        except (OSError, ValueError):
            valid = False
    add("sheets_valid", valid, len(mp4s))

    rows = ctx.db.query("SELECT 1 FROM outputs WHERE job_id=? AND kind='clip'", (ctx.job_id,))
    add("outputs_rows", len(rows) == len(mp4s) and len(rows) >= 2, len(rows))
    return _report(checks, benchmarks)


def _report(checks: list[GateCheck], benchmarks: dict[str, float]) -> GateReport:
    return GateReport(
        slice="m3-clipper",
        date=datetime.now(UTC).date().isoformat(),
        machine={"gpu": None, "ram_gb": None, "disk_type": None},
        checks=checks,
        benchmarks=benchmarks,
    )


def main() -> int:
    """Run the gate in a temp directory, write the report and print a one-line verdict per check."""
    if not ffmpeg.available():
        print("gate_m3_clipper: ffmpeg/ffprobe not found")
        return 2
    with tempfile.TemporaryDirectory(prefix="ns-gate-m3-") as tmp:
        report = run_gate(Path(tmp))
    out = ROOT / "logs" / f"gate-m3-clipper-{report.date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for c in report.checks:
        print(f"{'PASS' if c.passed else 'FAIL'} {c.name} {c.value if c.value is not None else ''}")
    print(f"gate_m3_clipper: {'PASSED' if report.passed else 'FAILED'} -> {out}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
