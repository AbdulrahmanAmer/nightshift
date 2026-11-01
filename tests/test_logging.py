import json
from datetime import UTC, datetime
from pathlib import Path

from ns_common.logging import NsLogger


def test_writes_jsonl_with_required_fields(tmp_path: Path):
    fixed = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
    log = NsLogger("studio", tmp_path, echo=False, clock=lambda: fixed)
    log.info("stage_start", "starting ideas", job_id="j1", tenant="llama")
    f = tmp_path / "studio-20261101.jsonl"
    assert f.exists()
    rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    assert rec == {
        "ts": "2026-11-01T03:00:00+00:00",
        "level": "INFO",
        "component": "studio",
        "event": "stage_start",
        "msg": "starting ideas",
        "data": {"job_id": "j1", "tenant": "llama"},
    }


def test_timed_logs_start_and_end(tmp_path: Path):
    log = NsLogger("studio", tmp_path, echo=False)
    with log.timed("render", job_id="j1"):
        pass
    lines = [json.loads(line) for line in (next(tmp_path.glob("*.jsonl"))).read_text().splitlines()]
    assert [r["event"] for r in lines] == ["render_start", "render_end"]
    assert "duration_s" in lines[1]["data"]


def test_non_serializable_data_is_stringified(tmp_path: Path):
    log = NsLogger("x", tmp_path, echo=False)
    log.warn("odd", path=Path("C:/a"))
    rec = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[0])
    assert rec["data"]["path"] == "C:\\a" or rec["data"]["path"] == "C:/a"
