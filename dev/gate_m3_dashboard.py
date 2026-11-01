"""M3 gate for the Dashboard (component C9).

Starts the app in-process with FastAPI's TestClient on fake services, enqueues a fake clip job through
`/api/jobs/clip`, waits for it, checks that the clip shows up in the pending review queue, approves it,
and checks status. Needs `fastapi` (and `httpx`); `--fake-services` is accepted for symmetry with the
other gates and is always on.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common import ffmpeg
from ns_common.models import GateCheck, GateReport
from ns_common.paths import Paths

SLICE = "m3-c9"
BASE = "http://127.0.0.1:8100"
TERMINAL = ("done", "failed", "cancelled")


def _check(
    checks: list[GateCheck], name: str, passed: bool, value: Any = None, limit: str | None = None
) -> bool:
    shown = None if value is None else str(value)
    checks.append(GateCheck(name=name, passed=bool(passed), value=shown, limit=limit))
    return bool(passed)


def _wait_for(client: Any, job_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job.get("status") in TERMINAL:
            return job
        time.sleep(0.05)
    return job


def _run_in(home: Path, checks: list[GateCheck]) -> None:
    from fastapi.testclient import TestClient

    from ns_dashboard.handlers import build_state
    from ns_dashboard.server import create_app
    from ns_dashboard.supervisor_link import SupervisorLink

    paths = Paths.from_env()
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.3))
    static = home / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
    app = create_app(state=state, start_worker=True, static_dir=static)
    fixture = home / "fixtures" / "source.mp4"
    if ffmpeg.available():
        ffmpeg.make_test_clip(fixture, width=320, height=180, seconds=2.0)
    else:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096)

    with TestClient(app, base_url=BASE) as client:
        status = client.get("/api/status")
        ok = status.status_code == 200 and "engine" in status.json()
        _check(checks, "status_ok", ok, status.status_code)
        refused = client.get("/api/status", headers={"host": "evil.example"})
        _check(checks, "host_header_rejected", refused.status_code == 403, refused.status_code, "403")

        made = client.post(
            "/api/channels",
            json={"slug": "gate-channel", "display_name": "Gate Channel", "niche": "gate tests"},
        )
        _check(checks, "channel_created", made.status_code == 201, made.status_code, "201")

        no_rights = client.post("/api/jobs/clip", json={"path": str(fixture), "channel": "gate-channel"})
        _check(checks, "rights_required", no_rights.status_code == 400, no_rights.status_code, "400")

        resp = client.post(
            "/api/jobs/clip",
            json={
                "path": str(fixture),
                "channel": "gate-channel",
                "template": "auto",
                "max_clips": 2,
                "min_s": 20,
                "max_s": 60,
                "rights_confirmed": True,
            },
        )
        enqueued = resp.status_code == 201 and bool(resp.json().get("job_id"))
        _check(checks, "clip_job_enqueued", enqueued, resp.status_code, "201")
        if not enqueued:
            return
        job = _wait_for(client, resp.json()["job_id"], 60.0)
        _check(checks, "clip_job_done", job.get("status") == "done", job.get("status"), "done within 60 s")

        pending = client.get("/api/outputs", params={"status": "pending", "kind": "clip"}).json()
        ids = (job.get("result") or {}).get("output_ids", [])
        listed = bool(ids) and {i["output_id"] for i in pending} >= set(ids)
        _check(checks, "clip_in_pending_queue", listed, len(pending), ">= 1 pending clip")
        if not listed:
            return

        oid = ids[0]
        ranged = client.get(f"/api/media/{oid}", headers={"Range": "bytes=0-9"})
        ok = ranged.status_code == 206 and len(ranged.content) == 10
        _check(checks, "media_range_206", ok, ranged.status_code)
        approved = client.post(f"/api/outputs/{oid}/approve")
        _check(checks, "approve_ok", approved.status_code == 200 and approved.json()["status"] == "approved")
        detail = client.get(f"/api/outputs/{oid}").json()
        got = detail.get("status")
        _check(checks, "status_is_approved", got == "approved", got, "approved")
        queue = client.get("/api/outputs", params={"status": "pending"}).json()
        still_pending = {i["output_id"] for i in queue}
        _check(checks, "left_pending_queue", oid not in still_pending)


def run_checks() -> GateReport:
    """Run the gate in a temporary NIGHTSHIFT_HOME and return the report."""
    checks: list[GateCheck] = []
    try:
        import fastapi  # noqa: F401
    except ImportError as e:
        _check(checks, "fastapi_installed", False, e)
        return _report(checks)
    saved = os.environ.get("NIGHTSHIFT_HOME")
    with tempfile.TemporaryDirectory(prefix="ns-gate-m3-") as td:
        home = Path(td) / "home"
        home.mkdir()
        os.environ["NIGHTSHIFT_HOME"] = str(home)
        try:
            _run_in(home, checks)
        except Exception as e:  # report any crash as a failed check instead of a traceback
            _check(checks, "gate_crashed", False, f"{type(e).__name__}: {e}")
        finally:
            if saved is None:
                os.environ.pop("NIGHTSHIFT_HOME", None)
            else:
                os.environ["NIGHTSHIFT_HOME"] = saved
    return _report(checks)


def _report(checks: list[GateCheck]) -> GateReport:
    return GateReport(
        slice=SLICE,
        date=datetime.now(UTC).isoformat(),
        machine={"os": platform.platform(), "python": platform.python_version(), "gpu": None},
        checks=checks,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: writes `logs/gate-m3-dashboard-<ts>.json` and returns 0 when every check passed."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="report path (default: logs/gate-m3-dashboard-<ts>.json)")
    ap.add_argument("--fake-services", action="store_true", help="accepted for symmetry; always on")
    args = ap.parse_args(argv)
    real_logs = Paths.from_env().logs
    report = run_checks()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else real_logs / f"gate-m3-dashboard-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    for c in report.checks:
        print(("PASS" if c.passed else "FAIL"), c.name, c.value if c.value is not None else "")
    print("report:", out)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
