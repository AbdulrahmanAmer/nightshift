from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.embed_client import FakeEmbed
from ns_common.llm_client import FakeLlm
from ns_common.models import GateCheck, GateReport
from ns_common.paths import Paths, repo_root
from ns_common.tts_client import FakeTts

PY = sys.executable
SLICE = "m0-c3"


def _sub(name: str, args: list[str]) -> GateCheck:
    p = subprocess.run([PY, *args], capture_output=True, text=True, cwd=repo_root())
    return GateCheck(
        name=name, passed=p.returncode == 0, value=p.returncode, log=(p.stdout + p.stderr)[-2000:]
    )


def run_checks(*, skip_slow: bool = False) -> GateReport:
    checks: list[GateCheck] = []
    if not skip_slow:
        checks.append(_sub("pytest", ["-m", "pytest", "-q"]))
        checks.append(_sub("ruff", ["-m", "ruff", "check", "."]))
        checks.append(_sub("pyright", ["-m", "pyright"]))
        checks.append(_sub("manifest_check", ["tools/resolve_manifest.py", "--check"]))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            FakeLlm(repo_root() / "tests" / "fixtures" / "llm").complete_json(
                prompt_name="_example", system="", user="", schema={"type": "object"})
            checks.append(GateCheck(name="fake_llm_schema", passed=True))
        except Exception as e:
            checks.append(GateCheck(name="fake_llm_schema", passed=False, log=str(e)))
        secs = FakeTts().speak("hello from the gate", "Ryan", 1.0, tmp / "t.wav")
        checks.append(GateCheck(name="fake_tts_wav", passed=secs >= 0.4, value=secs, limit=">=0.4"))
        vec = FakeEmbed().embed(["x"])[0]
        checks.append(GateCheck(name="fake_embed_vector", passed=len(vec) == 256, value=len(vec), limit="256"))
        try:
            clip = ff.make_test_clip(tmp / "c.mp4", width=320, height=180, seconds=2.0)
            dur = ff.duration_s(clip)
            ok = ff.has_faststart(clip) and 1.9 <= dur <= 2.1
            checks.append(GateCheck(name="ffmpeg_clip", passed=ok, value=dur, limit="2.0+-0.1"))
        except Exception as e:
            checks.append(GateCheck(name="ffmpeg_clip", passed=False, log=str(e)))
    return GateReport(
        slice=SLICE,
        date=datetime.now(UTC).isoformat(),
        machine={"os": platform.platform(), "python": platform.python_version(), "gpu": None},
        checks=checks,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    report = run_checks()
    paths = Paths.from_env()
    paths.ensure()
    out = Path(a.out) if a.out else paths.logs / f"gate-m0-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    for c in report.checks:
        print(("PASS" if c.passed else "FAIL"), c.name, c.value if c.value is not None else "")
    print("report:", out)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
