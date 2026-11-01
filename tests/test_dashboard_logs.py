import json
from datetime import UTC, datetime
from pathlib import Path

from ns_common.logging import NsLogger
from ns_dashboard.logs_tail import component_of, tail


def write(logs: Path, name: str, rows: list[dict | str]) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / name).open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def rec(ts: str, level: str, component: str, event: str = "e") -> dict:
    return {"ts": ts, "level": level, "component": component, "event": event, "msg": "", "data": {}}


def test_component_of():
    assert component_of(Path("supervisor-20261101.jsonl")) == "supervisor"
    assert component_of(Path("studio-core-20261101.jsonl")) == "studio-core"
    assert component_of(Path("odd.jsonl")) == "odd"


def test_merges_components_sorted_by_time(tmp_path: Path):
    write(tmp_path, "a-20261101.jsonl", [rec("2026-11-01T01:00:00+00:00", "INFO", "a", "a1"),
                                         rec("2026-11-01T03:00:00+00:00", "INFO", "a", "a2")])
    write(tmp_path, "b-20261101.jsonl", [rec("2026-11-01T02:00:00+00:00", "INFO", "b", "b1")])
    out = tail(tmp_path)
    assert [r["event"] for r in out["lines"]] == ["a1", "b1", "a2"]
    assert out["components"] == ["a", "b"]


def test_filters_component_level_n_after(tmp_path: Path):
    rows = [rec(f"2026-11-01T00:00:0{i}+00:00", lvl, "a", f"e{i}")
            for i, lvl in enumerate(["DEBUG", "INFO", "WARN", "ERROR", "INFO"])]
    write(tmp_path, "a-20261101.jsonl", rows)
    write(tmp_path, "b-20261101.jsonl", [rec("2026-11-01T00:00:09+00:00", "ERROR", "b", "b1")])
    assert [r["event"] for r in tail(tmp_path, component="a")["lines"]] == ["e0", "e1", "e2", "e3", "e4"]
    assert [r["event"] for r in tail(tmp_path, level="WARN")["lines"]] == ["e2", "e3", "b1"]
    assert [r["event"] for r in tail(tmp_path, level="warn", component="a")["lines"]] == ["e2", "e3"]
    assert [r["event"] for r in tail(tmp_path, component="a", n=2)["lines"]] == ["e3", "e4"]
    later = tail(tmp_path, component="a", after="2026-11-01T00:00:02+00:00")["lines"]
    assert [r["event"] for r in later] == ["e3", "e4"]
    assert tail(tmp_path, component="zzz")["lines"] == []


def test_tolerates_bad_lines_and_missing_dir(tmp_path: Path):
    write(tmp_path, "a-20261101.jsonl", ["{broken", "[]", json.dumps({"no": "ts"}),
                                         json.dumps(rec("2026-11-01T00:00:00+00:00", "INFO", "a", "ok"))])
    assert [r["event"] for r in tail(tmp_path)["lines"]] == ["ok"]
    assert tail(tmp_path / "missing") == {"lines": [], "components": []}


def test_reads_only_the_end_of_big_files(tmp_path: Path):
    rows = [rec(f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}+00:00", "INFO", "a", f"e{i}") for i in range(1000)]
    write(tmp_path, "a-20261101.jsonl", rows)
    out = tail(tmp_path, n=5, max_bytes=2000)
    assert [r["event"] for r in out["lines"]][-1] == "e999"
    assert len(out["lines"]) == 5


def test_reads_what_nslogger_writes(tmp_path: Path):
    log = NsLogger("dashboard", tmp_path, clock=lambda: datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    log.info("job_start", "hello", job_id="j1")
    log.error("job_failed", "boom")
    out = tail(tmp_path, component="dashboard", level="ERROR")
    assert [r["event"] for r in out["lines"]] == ["job_failed"]
    assert tail(tmp_path)["lines"][0]["data"] == {"job_id": "j1"}
